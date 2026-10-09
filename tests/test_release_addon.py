"""The add-on built for one ClearSignage release.

ClearSignage's one release workflow calls ``scripts/release-addon.sh`` when a run ticks the
Home Assistant destination, handing it the checkout it gated and the screen release it signed.
Driven here as it runs: from a copy of this repository, against a ClearSignage checkout made in
the test, a ``docker`` that records what it is asked, and the publishing scripts replaced by
ones that record their arguments. ``fetch-source.sh``, ``channels.py`` and the screen release's
check run for real — the check through the fake checkout's own verifier, because it is the
commit being shipped that decides what a screen trusts.

What this repository no longer holds, and where it went: serialising runs and choosing the
release's number are the release workflow's, and are tested where it lives.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from channels import CHANNELS, channel_settings  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "scripts" / "release-addon.sh"
VERSION = "20261008.03"
TOKEN = "ghp_fixture-token"
ACCEPTED = "signed-by-the-release-key"

#: Stand-ins for the scripts that publish, each recording how it was called.
RECORDING = {
    "validate-packaging.sh": '#!/bin/bash\necho "validate" >> "$CALLS"\nexit "${FAIL_VALIDATE:-0}"\n',
    "build-venue-bundle.sh": (
        "#!/bin/bash\n"
        'source "$(dirname "$0")/channel-env.sh"\n'
        'here="$(cd "$(dirname "$0")/.." && pwd)"\n'
        'carries=no; [ -f "$here/$ADDON_DIR/src/screen-release/manifest-$CHANNEL.json" ] && carries=yes\n'
        'echo "bundle $BUNDLE_VERSION required=$BUNDLE_REQUIRED screens=$carries" >> "$CALLS"\n'
        'mkdir -p "$here/venue-release"\n'
    ),
    "next-image-version.py": (
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALLS'], 'a') as log:\n"
        "    log.write('version ' + ' '.join(args) + '\\n')\n"
        "given = args[args.index('--set') + 1] if '--set' in args else args[args.index('--release') + 1]\n"
        "print(os.environ.get('FAKE_APP_VERSION') or given)\n"
    ),
    "push-venue-bundle.py": (
        "import os, sys\n"
        "with open(os.environ['CALLS'], 'a') as log:\n"
        "    log.write('push-bundle ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    ),
    "record-published-version.sh": (
        '#!/bin/bash\necho "record $RECORD_VERSION on $RECORD_BRANCH" >> "$CALLS"\n'
    ),
    "prune-ghcr-releases.py": (
        "import os, sys\n"
        "with open(os.environ['CALLS'], 'a') as log:\n"
        "    log.write('prune ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    ),
}

# The real login consumes --password-stdin. Closing it early makes printf fail with SIGPIPE.
DOCKER = '#!/bin/bash\nprintf "docker %s\\n" "$*" >> "$CALLS"\n[ "$1" = "login" ] && cat >/dev/null\n[ "$1 $2" = "buildx inspect" ] && exit 1\nexit 0\n'

#: The verifier the fake ClearSignage commit ships: it trusts what its keyring names.
FAKE_UPDATES = '''
import json


class UpdateManifest:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    @classmethod
    def model_validate_json(cls, text):
        return cls(**json.loads(text))


def verify_manifest_signature(manifest, keyring):
    return manifest.signature == keyring["accepts"]
'''
FAKE_CHECKSUMS = '''
import hashlib


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
'''

#: What fetch-source.sh checks a ClearSignage commit carries before it builds from it.
SOURCE_FILES = {
    "device/requirements.txt": "",
    "device/constraints.txt": "",
    "device/app/main.py": "",
    "device/app/update_signing_public.json": json.dumps({"accepts": ACCEPTED}),
    "shared/pyproject.toml": "",
    "shared/signage_shared/__init__.py": "",
    "shared/signage_shared/updates.py": FAKE_UPDATES,
    "shared/signage_shared/checksums.py": FAKE_CHECKSUMS,
    "hosted/__main__.py": "",
    "clearvenue/__main__.py": "",
    "clearvenue/hosts/__init__.py": "",
    "event_share/app/VERSION": "1\n",
    "event_share/install/install.php": "",
}


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def bench(tmp_path):
    """Return a copy of this repository with its publishers recording, beside a checkout."""
    packaging = tmp_path / "clearvenue-ha"
    shutil.copytree(REPO / "scripts", packaging / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    for channel in CHANNELS:
        folder = channel_settings(channel)["addon_dir"]
        shutil.copytree(REPO / folder, packaging / folder)
    for name, body in RECORDING.items():
        script = packaging / "scripts" / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER, encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)

    source = tmp_path / "clearsignage"
    for relative, body in SOURCE_FILES.items():
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    _git(source, "add", ".")
    _git(source, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "release")
    return packaging, source, bin_dir, tmp_path / "calls"


def _screen_release(folder: Path, channel: str, *, version: str = VERSION, signature: str = ACCEPTED,
                    said_version: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    package = folder / f"clearsignage-{version}.tar.gz"
    package.write_bytes(b"a screen release")
    (folder / f"manifest-{channel}.json").write_text(
        json.dumps({
            "version": said_version or version,
            "channel": channel,
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "signature": signature,
        }),
        encoding="utf-8",
    )
    return folder


def _run(bench, channel: str = "stable", *, screens: Path | None = None, **env: str):
    packaging, source, bin_dir, calls = bench
    if screens is None:
        screens = _screen_release(packaging.parent / "dist", channel)
    # Nothing of the run calling this suite may leak in: release-addon.sh runs these tests
    # first, inside a release that may itself be a dry run.
    inherited = ("GHCR_", "UPDATE_", "RELEASE_", "CLEARSIGNAGE_", "SCREEN_RELEASE_", "DRY_RUN", "CHANNEL")
    clean = {key: value for key, value in os.environ.items() if not key.startswith(inherited)}
    ran = subprocess.run(
        ["bash", str(packaging / "scripts" / "release-addon.sh")],
        env={
            **clean,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(calls),
            "CHANNEL": channel,
            "CLEARSIGNAGE_SOURCE": str(source),
            "CLEARSIGNAGE_REF": _git(source, "rev-parse", "HEAD"),
            "RELEASE_VERSION": VERSION,
            "SCREEN_RELEASE_DIR": str(screens),
            "GHCR_USERNAME": "release-bot",
            "GHCR_TOKEN": TOKEN,
            "UPDATE_SIGNING_PRIVATE_KEY": "a key",
            "PYTHON": sys.executable,
            "DRY_RUN": "false",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    said = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return ran, said


def _image(channel: str) -> str:
    return channel_settings(channel)["image"]


# ── a release, published ─────────────────────────────────────────────────────────


def test_a_stable_release_is_built_published_recorded_and_pruned_in_that_order(bench):
    ran, said = _run(bench)

    assert ran.returncode == 0, ran.stderr
    image = _image("stable")
    steps = [line.split()[0] if not line.startswith("docker") else " ".join(line.split()[:3]) for line in said]
    assert steps == [
        "validate",
        "version",
        "bundle",
        "docker run --privileged",
        "docker buildx inspect",
        "docker buildx create",
        "docker login ghcr.io",
        "docker buildx build",
        "docker buildx build",
        "docker buildx imagetools",
        "docker buildx imagetools",
        "push-bundle",
        "record",
        "prune",
        "docker logout ghcr.io",
    ]
    assert said[1] == f"version --channel stable --release {VERSION}"
    assert said[2] == f"bundle {VERSION} required=true screens=yes", "the screen release rides in it"
    assert f"--tag {image}:{VERSION} --tag {image}:latest" in said[9]
    assert said[11] == f"push-bundle --channel stable --version {VERSION}"
    assert said[12] == f"record {VERSION} on main"
    assert said[13] == f"prune --channel stable --current {VERSION} --apply"


@pytest.mark.parametrize("channel", CHANNELS)
def test_each_channel_builds_both_architectures_of_its_own_image_from_the_release_commit(bench, channel):
    _packaging, source, _bin, _calls = bench
    ran, said = _run(bench, channel)

    assert ran.returncode == 0, ran.stderr
    builds = [line for line in said if line.startswith("docker buildx build")]
    commit = _git(source, "rev-parse", "HEAD")
    for line, (platform, arch) in zip(builds, [("linux/arm64", "aarch64"), ("linux/amd64", "amd64")]):
        assert f"--platform {platform}" in line
        assert f"--tag {_image(channel)}:{VERSION}-{arch}" in line
        assert f"--label org.opencontainers.image.revision={commit}" in line
        assert line.endswith(channel_settings(channel)["addon_dir"])
        assert "--push" in line
    assert len(builds) == 2


def test_a_release_built_again_is_a_new_add_on_carrying_the_same_bundle(bench):
    """Home Assistant needs a new version to offer an update; the code inside has not changed."""
    ran, said = _run(bench, FAKE_APP_VERSION=f"{VERSION}.1")

    assert ran.returncode == 0, ran.stderr
    assert f"bundle {VERSION} required=true screens=yes" in said
    assert any(f"{_image('stable')}:{VERSION}.1-aarch64" in line for line in said)
    assert not [line for line in said if line.startswith("push-bundle")], "one bundle per release"
    assert f"record {VERSION}.1 on main" in said
    assert "this rebuild publishes the add-on only" in ran.stdout


def test_dev_takes_the_number_the_release_workflow_gave_it(bench):
    ran, said = _run(bench, "dev")

    assert ran.returncode == 0, ran.stderr
    assert said[1] == f"version --channel dev --set {VERSION}", "dev's counter was already asked"


def test_the_source_built_is_the_release_commit_and_is_gone_afterwards(bench):
    packaging, source, _bin, _calls = bench
    ran, _said = _run(bench)

    assert ran.returncode == 0, ran.stderr
    assert f"Fetched {_git(source, 'rev-parse', 'HEAD')}" in ran.stdout
    for leftover in ("clearvenue/src", ".upstream", "venue-release"):
        assert not (packaging / leftover).exists(), f"{leftover} was left on the agent"


def test_the_registry_token_is_never_an_argument(bench):
    ran, said = _run(bench)

    assert ran.returncode == 0, ran.stderr
    assert not [line for line in said if TOKEN in line]
    assert "--password-stdin" in next(line for line in said if line.startswith("docker login"))


# ── a dry run ────────────────────────────────────────────────────────────────────


def test_a_dry_run_builds_everything_and_publishes_nothing(bench):
    ran, said = _run(bench, DRY_RUN="true", GHCR_TOKEN="", UPDATE_SIGNING_PRIVATE_KEY="")

    assert ran.returncode == 0, ran.stderr
    assert said[1] == f"version --channel stable --set {VERSION}", "a dry run reads no registry"
    assert f"bundle {VERSION} required=false screens=yes" in said
    builds = [line for line in said if line.startswith("docker buildx build")]
    assert len(builds) == 2 and all("--output=type=cacheonly" in line for line in builds)
    for publishing in ("docker login", "docker buildx imagetools", "push-bundle", "record", "prune"):
        assert not [line for line in said if line.startswith(publishing)], publishing
    assert "nothing was published" in ran.stdout


# ── what is refused before anything is built ─────────────────────────────────────


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"signature": "somebody else"}, "does not verify against the keyring"),
        ({"said_version": "20261008.09"}, "it names 20261008.09 (stable)"),
    ],
)
def test_a_screen_release_no_screen_would_take_stops_the_build(bench, change, said):
    packaging = bench[0]
    screens = _screen_release(packaging.parent / "dist", "stable", **change)

    ran, calls = _run(bench, screens=screens)

    assert ran.returncode != 0
    assert said in ran.stderr
    assert not [line for line in calls if line.startswith(("docker", "bundle"))]


def test_a_screen_release_that_is_not_there_stops_the_build(bench):
    ran, calls = _run(bench, screens=bench[0].parent / "nothing-here")

    assert ran.returncode == 2
    assert "sign it first" in ran.stderr
    assert calls == []


@pytest.mark.parametrize(
    ("env", "said"),
    [
        ({"RELEASE_VERSION": "1.4.0"}, "YYYYMMDD.NN"),
        ({"CLEARSIGNAGE_REF": "main"}, "full commit SHA"),
        ({"CHANNEL": "prod"}, "stable, beta or dev"),
        ({"GHCR_TOKEN": ""}, "GHCR_USERNAME and GHCR_TOKEN are needed"),
        ({"UPDATE_SIGNING_PRIVATE_KEY": ""}, "bundle must be signed"),
    ],
)
def test_a_run_missing_what_it_needs_says_so_before_anything(bench, env, said):
    ran, calls = _run(bench, **env)

    assert ran.returncode == 2
    assert said in ran.stderr
    assert calls == []


def test_packaging_that_fails_its_own_tests_builds_nothing(bench):
    ran, calls = _run(bench, FAIL_VALIDATE="1")

    assert ran.returncode != 0
    assert calls == ["validate"]
