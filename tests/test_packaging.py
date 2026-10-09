"""What this repository can actually be wrong about (Epic 119 Pass 4).

It ships no logic — the decisions live in ClearSignage's ``hosted`` package, which has its
own suite. What it *can* get wrong is the contract between the two: a manifest the
Supervisor rejects, an option the app ignores because its schema entry is missing, or a
path in the Dockerfile that no longer exists upstream. Those are the failures that only
show up on a real Home Assistant, at install time, so they are worth catching here.

The image build itself is not tested. Building it needs Docker and a base image per
architecture, and a test that skipped whenever those were absent would be the same silent
skip that let eight designer tests sit red on ClearSignage's main for a year.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "clearvenue"
CONFIG = yaml.safe_load((APP / "config.yaml").read_text())
BUILD = yaml.safe_load((APP / "build.yaml").read_text())
#: The one place the add-on is built and published, for ClearSignage's release workflow.
ENTRY = (REPO / "scripts" / "release-addon.sh").read_text()
RECORDER = (REPO / "scripts" / "record-published-version.sh").read_text()


@pytest.fixture(params=["stable", "beta", "dev"], autouse=True)
def packaging_channel(request):
    global APP, CONFIG, BUILD
    folder = "clearvenue" if request.param == "stable" else "clearvenue_" + request.param
    APP = REPO / folder
    CONFIG = yaml.safe_load((APP / "config.yaml").read_text())
    BUILD = yaml.safe_load((APP / "build.yaml").read_text())


def _load_script(filename: str, module_name: str):
    """Import a `scripts/` file that is a command, not an installed module."""
    path = REPO / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_pruner():
    return _load_script("prune-ghcr-releases.py", "prune_ghcr_releases")


def _load_version_chooser():
    return _load_script("next-image-version.py", "next_image_version")


def _dockerfile_directives() -> str:
    """Return the Dockerfile with comments stripped.

    Scanning the raw text would match the comments that *explain* why feh and a second
    avahi are absent — the prose asserting the property would fail the assertion. Written
    down because the first version of these tests did exactly that.
    """
    lines = (APP / "Dockerfile").read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _service_script(name: str, *, uncommented: bool = False) -> str:
    """Return an s6 service script, optionally with comment lines stripped.

    Same trap as `_dockerfile_directives`: these scripts explain in prose which binaries
    they must *not* call, so a test asserting the absence of one would match the sentence
    saying so. The shebang is kept — it is the interpreter, not a comment.
    """
    text = (APP / "rootfs/etc/services.d/clearvenue" / name).read_text()
    if not uncommented:
        return text
    lines = text.splitlines()
    return "\n".join(
        line
        for line in lines
        if line.startswith("#!") or not line.lstrip().startswith("#")
    )


def test_every_yaml_file_parses():
    for path in sorted(REPO.rglob("*.yaml")):
        if ".git" in path.parts or "src" in path.parts:
            continue
        assert isinstance(yaml.safe_load(path.read_text()), dict), path


def test_the_version_is_chosen_from_what_is_already_published():
    """YYYYMMDD.NN, counted from the registry rather than from a number in the repo.

    A counter kept in a file is wrong the moment two builds run from one commit, or a
    publish fails after its version was written down. The tags cannot disagree with
    themselves, which is why ClearSignage's own releases count from the objects in R2 and
    this counts from the objects in GHCR.
    """
    chooser = _load_version_chooser()
    day = dt.date(2026, 8, 31)

    assert chooser.next_version([], day) == "20260831.01"
    assert chooser.next_version(["20260831.01", "latest"], day) == "20260831.02"
    # The per-architecture tags published beside the manifest are the same release.
    arch_tags = ["20260831.01-aarch64", "20260831.01-amd64"]
    assert chooser.next_version(arch_tags, day) == "20260831.02"
    # Yesterday's releases, and the versions from before this scheme, are not this day's.
    assert chooser.next_version(["20260830.09", "0.1.936", "20260831"], day) == "20260831.01"
    # Counting reads the greatest, not the count: a pruned .01 must not be handed out twice.
    assert chooser.next_version(["20260831.04"], day) == "20260831.05"


def test_a_tag_for_today_that_makes_no_sense_stops_the_build():
    """Skipping it is how a version gets handed out twice, and the second one overwrites
    an image somebody is running. ClearSignage's own selector refuses for the same reason."""
    chooser = _load_version_chooser()
    with pytest.raises(ValueError):
        chooser.next_version(["20260831.1"], dt.date(2026, 8, 31))


def test_a_day_that_runs_out_of_counters_stops_rather_than_wrapping():
    """Two digits is the format; .100 would sort below .99 and collide with a published
    tag, so the ninety-ninth build of one day is where a person has to be told."""
    chooser = _load_version_chooser()
    with pytest.raises(OverflowError):
        chooser.next_version(["20260831.99"], dt.date(2026, 8, 31))


def test_stamping_the_manifest_touches_only_the_version():
    """The manifest is mostly comments explaining the decisions in it, and a rewrite that
    dropped them would be a silent loss no test would otherwise notice."""
    chooser = _load_version_chooser()
    original = (APP / "config.yaml").read_text(encoding="utf-8")
    stamped = chooser.stamp_version(original, "20260831.07")

    assert yaml.safe_load(stamped)["version"] == "20260831.07"
    assert stamped.count("\n") == original.count("\n")
    for line in original.splitlines():
        if not line.startswith("version:"):
            assert line in stamped, line

    # Stamping the same version again is the no-op the pipeline relies on to tell
    # "already recorded" from "needs a commit".
    assert chooser.stamp_version(stamped, "20260831.07") == stamped


def test_a_manifest_with_no_version_line_is_an_error_not_a_silent_no_op():
    chooser = _load_version_chooser()
    with pytest.raises(ValueError):
        chooser.stamp_version("---\nname: ClearSignage\n", "20260831.01")


def test_the_manifest_version_is_a_tag_the_registry_and_the_pruner_both_accept():
    """One string has to be three things: a Docker tag, what the Supervisor tracks, and
    something `prune-ghcr-releases.py` can order. A hand-written "1.0-beta" would publish
    and then quietly stop being prunable."""
    pruner = _load_pruner()
    assert re.fullmatch(r"\d{8}\.\d{2}", CONFIG["version"])
    assert pruner.release_from_tags([CONFIG["version"]]) == CONFIG["version"]

    # The first dated release had to be newer than the legacy version.  Once Jenkins
    # successfully recorded that release (and every release after it), the manifest is
    # expected to be equal to or newer than that boundary.  Comparing in the opposite
    # direction made validation fail immediately after its own first successful run.
    assert pruner.version_key(CONFIG["version"]) >= pruner.version_key("20260831.01")


def test_the_entry_chooses_the_version_and_records_what_it_published():
    """Both halves, because either one alone is broken.

    Choosing without recording publishes an image no install is offered — Home Assistant
    reads the version from config.yaml in this repository, so an uncommitted version
    reaches nobody. Recording without publishing first advertises a tag that is not in the
    registry yet, and an operator upgrading in that window gets a pull failure. The order is
    driven for real in test_release_addon.py; these are the lines that make it.
    """
    assert '"${HERE}/scripts/next-image-version.py"' in ENTRY
    assert ENTRY.index("next-image-version.py") < ENTRY.index("buildx build")
    assert ENTRY.index("imagetools create") < ENTRY.index("record-published-version.sh"), (
        "the version is recorded before the image it names exists"
    )
    assert 'RECORD_VERSION="${APP_VERSION}"' in ENTRY, "the recorded version must be the one built"
    assert "RECORD_BRANCH=main" in ENTRY
    # What that recorder does with it — plumbing, a retry on a moved branch, and a
    # failure that says the image is published — is driven in test_record_version.py
    # against real repositories, which is the point of it being a script.
    assert "THE IMAGE IS PUBLISHED but its version was not recorded" in RECORDER, (
        "a failure there leaves a published image nobody is offered; it has to say so"
    )
    assert "git checkout" not in RECORDER, (
        "the recorder must not move the workspace: later steps run this build's scripts"
    )


def test_the_github_token_never_reaches_a_url_or_an_argument():
    """The caller answers this repository's remote through GIT_ASKPASS for this reason: a PAT
    in a remote URL lands in `git config`, in `ps` output, and in any command echo."""
    assert "@github.com" not in ENTRY, "a credential in a remote URL lands in git config and ps"
    assert "GIT_ASKPASS" in ENTRY, "recording and pruning authenticate some other way"
    assert '--password-stdin' in ENTRY, "the registry token is never an argument"


def test_the_app_version_is_stated_in_exactly_one_place():
    """One number to bump, and the manifest is where it has to be.

    The Supervisor parses `config.yaml` and tracks an installed app by the version in it,
    so that one cannot be generated or templated — which makes it the source and every
    other copy a liability. `release.yaml` used to carry one, kept in step by a test that
    forced an edit rather than a review; the publish-time revision check replaced it.

    Asserted over every YAML file rather than the one that used to have it, because the
    next copy will be added somewhere else.
    """
    assert CONFIG["version"], "the manifest states no version"

    for path in sorted(REPO.rglob("*.yaml")):
        if ".git" in path.parts or "src" in path.parts or path.name == "config.yaml":
            continue
        loaded = yaml.safe_load(path.read_text()) or {}
        repeated = {
            key: value
            for key, value in loaded.items()
            if isinstance(value, str) and value == CONFIG["version"]
        }
        assert not repeated, (
            f"{path.relative_to(REPO)} repeats the app version in {sorted(repeated)}; "
            f"read it from clearvenue/config.yaml instead"
        )


def test_pipeline_labels_the_image_with_the_resolved_revision():
    assert '--label "org.opencontainers.image.revision=${CLEARSIGNAGE_REF}"' in ENTRY


def test_the_manifest_has_what_the_supervisor_requires():
    for key in ("name", "version", "slug", "description", "arch"):
        assert key in CONFIG, key
    assert re.fullmatch(r"[a-z0-9_]+", CONFIG["slug"])


def test_the_architectures_are_the_two_that_have_wheels():
    """32-bit Arm is absent on purpose: the device pins resolve to manylinux
    aarch64/x86_64 wheels, and HA OS dropped 32-bit anyway."""
    assert set(CONFIG["arch"]) == {"aarch64", "amd64"}
    assert set(BUILD["build_from"]) == set(CONFIG["arch"])


def test_ingress_is_on_and_has_a_port():
    """Ingress is the entry path that carries authority (DP64)."""
    assert CONFIG["ingress"] is True
    assert isinstance(CONFIG["ingress_port"], int)


def test_the_privileges_the_epic_requires_are_asked_for():
    """host_network for peer sync and discovery (DP65); host_dbus for avahi (DP66).

    Both are load-bearing rather than convenient — a peer addresses the host, not a
    container port, and HA OS already owns 5353.
    """
    assert CONFIG["host_network"] is True
    assert CONFIG["host_dbus"] is True


def test_staff_voice_can_reach_core_without_supervisor_management_access():
    """All channels must grant the Core STT adapter access to the configured providers."""
    assert CONFIG["homeassistant_api"] is True
    assert not CONFIG.get("hassio_api", False)
    assert "hassio_role" not in CONFIG


def test_no_privilege_is_asked_for_that_the_epic_does_not_justify():
    """An app asking for more than it needs is how a private repo becomes a liability."""
    assert "privileged" not in CONFIG, CONFIG.get("privileged")
    assert "devices" not in CONFIG
    assert "host_pid" not in CONFIG
    # /data is mounted for every app without asking; this app wants nothing else — not
    # Home Assistant's config, not its SSL, not its media.
    assert "map" not in CONFIG


def test_every_option_has_a_schema_entry():
    """A mismatch is how an app silently ignores what the operator typed."""
    assert set(CONFIG["options"]) == set(CONFIG["schema"])


@pytest.mark.parametrize("key", sorted(CONFIG["schema"]))
def test_each_schema_type_is_one_the_supervisor_understands(key):
    spec = CONFIG["schema"][key]
    assert re.fullmatch(
        r"(str|int|float|bool|port|email|url|password|match\(.*\)|list\(.*\))\??", spec
    ), (key, spec)


def test_the_default_log_level_is_one_of_the_allowed_values():
    allowed = CONFIG["schema"]["log_level"][len("list(") : -1].split("|")
    assert CONFIG["options"]["log_level"] in allowed


def test_the_watchdog_points_at_the_port_we_actually_serve():
    assert f"[PORT:{CONFIG['ingress_port']}]" in CONFIG["watchdog"]


def test_the_live_database_is_kept_out_of_home_assistant_s_backup():
    """A file-level copy of a live database is not a backup (Epic 149).

    The venue writes its state as three files at once and the Supervisor copies them moments
    apart while a kitchen is pressing buttons — so what comes back is a database missing the
    last few hours, or one SQLite will not open. All three are excluded together: excluding
    the database and leaving its write-ahead log behind would put half a database in the
    backup, which is worse than either whole answer.
    """
    excluded = set(CONFIG["backup_exclude"])

    assert {"venue.sqlite3", "venue.sqlite3-wal", "venue.sqlite3-shm"} <= excluded, (
        f"a database and its log have to travel together or not at all: {sorted(excluded)}"
    )


def test_something_writes_the_copy_that_is_backed_up_instead():
    """Excluding the database without this is a backup with no venue in it.

    The two halves are one decision and either alone is worse than neither: the exclusion
    without the hook backs up nothing, and the hook without the exclusion backs up a torn
    database beside a good copy of it.
    """
    assert "backup_pre" in CONFIG, "the database is excluded and nothing replaces it"
    assert "clearvenue.backup_hook" in CONFIG["backup_pre"], CONFIG["backup_pre"]


def test_nothing_that_is_a_second_whole_venue_rides_along_in_the_snapshot():
    """Two files that are not the backup and would double every snapshot they appear in.

    A `.damaged` database is one a repair moved aside because it would not open. It stays on
    the box deliberately — a recovery tool may still read rows out of it — but it is a local
    artefact, not something a restore ever wants, and it is the same size as the venue.

    A `.partial` is the hand-over copy caught mid-write. The copy is written under that name
    and moved into place, so one existing at all means an earlier hook died part way through:
    rubbish by definition, and rubbish that looks like a database.
    """
    excluded = {one.rstrip("/") for one in CONFIG["backup_exclude"]}

    assert "venue.sqlite3.damaged" in excluded, f"the snapshot doubles: {sorted(excluded)}"
    assert "venue-for-backup.sqlite3.partial" in excluded


def test_an_operators_own_copies_are_not_backed_up_as_well():
    """They are already copies of the venue, kept on their own rule and rotated on it.

    Backing them up would put several copies of the same venue in every snapshot, on a
    machine whose disk the live database also has to write to.
    """
    assert any(one.rstrip("/") == "copies" for one in CONFIG["backup_exclude"])


def test_this_platform_offers_no_local_names():
    """Decided in Epic 149, and the manifest is where it has to be true.

    Home Assistant has listened on 80 itself since 2026.9. Publishing anyway is worse than
    losing the names: avahi's record carries no port, so the name resolves, this venue's bind
    fails, and a volunteer typing it reaches Home Assistant's login page rather than the
    screen. The option, its schema entry and the operator-facing text all go together — an
    option left behind is one somebody sets expecting it to do something.
    """
    assert "vhost_port" not in CONFIG["options"]
    assert "vhost_port" not in CONFIG["schema"]

    docs = (APP / "DOCS.md").read_text()
    assert "vhost_port" not in docs, "the docs still offer a setting that is gone"

    translations = yaml.safe_load((APP / "translations" / "en.yaml").read_text())
    assert "vhost_port" not in translations["configuration"]


def test_every_option_is_explained_to_the_operator():
    """An option nobody can interpret is an option nobody will set correctly."""
    translations = yaml.safe_load((APP / "translations" / "en.yaml").read_text())
    assert set(translations["configuration"]) == set(CONFIG["options"])
    for key, entry in translations["configuration"].items():
        assert entry.get("name"), key
        assert entry.get("description"), key


def test_the_run_script_execs_the_venue_rather_than_backgrounding_it():
    """s6 supervises PID 1 of the service; a backgrounded process is unsupervised.

    ``-m clearvenue``, not ``-m hosted`` (DP92): a venue is the supervisor *plus* the
    surfaces that make it the building's source of truth — the till, enrolment, the
    replication lane. Running the supervisor alone is what left an operator here with the
    Screens page and nothing else, so the module named is the whole of that fix.
    """
    run = (APP / "rootfs/etc/services.d/clearvenue/run").read_text()
    assert re.search(r"^exec .*-m clearvenue$", run, re.M), run[-200:]


def test_the_run_script_says_which_platform_is_hosting_the_venue():
    """Left unset, ``clearvenue.hosts`` resolves Ubuntu Core — the wrong answer here.

    That default is deliberate upstream (it is the platform the venue snap ships on), and
    it is exactly why this file has to state its own: an unstated venue on Home Assistant
    would mount a sign-in of its own over an operator the Supervisor has already
    authenticated, and try to bind a privileged port it does not own.
    """
    run = (APP / "rootfs/etc/services.d/clearvenue/run").read_text()
    assert re.search(r"^CLEARVENUE_HOST=home-assistant$", run, re.M), run[:400]
    assert "export CLEARVENUE_HOST" in run, "set but never exported reaches no child"


def test_the_finish_script_brings_the_whole_app_down():
    """Otherwise s6 restarts one service and the app looks healthy with no screens."""
    finish = (APP / "rootfs/etc/services.d/clearvenue/finish").read_text()
    assert "/run/s6/basedir/bin/halt" in finish


def test_the_finish_script_calls_nothing_s6_overlay_v3_does_not_ship():
    """`s6-test` is a v2 binary: it moved into execline as `eltest`, and the base image
    ships only the latter. Calling it crash-looped the container with "unable to spawn
    s6-test" and no way for the app to ever come down cleanly.

    `/var/run/s6/services` is the same mistake in path form — v3's scandir is
    `/run/service` — so both are checked here rather than rediscovered on a screen.
    """
    finish = _service_script("finish", uncommented=True)
    assert "s6-test" not in finish
    assert "/var/run/s6/services" not in finish


def test_the_dockerfile_installs_no_display_stack():
    """A hosted instance has no screen to drive (DP63); the browser is the display.

    Installing X, feh, mpv or chromium here would be shipping an appliance's display
    stack into an image that can never use it.
    """
    directives = _dockerfile_directives().lower()
    for absent in ("xserver", "xorg", "feh", "imv", "mpv", "chromium", "cage", "plymouth"):
        assert absent not in directives, absent


def test_the_dockerfile_installs_what_mdns_needs():
    """avahi-publish claims each screen's name through the host's daemon (DP66)."""
    directives = _dockerfile_directives()
    assert "avahi-utils" in directives
    # ...and not a second daemon, which would contend with HA OS's for 5353 and lose.
    assert "avahi-daemon" not in directives


def test_the_dockerfile_installs_every_command_the_run_script_calls():
    """The run script's default path reads the host address from `ip`, and neither
    Debian's rootfs nor the Home Assistant base image ships it.

    Its absence was invisible in the worst way: `ip` exited 127, `set -o pipefail` made
    the assignment fail, `set -e` exited the service, and the redirect to /dev/null meant
    the container died having logged nothing at all.
    """
    directives = _dockerfile_directives()
    run = _service_script("run", uncommented=True)
    if re.search(r"\bip route\b", run):
        assert "iproute2" in directives


def test_the_app_dir_points_at_the_package_uvicorn_is_told_to_import():
    """The supervisor spawns each screen with ``cwd=CLEARSIGNAGE_APP_DIR`` and an
    environment it builds from scratch, so nothing on this image's PYTHONPATH reaches a
    screen — that working directory is the only thing that can make ``app.main:app``
    importable, exactly as the appliance's signage-api.service cds to SIGNAGE_APP_DIR.

    Pointing it at the copy root instead of the directory holding the package left every
    screen dying with ModuleNotFoundError, which reaches the operator as a 502 from
    ingress and says nothing about why.
    """
    directives = _dockerfile_directives()
    copied_to = re.search(r"^COPY\s+src/\s+(\S+)", directives, re.M)
    assert copied_to, directives
    app_dir = re.search(r"CLEARSIGNAGE_APP_DIR=(\S+)", directives)
    assert app_dir, directives

    # fetch-source.sh is what guarantees the shape of the tree that gets copied: it
    # refuses to finish unless device/app/main.py is there.
    fetch = (REPO / "scripts/fetch-source.sh").read_text()
    assert "device/app/main.py" in fetch, fetch

    root = copied_to.group(1).rstrip("/")
    assert app_dir.group(1).rstrip("/") == f"{root}/device"


def test_the_run_script_agrees_with_the_dockerfile_about_where_the_app_is():
    """Two defaults for one path is one of them being wrong later."""
    from_dockerfile = re.search(r"CLEARSIGNAGE_APP_DIR=(\S+)", _dockerfile_directives())
    assert from_dockerfile
    fallback = re.search(
        r"CLEARSIGNAGE_APP_DIR=\"\$\{CLEARSIGNAGE_APP_DIR:-([^}]+)\}\"",
        _service_script("run", uncommented=True),
    )
    assert fallback
    assert fallback.group(1).rstrip("/") == from_dockerfile.group(1).rstrip("/")


def test_nothing_in_the_run_script_can_kill_the_service_before_it_explains_itself():
    """`set -e` plus a bare command substitution is a silent exit — a container that
    started and vanished, having logged nothing an operator can act on.

    This used to assert the one `ip route` line carried `|| true`. **Epic 149 deleted that
    line**: working out this box's own address moved into `clearvenue/own_address.py`, where
    it prefers a local address over a VPN's and — being Python rather than a shell seam —
    has tests. So the assertion is the general one it should always have been: every command
    substitution here either cannot fail the script or is a `bashio::config` read, which
    answers for an option the manifest declares rather than running anything.
    """
    run = _service_script("run", uncommented=True)
    assert "ip route" not in run, "the address guess is Python's now, and tested there"

    for line in re.findall(r"^.*\$\(.*$", run, re.M):
        assert "|| true" in line or "bashio::config" in line, (
            f"this can exit the service with nothing in the log: {line.strip()}"
        )


def test_an_unset_host_ip_is_not_advertised_to_peers_as_the_string_null():
    """bashio::config returns the literal "null" for a cleared option, and every screen
    would announce that verbatim as the address peers should reach this host at."""
    run = _service_script("run", uncommented=True)
    assert '"null"' in run or "'null'" in run


def test_the_dependency_set_is_pinned_to_the_appliance_s_own_constraints():
    """"It worked on the Pi" only means something if both install the same artifacts."""
    directives = _dockerfile_directives()
    assert "device/requirements.txt" in directives
    assert "-c /opt/clearsignage/device/constraints.txt" in directives


def test_the_source_is_fetched_not_vendored():
    """DP34's line, applied here: another product's source does not live in this repo."""
    assert not (APP / "src").exists() or not (APP / "src" / "device" / ".git").exists()
    fetch = (REPO / "scripts" / "fetch-source.sh").read_text()
    for path in ("hosted", "device", "shared"):
        assert path in fetch
    # The appliance-only trees are deliberately not copied.
    for absent in ("android", "cloud-provisioner", "infra"):
        assert f'"{absent}"' not in fetch


def test_the_pythonpath_matches_where_the_source_is_copied():
    """The one line that decides whether either half of the app can import at all."""
    directives = _dockerfile_directives()
    assert "COPY src/ /opt/clearsignage/" in directives
    for entry in ("/opt/clearsignage", "/opt/clearsignage/device", "/opt/clearsignage/shared"):
        assert entry in directives


def test_the_image_is_a_prebuilt_multi_arch_manifest():
    """Local builds would need private-repo credentials on every customer's machine.

    And the `{arch}` placeholder is the deprecated form — a manifest list lets the
    Supervisor pull the right layer itself, so a per-arch image name here would be
    fighting the platform.
    """
    assert CONFIG["image"].startswith("ghcr.io/")
    assert CONFIG["image"] == CONFIG["image"].lower(), (
        "OCI image repository names must be lowercase"
    )
    assert "{arch}" not in CONFIG["image"]


def test_the_entry_builds_both_architectures_into_one_manifest():
    """A manifest naming one architecture installs on half the fleet and nobody notices
    until the other half tries."""
    assert "linux/arm64" in ENTRY
    assert "linux/amd64" in ENTRY
    assert "imagetools create" in ENTRY
    # The image pushed must be the one the manifest tells HA to pull: channels.py's.
    assert 'source "${HERE}/scripts/channel-env.sh"' in ENTRY
    assert '"${IMAGE}:${APP_VERSION}"' in ENTRY


def test_the_entry_keeps_only_the_current_and_previous_image_releases():
    assert '"${HERE}/scripts/prune-ghcr-releases.py"' in ENTRY
    assert '--current "${APP_VERSION}" --apply' in ENTRY

    pruner = _load_pruner()
    versions = []
    version_id = 1
    for release in ("0.1.87", "0.1.88", "0.1.89"):
        for suffix in ("", "-aarch64", "-amd64"):
            tags = [f"{release}{suffix}"]
            if release == "0.1.89" and not suffix:
                tags.append("latest")
            versions.append(
                {"id": version_id, "metadata": {"container": {"tags": tags}}}
            )
            version_id += 1
    # An untagged platform manifest may still be referenced by a retained index.
    versions.append({"id": 10, "metadata": {"container": {"tags": []}}})

    assert pruner.versions_to_delete(versions, "0.1.89") == [1, 2, 3]


def test_the_entry_does_not_leave_private_source_on_the_agent():
    """The fetched tree is a full ClearSignage checkout; driven in test_release_addon.py."""
    assert 'rm -rf "${HERE}/${ADDON_DIR}/src" "${HERE}/.upstream" "${HERE}/venue-release"' in ENTRY
    assert "trap cleanup EXIT" in ENTRY
    assert '"${DOCKER}" logout' in ENTRY


def test_the_operator_is_told_to_add_registry_credentials_first():
    """A private image makes this a prerequisite, not a footnote.

    Without the credential the install fails at the pull with an authentication error,
    which reads like a broken repository — an operator would go back and re-check the URL
    they just added. The instruction has to be there, and it has to come first.
    """
    docs = (APP / "DOCS.md").read_text()
    registry = CONFIG["image"].split("/")[0]
    assert registry in docs
    assert docs.index(registry) < docs.index("Repositories")


def test_the_image_contains_the_package_the_run_script_execs():
    """The two files that must agree, and the way they can silently stop agreeing.

    ``fetch-source.sh`` decides what goes into the image; ``run`` decides what is started
    from it. Nothing else connects them, so a package dropped from the copy list — or a
    module renamed upstream — produces an image that builds cleanly, passes every other
    test here, and then exits with ``No module named clearvenue`` on somebody's Home
    Assistant.

    Asserted both ways round: the package is copied, *and* the fetch script checks it
    arrived. The second is what turns an upstream rename into a failed build rather than
    a published image.
    """
    fetch = (REPO / "scripts" / "fetch-source.sh").read_text(encoding="utf-8")
    run = (APP / "rootfs/etc/services.d/clearvenue/run").read_text(encoding="utf-8")

    execed = re.search(r"^exec .*-m (\w+)$", run, re.M)
    assert execed, "the run script execs no module at all"
    package = execed.group(1)

    copied = re.search(r"^for path in ([^;]+); do$", fetch, re.M)
    assert copied, "fetch-source.sh no longer states which paths it copies"
    assert package in copied.group(1).split(), (
        f"the run script starts {package!r}, which fetch-source.sh does not copy: "
        f"the image would build without it"
    )
    assert f'"${{DEST}}/{package}/__main__.py"' in fetch, (
        f"{package} is copied but never verified, so an upstream rename would publish "
        f"an image that cannot start"
    )


def test_the_image_carries_the_service_the_venue_deploys_to_its_hosting():
    """A venue builds Event Share's release from its own source when *Deploy* is pressed.

    Nothing in this image runs PHP, so nothing here would notice the source missing: the
    image built, started and served every page, and the first *Deploy* on somebody's Home
    Assistant ended in a 500 reading ``event_share/app/VERSION``. Copied, and checked on
    arrival by the two files the venue reads first.
    """
    fetch = (REPO / "scripts" / "fetch-source.sh").read_text(encoding="utf-8")
    copied = re.search(r"^for path in ([^;]+); do$", fetch, re.M)
    assert copied, "fetch-source.sh no longer states which paths it copies"
    assert "event_share" in copied.group(1).split(), "the venue could not build its release"
    for required in ("event_share/app/VERSION", "event_share/install/install.php"):
        assert f'"${{DEST}}/{required}"' in fetch, f"{required} is copied but never verified"


# ── The screen release a venue hands to the screens that joined it (ClearSignage DP210) ──

def test_the_screen_release_lands_where_the_venue_is_told_to_look():
    """Three files must agree, and nothing else connects them.

    The entry places the run's signed release under ``src/``, the Dockerfile's COPY carries
    ``src/`` to ``/opt/clearsignage/``, and the ENV tells the venue where that put it. A
    release placed one folder off builds, publishes and is offered to nobody.
    """
    assert 'mkdir -p "${HERE}/${ADDON_DIR}/src/screen-release"' in ENTRY
    directives = _dockerfile_directives()
    assert "COPY src/ /opt/clearsignage/" in directives
    assert "CLEARVENUE_SCREEN_RELEASE_DIR=/opt/clearsignage/screen-release" in directives


def test_the_screen_release_is_the_release_workflow_s_and_checked_by_the_commit_shipped():
    """Signed once, by ClearSignage's release workflow, never here.

    And checked against the keyring the commit being shipped bakes into screens before it is
    placed: the last point where a release that would not verify is still this build's
    problem. Driven in test_release_addon.py.
    """
    assert '"${HERE}/.upstream/clearsignage"' in ENTRY
    assert "device/app/update_signing_public.json" in ENTRY
    assert "verify_manifest_signature" in ENTRY
    assert not list(REPO.rglob("sign_update_manifest.py")), "a signer was copied into this repo"
    assert not (REPO / "scripts" / "build-screen-release.sh").exists(), "a second release build"


def test_a_public_url_reaches_the_venue_only_when_the_operator_set_one():
    """An empty or cleared option must not reach the venue as a public address of "null"."""
    run = _service_script("run", uncommented=True)
    assert "bashio::config 'public_url'" in run
    assert 'if [ "${CLEARVENUE_PUBLIC_URL}" = "null" ]' in run
    assert "unset CLEARVENUE_PUBLIC_URL" in run
    assert CONFIG["options"]["public_url"] == ""
    assert CONFIG["schema"]["public_url"] == "str?"


def test_the_operator_is_told_how_to_reach_the_venue_from_outside():
    docs = (APP / "DOCS.md").read_text()
    assert "Reaching ClearVenue from outside the building" in docs
    assert "port `8098`" in docs and "`public_url`" in docs
    assert "Nginx Proxy Manager" in docs and "Only port `8098`" in docs
