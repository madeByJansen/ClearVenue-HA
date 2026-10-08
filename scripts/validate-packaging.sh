#!/usr/bin/env bash
# This repository's own checks: its tests, and the channel folders in step with each other.
#
# Run before anything is built for a release (release-addon.sh) and on every pull request
# (.github/workflows/tests.yml), so the packaging a release uses is the packaging that passed.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
"${PYTHON}" -m pytest "${HERE}/tests" -q -p no:cacheprovider
"${PYTHON}" "${HERE}/scripts/sync-channel-packaging.py" --check
