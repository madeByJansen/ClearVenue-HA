#!/usr/bin/env bash
# Build the ClearVenue release bundle, and build this image from it.
#
# One bundle per release, `clearvenue-<version>.tar.gz`, made by ClearSignage's own
# `scripts/build_venue_bundle.py` from the commit fetch-source.sh checked out. The image is
# built from that bundle's contents, and the same bundle is published beside the image for
# venues that are not run by Home Assistant — so one version number is the same code
# wherever it is installed.
#
# The bundle's manifest is signed with the release key by ClearSignage's own
# `packaging/sign_venue_release.py`, which checks the signature against the keyring that
# commit ships before it writes anything.
#
# Output:
#   <addon>/src/                                  replaced by the bundle's contents
#   venue-release/clearvenue-<version>.tar.gz     the bundle, for push-venue-bundle.py
#   venue-release/clearvenue-<channel>.json       its signed manifest
#
# A commit from before the bundle existed has no builder: the image is then built from the
# source fetch-source.sh copied, and no bundle is published for it.
#
# Environment:
#   CHANNEL                      stable | beta | dev (channel-env.sh)
#   BUNDLE_VERSION               required; the app version, YYYYMMDD.NN
#   UPDATE_SIGNING_PRIVATE_KEY   the release signing key (PEM)
#   UPDATE_SIGNING_KEY_ID        its id in the keyring (default release-2026-08)
#   BUNDLE_REQUIRED              true: a missing key fails the build (a published image)
#   PYTHON                       an interpreter with cryptography and pydantic (default python3)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "${HERE}/scripts/channel-env.sh"

VERSION="${BUNDLE_VERSION:-}"
if [[ ! "${VERSION}" =~ ^[0-9]{8}\.[0-9]{2}$ ]]; then
    echo "BUNDLE_VERSION must be the app version, YYYYMMDD.NN (got '${VERSION}')" >&2
    exit 2
fi

SRC="${HERE}/${ADDON_DIR}/src"
UPSTREAM="${HERE}/.upstream/clearsignage"
OUT="${HERE}/venue-release"
PYTHON="${PYTHON:-python3}"
KEY_ID="${UPDATE_SIGNING_KEY_ID:-release-2026-08}"
rm -rf "${OUT}"

[ -f "${SRC}/CLEARSIGNAGE_REF" ] || { echo "Run scripts/fetch-source.sh first: ${SRC} is missing" >&2; exit 1; }
REF="$(cat "${SRC}/CLEARSIGNAGE_REF")"
if [ ! -d "${UPSTREAM}/.git" ] || [ "$(git -C "${UPSTREAM}" rev-parse HEAD)" != "${REF}" ]; then
    echo "The checkout fetch-source.sh kept is missing or is not ${REF}; run it again." >&2
    exit 1
fi

if [ ! -f "${UPSTREAM}/scripts/build_venue_bundle.py" ]; then
    echo "NOTICE: ${REF} predates the ClearVenue bundle; the image is built from its source"
    echo "        and no bundle is published for this release."
    exit 0
fi

if [ -z "${UPDATE_SIGNING_PRIVATE_KEY:-}" ] && [ "${BUNDLE_REQUIRED:-false}" = true ]; then
    echo "No release signing key: a published ClearVenue bundle must be signed." >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

screen_release=()
if [ -d "${SRC}/screen-release" ]; then
    screen_release=(--screen-release "${SRC}/screen-release")
fi
BUNDLE="$("${PYTHON}" "${UPSTREAM}/scripts/build_venue_bundle.py" "${WORK}/dist" \
    --version "${VERSION}" --revision "${REF}" ${screen_release[@]+"${screen_release[@]}"})"
NAME="clearvenue-${VERSION}.tar.gz"
[ "$(basename "${BUNDLE}")" = "${NAME}" ] || { echo "The builder made ${BUNDLE}, not ${NAME}" >&2; exit 1; }

# The image is built from exactly what the bundle holds, so what a venue installs from the
# bundle and what this image runs cannot differ.
mkdir -p "${WORK}/unpacked"
tar -xzf "${BUNDLE}" -C "${WORK}/unpacked"
[ -f "${WORK}/unpacked/clearvenue-${VERSION}/RELEASE.json" ] \
    || { echo "${NAME} is not laid out as a ClearVenue bundle" >&2; exit 1; }
rm -rf "${SRC}"
mv "${WORK}/unpacked/clearvenue-${VERSION}" "${SRC}"
printf '%s\n' "${REF}" > "${SRC}/CLEARSIGNAGE_REF"

mkdir -p "${OUT}"
cp "${BUNDLE}" "${OUT}/${NAME}"
if [ -z "${UPDATE_SIGNING_PRIVATE_KEY:-}" ]; then
    echo "WARNING: no release signing key — this dry run's bundle is not signed."
    exit 0
fi
"${PYTHON}" "${UPSTREAM}/packaging/sign_venue_release.py" "${OUT}/${NAME}" \
    --channel "${CHANNEL}" --key-id "${KEY_ID}" --out "${OUT}/clearvenue-${CHANNEL}.json"
echo "ClearVenue ${VERSION} (${CHANNEL}) bundled, signed, and placed in ${SRC}"
