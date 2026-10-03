#!/usr/bin/env bash
# Fetch the ClearSignage source into the build context (Epic 119 Pass 4).
#
# This repository holds packaging and nothing else. ClearSignage's source is fetched at
# build time rather than vendored, for the reason DP34 already gives about role
# applications: another product's *source* does not enter a repo that is not its own —
# its built artefact does. Keeping that line here means this repo can never quietly fork
# the runtime it is supposed to package.
#
# Only five paths are copied, and the omissions are the point: `hosted/` is the
# supervisor, `device/` is one screen, `shared/` is the single-sourced operator UI, and
# `clearvenue/` is the venue role this add-on runs (DP92) — the till, enrolment, and the
# replication lane that feeds a screen its prices. The appliance's image builder, its
# systemd units, its Android port and the cloud Worker are all absent, because a hosted
# instance is none of those things.
#
# `event_share/` runs nowhere in this image. It is the small PHP service a venue deploys to
# its own web hosting (DP157), and the venue builds that release from this source on the
# press of *Deploy* — so without it every deploy failed on its first read of
# event_share/app/VERSION, as a 500.
#
# `clearvenue/` is the newest of the four and the reason the add-on execs `-m clearvenue`
# rather than `-m hosted`: a venue is a supervisor plus its own surfaces, and which
# platform hosts it is a value inside that package rather than a second implementation.
set -euo pipefail

# Stable uses prod: a published add-on image is a release and reaches customers'
# Home Assistant installs, so the default has to be code that went through the
# release gate. The channel selects its branch; an exact SHA can override it.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "${HERE}/scripts/channel-env.sh"
REF="${CLEARSIGNAGE_REF_OVERRIDE:-${SOURCE_BRANCH}}"
if [ -n "${CLEARSIGNAGE_REF_OVERRIDE:-}" ] && [[ ! "${REF}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "CLEARSIGNAGE_REF_OVERRIDE must be a full 40-character commit SHA" >&2
    exit 2
fi
REPO="${CLEARSIGNAGE_REPO:-https://github.com/madeByJansen/clearsignage.git}"
DEST="${HERE}/${ADDON_DIR}/src"
# The checkout is kept beside the build context rather than in a temporary folder, so the
# venue bundle (build-venue-bundle.sh) is built from this same commit without fetching it
# again. Both pipelines remove it with src/ when they finish.
CHECKOUT="${HERE}/.upstream/clearsignage"
rm -rf "${CHECKOUT}"
mkdir -p "$(dirname "${CHECKOUT}")"

echo "Fetching ${REPO} @ ${REF}"
git clone --quiet --depth 1 --branch "${REF}" "${REPO}" "${CHECKOUT}" 2>/dev/null \
    || {
        # A commit SHA cannot be cloned with --branch; fall back to fetching it directly.
        rm -rf "${CHECKOUT}"
        git init --quiet "${CHECKOUT}"
        git -C "${CHECKOUT}" remote add origin "${REPO}"
        git -C "${CHECKOUT}" fetch --quiet --depth 1 origin "${REF}"
        git -C "${CHECKOUT}" checkout --quiet FETCH_HEAD
    }

rm -rf "${DEST}"
mkdir -p "${DEST}"
for path in hosted device shared clearvenue event_share; do
    cp -a "${CHECKOUT}/${path}" "${DEST}/${path}"
done

# Tests are not shipped into an image an operator runs. They are run in ClearSignage's
# own pipeline, against the same source this pinned.
find "${DEST}" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

RESOLVED="$(git -C "${CHECKOUT}" rev-parse HEAD)"
printf '%s\n' "${RESOLVED}" > "${DEST}/CLEARSIGNAGE_REF"
echo "Fetched ${RESOLVED} into ${DEST}"

# Fail loudly rather than building an image that cannot start. Each of these is something
# the Dockerfile or the runtime reaches for by name, so a rename upstream is caught here
# rather than at 3am on somebody's Home Assistant.
for required in \
    "${DEST}/device/requirements.txt" \
    "${DEST}/device/constraints.txt" \
    "${DEST}/device/app/main.py" \
    "${DEST}/shared/pyproject.toml" \
    "${DEST}/hosted/__main__.py" \
    "${DEST}/clearvenue/__main__.py" \
    "${DEST}/clearvenue/hosts/__init__.py" \
    "${DEST}/event_share/app/VERSION" \
    "${DEST}/event_share/install/install.php"
do
    [ -f "${required}" ] || { echo "missing from source: ${required}" >&2; exit 1; }
done
echo "Source layout verified."
