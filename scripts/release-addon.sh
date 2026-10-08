#!/usr/bin/env bash
# Build and publish the Home Assistant add-on for one ClearSignage release.
#
# The one entry ClearSignage's release workflow calls when a run builds the add-on, whichever
# runner it is on. This repository keeps
# the add-on's packaging; the release workflow decides *what* is released and hands it here:
# the source checkout it gated and built, and the screen release it signed. Nothing is built
# twice and no number is chosen twice, so the add-on, the screen release inside it and the
# venue bundle all say the version of the commit they carry.
#
#   validate     this repository's own tests, and the channel folders in step
#   source       fetch-source.sh, from the release workflow's own checkout at its commit
#   screens      the run's signed screen release, checked, into <addon>/src/screen-release
#   version      stable and beta: the release's number, or a third part when the same
#                release is built again; dev: the run's number (the dev counter)
#   bundle       build-venue-bundle.sh: the bundle, and the image's context from it
#   images       both architectures, then one multi-architecture tag and `latest`
#   publish      the bundle (once per release), the version recorded on main, old pruned
#
# A dry run builds everything and publishes nothing: the images go to the build cache, the
# bundle is kept in venue-release/, and main and the registry are left alone.
#
# Environment:
#   CHANNEL                      stable | beta | dev
#   CLEARSIGNAGE_SOURCE          the ClearSignage checkout the release was built from
#   CLEARSIGNAGE_REF             its full commit SHA
#   RELEASE_VERSION              the release's number, YYYYMMDD.NN (dev: the dev counter's)
#   SCREEN_RELEASE_DIR           holds manifest-<channel>.json and clearsignage-<version>.tar.gz
#   UPDATE_SIGNING_PRIVATE_KEY   the release signing key, for the bundle's manifest
#   GHCR_USERNAME, GHCR_TOKEN    a GHCR login with write:packages
#   GIT_ASKPASS                  answers for this repository's remote (recording, pruning)
#   DRY_RUN                      true: build only
#   PYTHON                       an interpreter with pytest, pyyaml, cryptography, pydantic
#   DOCKER                       the docker command (default docker)
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "${HERE}"
PYTHON="${PYTHON:-python3}"
DOCKER="${DOCKER:-docker}"
DRY_RUN="${DRY_RUN:-false}"
REGISTRY="ghcr.io"

say() { printf '%s\n' "$*"; }
refuse() { printf '%s\n' "$*" >&2; exit 2; }

# ── what the release workflow handed over ────────────────────────────────────────
source "${HERE}/scripts/channel-env.sh" || refuse "CHANNEL must be stable, beta or dev."
VERSION="${RELEASE_VERSION:-}"
[[ "${VERSION}" =~ ^[0-9]{8}\.[0-9]{2}$ ]] \
    || refuse "RELEASE_VERSION must be the release's number, YYYYMMDD.NN (got '${VERSION}')."
[[ "${CLEARSIGNAGE_REF:-}" =~ ^[0-9a-f]{40}$ ]] \
    || refuse "CLEARSIGNAGE_REF must be the full commit SHA the release was built from."
[ -d "${CLEARSIGNAGE_SOURCE:-}/.git" ] \
    || refuse "CLEARSIGNAGE_SOURCE must be the ClearSignage checkout the release was built from."
SCREENS="${SCREEN_RELEASE_DIR:-}"
SIGNED="${SCREENS}/manifest-${CHANNEL}.json"
PACKAGE_FILE="${SCREENS}/clearsignage-${VERSION}.tar.gz"
for needed in "${SIGNED}" "${PACKAGE_FILE}"; do
    [ -f "${needed}" ] || refuse "The release has no $(basename "${needed}") in '${SCREENS}': sign it first."
done
if [ "${DRY_RUN}" != true ]; then
    [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_TOKEN:-}" ] \
        || refuse "GHCR_USERNAME and GHCR_TOKEN are needed to publish the add-on."
    [ -n "${UPDATE_SIGNING_PRIVATE_KEY:-}" ] \
        || refuse "No release signing key: a published ClearVenue bundle must be signed."
fi

LOGGED_IN=false
cleanup() {
    rm -rf "${HERE}/${ADDON_DIR}/src" "${HERE}/.upstream" "${HERE}/venue-release"
    if [ "${LOGGED_IN}" = true ]; then
        "${DOCKER}" logout "${REGISTRY}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ── validate ─────────────────────────────────────────────────────────────────────
say "Validating the add-on's packaging"
PYTHON="${PYTHON}" "${HERE}/scripts/validate-packaging.sh"

# ── source ───────────────────────────────────────────────────────────────────────
CLEARSIGNAGE_REPO="${CLEARSIGNAGE_SOURCE}" CLEARSIGNAGE_REF_OVERRIDE="${CLEARSIGNAGE_REF}" \
    "${HERE}/scripts/fetch-source.sh"
[ "$(cat "${HERE}/${ADDON_DIR}/src/CLEARSIGNAGE_REF")" = "${CLEARSIGNAGE_REF}" ] \
    || refuse "The fetched source is not ${CLEARSIGNAGE_REF}."

# ── screens ──────────────────────────────────────────────────────────────────────
# The last point where a broken release is still ours rather than every screen's: the
# manifest must verify against the keyring this commit bakes into screens, and name the
# package's real checksum, version and channel.
"${PYTHON}" - "${HERE}/.upstream/clearsignage" "${SIGNED}" "${PACKAGE_FILE}" \
    "${VERSION}" "${CHANNEL}" <<'PY'
import json
import sys
from pathlib import Path

upstream, manifest_path, package, version, channel = sys.argv[1:]
sys.path.insert(0, str(Path(upstream) / "shared"))
from signage_shared.checksums import sha256_file
from signage_shared.updates import UpdateManifest, verify_manifest_signature

keyring = json.loads((Path(upstream) / "device/app/update_signing_public.json").read_text())
manifest = UpdateManifest.model_validate_json(Path(manifest_path).read_text())
problems = []
if not verify_manifest_signature(manifest, keyring):
    problems.append("it does not verify against the keyring screens are built with")
if manifest.sha256 != sha256_file(Path(package)):
    problems.append("its checksum is not the package's")
if (manifest.version, manifest.channel) != (version, channel):
    problems.append(f"it names {manifest.version} ({manifest.channel})")
if problems:
    sys.exit("The signed screen release is unusable: " + "; ".join(problems))
PY
mkdir -p "${HERE}/${ADDON_DIR}/src/screen-release"
cp "${SIGNED}" "${PACKAGE_FILE}" "${HERE}/${ADDON_DIR}/src/screen-release/"
say "Screen release ${VERSION} (${CHANNEL}) placed in ${ADDON_DIR}/src/screen-release"

# ── version ──────────────────────────────────────────────────────────────────────
# Dev's number is already its counter (the release workflow asked it); a dry run reads no
# registry. Otherwise the release's own number, or a third part if it was built before.
if [ "${CHANNEL}" = dev ] || [ "${DRY_RUN}" = true ]; then
    APP_VERSION="$("${PYTHON}" "${HERE}/scripts/next-image-version.py" --channel "${CHANNEL}" \
        --set "${VERSION}")"
else
    APP_VERSION="$(GHCR_TOKEN="${GHCR_TOKEN}" "${PYTHON}" "${HERE}/scripts/next-image-version.py" \
        --channel "${CHANNEL}" --release "${VERSION}")"
fi
say "ClearVenue ${CHANNEL} ${APP_VERSION}, carrying ClearSignage ${VERSION} (${CLEARSIGNAGE_REF})"

# ── bundle ───────────────────────────────────────────────────────────────────────
BUNDLE_VERSION="${VERSION}" BUNDLE_REQUIRED="$([ "${DRY_RUN}" = true ] && echo false || echo true)" \
    PYTHON="${PYTHON}" "${HERE}/scripts/build-venue-bundle.sh"

# ── images ───────────────────────────────────────────────────────────────────────
build_from() {
    "${PYTHON}" -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["build_from"][sys.argv[2]])' \
        "${HERE}/${ADDON_DIR}/build.yaml" "$1"
}
"${DOCKER}" run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
if "${DOCKER}" buildx inspect clearvenue >/dev/null 2>&1; then
    "${DOCKER}" buildx use clearvenue
else
    "${DOCKER}" buildx create --name clearvenue --driver docker-container --use --bootstrap
fi
if [ "${DRY_RUN}" = true ]; then
    output_mode=(--output=type=cacheonly)
else
    printf '%s' "${GHCR_TOKEN}" | "${DOCKER}" login "${REGISTRY}" --username "${GHCR_USERNAME}" --password-stdin
    LOGGED_IN=true
    output_mode=(--push)
fi
build_one() {
    "${DOCKER}" buildx build --builder clearvenue \
        --platform "$1" --build-arg "BUILD_FROM=$2" \
        --build-arg "CLEARSIGNAGE_REF=${CLEARSIGNAGE_REF}" \
        --label "org.opencontainers.image.revision=${CLEARSIGNAGE_REF}" \
        --label "org.opencontainers.image.version=${APP_VERSION}" \
        --tag "${IMAGE}:${APP_VERSION}-$3" \
        "${output_mode[@]}" "${HERE}/${ADDON_DIR}"
}
build_one linux/arm64 "$(build_from aarch64)" aarch64
build_one linux/amd64 "$(build_from amd64)" amd64

if [ "${DRY_RUN}" = true ]; then
    say "Dry run: ClearVenue ${CHANNEL} ${APP_VERSION} built; nothing was published."
    exit 0
fi

# ── publish ──────────────────────────────────────────────────────────────────────
"${DOCKER}" buildx imagetools create \
    --tag "${IMAGE}:${APP_VERSION}" --tag "${IMAGE}:latest" \
    "${IMAGE}:${APP_VERSION}-aarch64" "${IMAGE}:${APP_VERSION}-amd64"
"${DOCKER}" buildx imagetools inspect "${IMAGE}:${APP_VERSION}"
# Once per release: built again, the add-on carries the same commit, whose bundle the
# package already holds under the release's own number.
if [ "${APP_VERSION}" = "${VERSION}" ]; then
    GHCR_USERNAME="${GHCR_USERNAME}" GHCR_TOKEN="${GHCR_TOKEN}" \
        "${PYTHON}" "${HERE}/scripts/push-venue-bundle.py" --channel "${CHANNEL}" --version "${VERSION}"
else
    say "ClearVenue bundle ${VERSION} is already published; this rebuild publishes the add-on only."
fi
# Recorded after both are published: until then nothing offers the version.
RECORD_VERSION="${APP_VERSION}" RECORD_BRANCH=main RECORD_PYTHON="${PYTHON}" \
    "${HERE}/scripts/record-published-version.sh"
GHCR_TOKEN="${GHCR_TOKEN}" "${PYTHON}" "${HERE}/scripts/prune-ghcr-releases.py" \
    --channel "${CHANNEL}" --current "${APP_VERSION}" --apply
say "Published ClearVenue ${CHANNEL} ${APP_VERSION}."
