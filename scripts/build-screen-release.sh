#!/usr/bin/env bash
# Build the signed screen release this venue hands to the screens that joined it.
#
# A screen joined to a venue takes its software from the venue rather
# than from a download host, so updating this add-on is how a venue's wall screens are
# updated. The venue does not build or sign anything at runtime — it serves the two files
# it was shipped with, and the screen checks them against the key baked into its own build.
# So the image has to carry a release that is:
#
#   * built from the exact commit the image runs (CLEARSIGNAGE_REF, from fetch-source.sh),
#     so a venue and the screens it updates are the same code;
#   * versioned as the add-on is (SCREEN_RELEASE_VERSION, the YYYYMMDD.NN the pipeline
#     chose), so every new add-on is a newer release to a screen;
#   * signed with the same key, by the same signer, as ClearSignage's own release job, and
#     checked against the keyring that commit ships to screens before it goes anywhere.
#
# Built with ClearSignage's own packaging scripts from that commit — never a copy of them
# here — for the reason fetch-source.sh gives about its source: this repository packages
# the runtime, it does not fork it.
#
# Output: <addon>/src/screen-release/{manifest-<channel>.json, clearsignage-<version>.tar.gz},
# which the Dockerfile's `COPY src/ /opt/clearsignage/` carries to the directory
# CLEARVENUE_SCREEN_RELEASE_DIR names.
#
# Environment:
#   CHANNEL                      stable | beta | dev (channel-env.sh)
#   SCREEN_RELEASE_VERSION       required; the add-on version
#   CLEARSIGNAGE_REF             the commit to build; default: what fetch-source.sh recorded
#   UPDATE_SIGNING_PRIVATE_KEY   the release signing key (PEM)
#   UPDATE_SIGNING_KEY_ID        its id in the keyring (default release-2026-08)
#   SCREEN_RELEASE_REQUIRED      true: a missing key fails the build (a published image)
#   PYTHON                       an interpreter with cryptography and pydantic (default python3)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "${HERE}/scripts/channel-env.sh"

VERSION="${SCREEN_RELEASE_VERSION:-}"
if [[ ! "${VERSION}" =~ ^[0-9]{8}\.[0-9]{2}$ ]]; then
    echo "SCREEN_RELEASE_VERSION must be the add-on version, YYYYMMDD.NN (got '${VERSION}')" >&2
    exit 2
fi

SRC="${HERE}/${ADDON_DIR}/src"
DEST="${SRC}/screen-release"
rm -rf "${DEST}"

# Asked first, before anything is fetched: a build with no key has nothing to sign, and a
# published image that silently carried no release would leave every joined screen on the
# version it has without anybody being told why.
if [ -z "${UPDATE_SIGNING_PRIVATE_KEY:-}" ]; then
    if [ "${SCREEN_RELEASE_REQUIRED:-false}" = true ]; then
        echo "No release signing key: a published ClearVenue must carry a signed screen release." >&2
        exit 1
    fi
    echo "WARNING: no release signing key — this image carries NO screen release."
    echo "         Screens that joined this venue will not be offered an update by it."
    exit 0
fi

[ -d "${SRC}" ] || { echo "Run scripts/fetch-source.sh first: ${SRC} is missing" >&2; exit 1; }
REF="${CLEARSIGNAGE_REF:-$(cat "${SRC}/CLEARSIGNAGE_REF")}"
if [[ ! "${REF}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "CLEARSIGNAGE_REF must be the full commit SHA the image is built from (got '${REF}')" >&2
    exit 2
fi
REPO="${CLEARSIGNAGE_REPO:-https://github.com/madeByJansen/clearsignage.git}"
PYTHON="${PYTHON:-python3}"
KEY_ID="${UPDATE_SIGNING_KEY_ID:-release-2026-08}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

echo "Building the screen release ${VERSION} (${CHANNEL}) from ${REF}"
git init --quiet "${WORK}/clearsignage"
git -C "${WORK}/clearsignage" remote add origin "${REPO}"
git -C "${WORK}/clearsignage" fetch --quiet --depth 1 origin "${REF}"
git -C "${WORK}/clearsignage" checkout --quiet FETCH_HEAD
# The image's source and the release must be one commit, or a venue would update its
# screens to code it is not running itself.
test "$(git -C "${WORK}/clearsignage" rev-parse HEAD)" = "${REF}"

UPSTREAM="${WORK}/clearsignage"
TARBALL="clearsignage-${VERSION}.tar.gz"
MANIFEST="manifest-${CHANNEL}.json"
OUT_DIR="${WORK}/dist" "${UPSTREAM}/packaging/build-release.sh" "${VERSION}" HEAD \
    >/dev/null
# build-release.sh archives the tree of whichever repository it runs in; run from here it
# would archive this one. It cds to its own checkout, so it archived ClearSignage — said
# again by the archive's own prefix, and by the screen runtime being inside it. The whole
# listing is read rather than piped into `head`, which closes the pipe early and makes
# `pipefail` report a good archive as a failure.
LISTING="$(tar -tzf "${WORK}/dist/${TARBALL}")"
if [ "${LISTING%%$'\n'*}" != "clearsignage-${VERSION}/" ] \
    || ! grep -qx "clearsignage-${VERSION}/device/app/main.py" <<<"${LISTING}"; then
    echo "The screen release is not a ClearSignage ${VERSION} archive" >&2
    exit 1
fi

# The same package URL ClearSignage's release job signs, so a manifest from either lane
# reads the same. A venue only ever uses its basename, to find the package beside it.
"${PYTHON}" "${UPSTREAM}/packaging/sign_update_manifest.py" \
    "${WORK}/dist/${TARBALL}" \
    --version "${VERSION}" \
    --channel "${CHANNEL}" \
    --key-id "${KEY_ID}" \
    --package-url "https://download.clearsignage.app/releases/${TARBALL}" \
    --out "${WORK}/dist/${MANIFEST}"

# The last point where a broken release is still ours rather than every screen's: the
# manifest must verify against the keyring this commit bakes into screens, and name the
# package's real checksum.
"${PYTHON}" - "${UPSTREAM}" "${WORK}/dist/${MANIFEST}" "${WORK}/dist/${TARBALL}" \
    "${VERSION}" "${CHANNEL}" <<'PY'
import json
import sys
from pathlib import Path

upstream, manifest_path, tarball, version, channel = sys.argv[1:]
sys.path.insert(0, str(Path(upstream) / "shared"))
from signage_shared.checksums import sha256_file
from signage_shared.updates import UpdateManifest, verify_manifest_signature

keyring = json.loads((Path(upstream) / "device/app/update_signing_public.json").read_text())
manifest = UpdateManifest.model_validate_json(Path(manifest_path).read_text())
problems = []
if not verify_manifest_signature(manifest, keyring):
    problems.append("it does not verify against the keyring screens are built with")
if manifest.sha256 != sha256_file(Path(tarball)):
    problems.append("its checksum is not the package's")
if (manifest.version, manifest.channel) != (version, channel):
    problems.append(f"it names {manifest.version} ({manifest.channel})")
if problems:
    sys.exit("The signed screen release is unusable: " + "; ".join(problems))
PY

mkdir -p "${DEST}"
cp "${WORK}/dist/${TARBALL}" "${WORK}/dist/${MANIFEST}" "${DEST}/"
echo "Screen release ${VERSION} (${CHANNEL}) signed and placed in ${DEST}"
