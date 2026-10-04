"""The ClearVenue bundle: built from the fetched commit, the image built from it, published
beside the image, and pruned with it — tags included.

Driven for real where it can be: the build script runs against a local checkout whose
builder and signer are stand-ins with the upstream ones' command lines, the push runs
against a registry that answers as the OCI distribution API does, and tag pruning deletes
tags from a real bare repository.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
PIPELINE = (REPO / "jenkinsfile-ha").read_text(encoding="utf-8")
VERSION = "20261003.04"


def _load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


# ── Building it ───────────────────────────────────────────────────────────────────

#: Stands in for ClearSignage's scripts/build_venue_bundle.py: same arguments, same output.
FAKE_BUILDER = '''
import argparse, io, json, sys, tarfile
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("out_dir", type=Path)
parser.add_argument("--version")
parser.add_argument("--revision")
parser.add_argument("--screen-release", type=Path, default=None)
parser.add_argument("--repository", default="")
args = parser.parse_args()
top = f"clearvenue-{args.version}"
stated = {"product": "clearvenue", "version": args.version, "revision": args.revision}
if args.repository:
    stated["repository"] = args.repository
files = {
    "VERSION": args.version + "\\n",
    "RELEASE.json": json.dumps(stated),
    "clearvenue/__main__.py": "from bundle\\n",
    "device/requirements.txt": "fastapi\\n",
}
if args.screen_release:
    files["screen-release/carried"] = "yes\\n"
args.out_dir.mkdir(parents=True, exist_ok=True)
path = args.out_dir / f"{top}.tar.gz"
with tarfile.open(path, "w:gz") as archive:
    for name, text in files.items():
        info = tarfile.TarInfo(f"{top}/{name}")
        info.size = len(text.encode())
        archive.addfile(info, io.BytesIO(text.encode()))
print(path)
'''

#: Stands in for ClearSignage's packaging/sign_venue_release.py.
FAKE_SIGNER = '''
import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("bundle", type=Path)
parser.add_argument("--channel")
parser.add_argument("--key-id")
parser.add_argument("--out", type=Path)
args = parser.parse_args()
assert os.environ["UPDATE_SIGNING_PRIVATE_KEY"] == "PEM"
args.out.write_text(json.dumps({"channel": args.channel, "key_id": args.key_id,
                                "package": args.bundle.name}))
'''


@pytest.fixture
def packaging(tmp_path):
    """A copy of this repo's scripts, a fetched src/, and the checkout it came from."""
    root = tmp_path / "packaging"
    (root / "scripts").mkdir(parents=True)
    for name in ("build-venue-bundle.sh", "channel-env.sh", "channels.py"):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    checkout = root / ".upstream" / "clearsignage"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    (checkout / "scripts").mkdir()
    (checkout / "packaging").mkdir()
    (checkout / "scripts" / "build_venue_bundle.py").write_text(FAKE_BUILDER)
    (checkout / "packaging" / "sign_venue_release.py").write_text(FAKE_SIGNER)
    _git(checkout, "add", ".")
    _git(checkout, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    src = root / "clearvenue" / "src"
    (src / "clearvenue").mkdir(parents=True)
    (src / "clearvenue" / "__main__.py").write_text("from fetch\n")
    (src / "CLEARSIGNAGE_REF").write_text(_git(checkout, "rev-parse", "HEAD") + "\n")
    return root


def _build(root: Path, **env: str) -> subprocess.CompletedProcess:
    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in {"UPDATE_SIGNING_PRIVATE_KEY", "BUNDLE_REQUIRED", "BUNDLE_VERSION"}
    }
    return subprocess.run(
        ["bash", str(root / "scripts" / "build-venue-bundle.sh")],
        env={**clean, "CHANNEL": "stable", "BUNDLE_VERSION": VERSION, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_image_is_built_from_the_bundle_and_the_signed_release_is_kept_for_publishing(
    packaging,
):
    (packaging / "clearvenue" / "src" / "screen-release").mkdir()
    ref = (packaging / "clearvenue" / "src" / "CLEARSIGNAGE_REF").read_text()

    ran = _build(packaging, UPDATE_SIGNING_PRIVATE_KEY="PEM", BUNDLE_REQUIRED="true")

    assert ran.returncode == 0, ran.stderr
    src = packaging / "clearvenue" / "src"
    assert (src / "clearvenue" / "__main__.py").read_text() == "from bundle\n"
    assert (src / "VERSION").read_text().strip() == VERSION
    assert (src / "screen-release" / "carried").exists(), "the screen release rides inside it"
    assert (src / "CLEARSIGNAGE_REF").read_text() == ref
    assert json.loads((src / "RELEASE.json").read_text())["revision"] == ref.strip()
    released = packaging / "venue-release"
    assert sorted(path.name for path in released.iterdir()) == [
        f"clearvenue-{VERSION}.tar.gz",
        "clearvenue-stable.json",
    ]
    signed = json.loads((released / "clearvenue-stable.json").read_text())
    assert signed == {
        "channel": "stable",
        "key_id": "release-2026-08",
        "package": f"clearvenue-{VERSION}.tar.gz",
    }


def test_the_release_says_where_its_updates_are_published_whatever_its_channel(packaging):
    """A venue installed from the bundle looks there, so ClearVenue's own code names nobody."""
    ran = _build(packaging, UPDATE_SIGNING_PRIVATE_KEY="PEM")

    assert ran.returncode == 0, ran.stderr
    stated = json.loads((packaging / "clearvenue" / "src" / "RELEASE.json").read_text())
    assert stated["repository"] == "madebyjansen/clearvenue"


def test_a_builder_from_before_the_option_is_not_told_it(packaging):
    builder = packaging / ".upstream" / "clearsignage" / "scripts" / "build_venue_bundle.py"
    builder.write_text(
        builder.read_text()
        .replace('parser.add_argument("--repository", default="")\n', "")
        .replace("if args.repository:\n    stated[\"repository\"] = args.repository\n", "")
    )
    assert "repository" not in builder.read_text()

    ran = _build(packaging, UPDATE_SIGNING_PRIVATE_KEY="PEM")

    assert ran.returncode == 0, ran.stderr
    stated = json.loads((packaging / "clearvenue" / "src" / "RELEASE.json").read_text())
    assert "repository" not in stated


def test_a_dry_run_without_the_key_builds_from_the_bundle_and_signs_nothing(packaging):
    ran = _build(packaging)

    assert ran.returncode == 0, ran.stderr
    assert "not signed" in ran.stdout
    src = packaging / "clearvenue" / "src"
    assert (src / "clearvenue" / "__main__.py").read_text() == "from bundle\n"
    assert not (packaging / "venue-release" / "clearvenue-stable.json").exists()


def test_a_published_bundle_without_the_key_stops_the_build(packaging):
    ran = _build(packaging, BUNDLE_REQUIRED="true")

    assert ran.returncode == 1
    assert "must be signed" in ran.stderr
    assert (packaging / "clearvenue" / "src" / "clearvenue" / "__main__.py").read_text() == (
        "from fetch\n"
    ), "nothing was replaced"


def test_a_commit_from_before_the_bundle_keeps_the_fetched_source_and_publishes_none(packaging):
    checkout = packaging / ".upstream" / "clearsignage"
    _git(checkout, "rm", "-q", "scripts/build_venue_bundle.py")
    _git(checkout, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "old")
    (packaging / "clearvenue" / "src" / "CLEARSIGNAGE_REF").write_text(
        _git(checkout, "rev-parse", "HEAD") + "\n"
    )

    ran = _build(packaging, BUNDLE_REQUIRED="true", UPDATE_SIGNING_PRIVATE_KEY="PEM")

    assert ran.returncode == 0, ran.stderr
    assert "predates" in ran.stdout
    assert (packaging / "clearvenue" / "src" / "clearvenue" / "__main__.py").read_text() == (
        "from fetch\n"
    )
    assert not (packaging / "venue-release").exists()


def test_a_checkout_that_is_not_the_fetched_commit_is_refused(packaging):
    (packaging / "clearvenue" / "src" / "CLEARSIGNAGE_REF").write_text("a" * 40 + "\n")

    ran = _build(packaging)

    assert ran.returncode == 1
    assert "run it again" in ran.stderr


@pytest.mark.parametrize("version", ["", "1.4.0", "20261003.1"])
def test_a_version_that_is_not_the_app_version_stops_the_build(packaging, version):
    ran = _build(packaging, BUNDLE_VERSION=version)

    assert ran.returncode == 2
    assert "YYYYMMDD.NN" in ran.stderr


def test_the_source_fetch_keeps_the_checkout_the_bundle_is_built_from():
    fetch = (REPO / "scripts" / "fetch-source.sh").read_text(encoding="utf-8")
    bundle = (REPO / "scripts" / "build-venue-bundle.sh").read_text(encoding="utf-8")
    assert 'CHECKOUT="${HERE}/.upstream/clearsignage"' in fetch
    assert 'UPSTREAM="${HERE}/.upstream/clearsignage"' in bundle
    assert '"${UPSTREAM}/scripts/build_venue_bundle.py"' in bundle
    assert '"${UPSTREAM}/packaging/sign_venue_release.py"' in bundle
    assert not list(REPO.rglob("sign_venue_release.py")), "a signer was copied into this repo"


# ── Publishing it ─────────────────────────────────────────────────────────────────


class FakeRegistry:
    """The OCI distribution API, as far as a push asks it."""

    def __init__(self, *, refuse_login: bool = False) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.refuse_login = refuse_login
        self.authorisations: list[str] = []

    def __call__(self, request):
        url, method = request.full_url, request.get_method()
        self.authorisations.append(request.get_header("Authorization") or "")
        if "/token?" in url:
            if self.refuse_login:
                raise urllib.error.HTTPError(url, 401, "no", {}, io.BytesIO(b""))
            return _Reply(200, {}, json.dumps({"token": "bearer-1"}).encode())
        path = url.split("/v2/madebyjansen/clearvenue/", 1)[1]
        if method == "HEAD" and path.startswith("blobs/"):
            if path[len("blobs/") :] in self.blobs:
                return _Reply(200, {}, b"")
            raise urllib.error.HTTPError(url, 404, "missing", {}, io.BytesIO(b""))
        if method == "POST" and path == "blobs/uploads/":
            return _Reply(202, {"Location": "/v2/madebyjansen/clearvenue/blobs/uploads/u1"}, b"")
        if method == "PUT" and path.startswith("blobs/uploads/u1?digest="):
            digest = path.split("digest=", 1)[1].replace("%3A", ":")
            assert "sha256:" + hashlib.sha256(request.data).hexdigest() == digest
            self.blobs[digest] = request.data
            return _Reply(201, {}, b"")
        if method == "PUT" and path.startswith("manifests/"):
            self.manifests[path[len("manifests/") :]] = request.data
            return _Reply(201, {}, b"")
        if method == "GET" and path.startswith("manifests/"):
            return _Reply(200, {}, self.manifests[path[len("manifests/") :]])
        raise AssertionError(f"unexpected {method} {url}")


class _Reply(io.BytesIO):
    def __init__(self, status, headers, body):
        super().__init__(body)
        self.status = status
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _release(folder: Path, *, channel: str = "stable", sha: str | None = None) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    bundle = b"the bundle's bytes"
    (folder / f"clearvenue-{VERSION}.tar.gz").write_bytes(bundle)
    (folder / f"clearvenue-{channel}.json").write_text(
        json.dumps(
            {
                "product": "clearvenue",
                "channel": channel,
                "version": VERSION,
                "package": f"clearvenue-{VERSION}.tar.gz",
                "sha256": sha or hashlib.sha256(bundle).hexdigest(),
                "signature": "c2lnbmVk",
            }
        )
    )
    return bundle


def test_the_bundle_is_published_under_its_version_and_as_the_newest_one(tmp_path):
    pusher = _load("push-venue-bundle.py", "push_venue_bundle")
    registry = FakeRegistry()
    bundle = _release(tmp_path / "venue-release")

    tags = pusher.push(
        pusher.Registry("madebyjansen/clearvenue", "ci", "ghp-secret", opener=registry),
        "stable",
        VERSION,
        tmp_path / "venue-release",
    )

    assert tags == [f"{VERSION}-bundle", "bundle"]
    assert registry.manifests[f"{VERSION}-bundle"] == registry.manifests["bundle"]
    artifact = json.loads(registry.manifests["bundle"])
    titles = {
        layer["annotations"]["org.opencontainers.image.title"]: layer["digest"]
        for layer in artifact["layers"]
    }
    # The names the venue's updater reads each file by.
    assert titles[f"clearvenue-{VERSION}.tar.gz"] == "sha256:" + hashlib.sha256(bundle).hexdigest()
    assert "clearvenue-stable.json" in titles
    assert set(titles.values()) <= set(registry.blobs)
    assert artifact["config"]["digest"] in registry.blobs
    assert "Basic " in registry.authorisations[0]
    assert all("ghp-secret" not in said for said in registry.authorisations[1:])


def test_a_signed_manifest_that_does_not_describe_the_bundle_is_not_published(tmp_path):
    pusher = _load("push-venue-bundle.py", "push_venue_bundle")
    registry = FakeRegistry()
    _release(tmp_path / "venue-release", sha="0" * 64)

    with pytest.raises(pusher.PushError, match="sha256"):
        pusher.push(
            pusher.Registry("madebyjansen/clearvenue", "ci", "t", opener=registry),
            "stable",
            VERSION,
            tmp_path / "venue-release",
        )
    assert registry.manifests == {}


def test_a_refused_login_says_so_without_the_token(tmp_path, monkeypatch, capsys):
    pusher = _load("push-venue-bundle.py", "push_venue_bundle")
    _release(tmp_path / "venue-release")
    monkeypatch.setattr(pusher.urllib.request, "urlopen", FakeRegistry(refuse_login=True))
    monkeypatch.setenv("GHCR_USERNAME", "ci")
    monkeypatch.setenv("GHCR_TOKEN", "ghp-secret")

    code = pusher.main(["--channel", "stable", "--version", VERSION, "--dir", str(tmp_path / "venue-release")])

    assert code == 1
    said = capsys.readouterr().err
    assert "refused the login" in said and "ghp-secret" not in said


def test_a_release_with_no_bundle_publishes_nothing(tmp_path, capsys):
    pusher = _load("push-venue-bundle.py", "push_venue_bundle")

    code = pusher.main(["--channel", "beta", "--version", VERSION, "--dir", str(tmp_path / "none")])

    assert code == 0
    assert "nothing to publish" in capsys.readouterr().out


def test_each_channel_publishes_into_its_own_package():
    pusher = _load("push-venue-bundle.py", "push_venue_bundle")
    assert [pusher.repository(channel) for channel in ("stable", "beta", "dev")] == [
        "madebyjansen/clearvenue",
        "madebyjansen/clearvenue-beta",
        "madebyjansen/clearvenue-dev",
    ]


# ── Counting and pruning it ───────────────────────────────────────────────────────


def test_a_bundle_tag_is_the_same_release_when_the_next_version_is_chosen():
    import datetime as dt

    chooser = _load("next-image-version.py", "next_image_version")
    tags = ["20261003.01", "20261003.01-amd64", "20261003.02-bundle", "bundle", "latest"]
    assert chooser.next_version(tags, dt.date(2026, 10, 3)) == "20261003.03"


def test_an_old_release_s_bundle_is_pruned_with_its_images():
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")
    versions = []
    for number, release in enumerate(("20261001.01", "20261002.01", "20261003.01")):
        for offset, suffix in enumerate(("", "-aarch64", "-amd64", "-bundle")):
            tags = [f"{release}{suffix}"]
            if release == "20261003.01" and suffix == "-bundle":
                tags.append("bundle")
            versions.append({"id": number * 10 + offset, "metadata": {"container": {"tags": tags}}})

    doomed = pruner.versions_to_delete(versions, "20261003.01")

    assert doomed == [0, 1, 2, 3]
    assert pruner.published_releases(versions, doomed) == {"20261002.01", "20261003.01"}


def test_the_tags_of_releases_whose_images_are_gone_are_selected_and_no_others():
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")
    tags = [
        "stable/v20260929.01",
        "stable/v20261001.01",
        "stable/v20261002.01",
        "stable/v20261003.01",
        "stable/v20261004.01",
        "beta/v20260929.01",
        "v20260831.01",
        "v20260928.01",
        "something-else",
    ]

    doomed = pruner.git_tags_to_delete(
        tags, "stable", published={"20261002.01", "20261003.01"}, current="20261003.01"
    )

    assert doomed == [
        "v20260831.01",
        "v20260928.01",
        "stable/v20260929.01",
        "stable/v20261001.01",
    ], "another channel's, the published, a newer one and an unknown tag are all kept"


def test_pruning_deletes_those_tags_from_the_remote(tmp_path, capsys):
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", str(work))
    (work / "f").write_text("x")
    _git(work, "add", "f")
    _git(work, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-qm", "x")
    for tag in ("stable/v20261001.01", "stable/v20261002.01", "v20260831.01", "dev/v20261001.01"):
        _git(work, "tag", tag)
    _git(work, "push", "-q", str(remote), "--tags")

    pruner.prune_git_tags(str(remote), "stable", {"20261002.01"}, "20261002.01", apply=False)
    assert len(pruner.remote_tags(str(remote))) == 4, "a dry run deletes nothing"

    pruner.prune_git_tags(str(remote), "stable", {"20261002.01"}, "20261002.01", apply=True)

    assert sorted(pruner.remote_tags(str(remote))) == ["dev/v20261001.01", "stable/v20261002.01"]
    assert "Deleted tag stable/v20261001.01" in capsys.readouterr().out


def test_a_remote_that_cannot_be_read_is_said_and_does_not_fail_the_release(tmp_path, capsys):
    pruner = _load("prune-ghcr-releases.py", "prune_ghcr_releases")

    pruner.prune_git_tags(str(tmp_path / "missing.git"), "stable", set(), "20261002.01", apply=True)

    assert "Could not prune the release tags" in capsys.readouterr().err


# ── Both pipelines ────────────────────────────────────────────────────────────────


def test_jenkins_builds_the_bundle_before_the_image_and_publishes_it_before_recording():
    order = [
        "stage('Build the screen release')",
        "stage('Build the venue bundle')",
        "stage('Build and publish')",
        "stage('Publish the venue bundle')",
        "stage('Record the published version')",
        "stage('Prune old releases')",
    ]
    assert [PIPELINE.index(stage) for stage in order] == sorted(
        PIPELINE.index(stage) for stage in order
    )
    building = PIPELINE[PIPELINE.index(order[1]) : PIPELINE.index(order[2])]
    assert 'BUNDLE_VERSION="${APP_VERSION}"' in building
    assert "string(credentialsId: 'update-signing-private-key'" in building
    assert '"BUNDLE_REQUIRED=${params.PUSH}"' in building
    publishing = PIPELINE[PIPELINE.index(order[3]) : PIPELINE.index(order[4])]
    assert "expression { params.PUSH }" in publishing
    assert 'GHCR_TOKEN="${GHCR_PSW}"' in publishing
    pruning = PIPELINE[PIPELINE.index(order[5]) :]
    assert "GIT_ASKPASS" in pruning, "deleting a tag is a push, authenticated as one"
    assert "rm -rf clearvenue/src clearvenue_beta/src clearvenue_dev/src .upstream venue-release" in PIPELINE


def test_actions_runs_the_same_steps_in_the_same_order():
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "homeassistant.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    order = [
        "Build the screen release",
        "Build the venue bundle",
        "Build both architectures",
        "Publish the venue bundle",
        "Record the published version",
        "Prune old releases",
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    building = steps[names.index("Build the venue bundle")]
    assert building["env"]["BUNDLE_REQUIRED"] == "${{ inputs.push }}"
    assert building["env"]["BUNDLE_VERSION"] == "${{ steps.version.outputs.version }}"
    publishing = steps[names.index("Publish the venue bundle")]
    assert publishing["if"] == "inputs.push"
    assert publishing["env"]["GHCR_TOKEN"] == "${{ github.token }}"
    assert ".upstream venue-release" in steps[names.index("Remove private source")]["run"]


def test_actions_can_publish_a_whole_release_without_jenkins():
    """The workflow is the way to publish while Jenkins is down, so it needs every right
    the Jenkins credentials give: push and delete packages, push the version commit, and
    delete the tags of pruned releases — which it does with the checkout's own token."""
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "homeassistant.yml").read_text(encoding="utf-8")
    )
    assert workflow["permissions"] == {"contents": "write", "packages": "write"}
    assert "workflow_dispatch" in workflow[True], "it is started by a person, as Jenkins is"
    steps = workflow["jobs"]["build"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout.get("with", {}).get("persist-credentials") is True
    names = [step["name"] for step in steps]
    for jenkins_stage in (
        "Build the screen release",
        "Build the venue bundle",
        "Publish the venue bundle",
        "Record the published version",
        "Prune old releases",
    ):
        assert f"stage('{jenkins_stage}')" in PIPELINE
        assert jenkins_stage in names, f"Jenkins runs {jenkins_stage!r} and Actions does not"
