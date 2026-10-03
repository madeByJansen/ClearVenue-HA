#!/usr/bin/env bash
# Build the app image locally for one architecture.
#
# The same two steps CI runs: pin the source, then build. Kept as a script rather than a
# README instruction because the build is worthless if the source step is skipped — the
# Dockerfile would COPY a stale src/ and nobody would notice until the image ran.
set -euo pipefail

ARCH="${1:-aarch64}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "${HERE}/scripts/channel-env.sh"

BUILD_FROM="$(
    python3 -c "import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))['build_from'][sys.argv[2]])" \
        "${HERE}/${ADDON_DIR}/build.yaml" "${ARCH}"
)"

"${HERE}/scripts/fetch-source.sh"

docker build \
    --build-arg "BUILD_FROM=${BUILD_FROM}" \
    --build-arg "CLEARSIGNAGE_REF=$(cat "${HERE}/${ADDON_DIR}/src/CLEARSIGNAGE_REF")" \
    --tag "${PACKAGE}:local-${ARCH}" \
    "${HERE}/${ADDON_DIR}"
