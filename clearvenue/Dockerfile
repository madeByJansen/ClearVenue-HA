# syntax=docker/dockerfile:1
ARG BUILD_FROM
FROM ${BUILD_FROM}

# The ClearSignage source is fetched into the build context before the build, never
# vendored into this repository (no other product's *source*
# lives in a repo that is not its own). scripts/fetch-source.sh does the fetch and pins
# the ref; CI runs it, and so does a developer building locally.
ARG CLEARSIGNAGE_REF=main

# CLEARSIGNAGE_APP_DIR is the directory that *contains* the `app` package, not the root
# this repo copies the source to. The appliance's own signage-api.service says the same
# thing — `SIGNAGE_APP_DIR=/media/signage/app`, then `cd "$SIGNAGE_APP_DIR"` and
# `uvicorn app.main:app` — and the supervisor reuses that entry point verbatim so a
# hosted screen starts the way an appliance does.
#
# Pointing it one level up at /opt/clearsignage put `app` out of reach: the supervisor
# spawns each screen with `cwd=app_dir` and an environment it builds from scratch, so
# the PYTHONPATH below never reaches a screen and cwd is the only thing that can make
# `app.main:app` importable. Every screen died with ModuleNotFoundError and each one
# surfaced as a 502 through ingress.
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    CLEARSIGNAGE_APP_DIR=/opt/clearsignage/device

# avahi-utils supplies avahi-publish, which is how each screen's <name>.local is claimed
# through the *host's* daemon over host_dbus. No avahi-daemon here: a second one
# would contend with HA OS's for 5353 and lose. dbus is the client library only.
#
# iproute2 supplies `ip`, which the run script reads the host's address from when the
# operator leaves host_ip blank — the default. Neither Debian's rootfs nor the Home
# Assistant base image ships it, and its absence killed the service before it logged a
# single line: `ip` exited 127, and `set -o pipefail` turned that into a silent exit.
#
# No X, no feh, no mpv, no chromium. A hosted instance has no display to drive — the
# browser framing /display is the screen — so the entire appliance display stack
# is absent rather than installed and unused.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        python3 \
        python3-pip \
        python3-venv \
        avahi-utils \
        dbus \
        iproute2 \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/clearsignage
COPY src/ /opt/clearsignage/

# -c constraints.txt is the point: the hosted image installs the byte-identical
# dependency set an appliance resolves, so "it worked on the Pi" keeps meaning something.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install \
        -r /opt/clearsignage/device/requirements.txt \
        -c /opt/clearsignage/device/constraints.txt \
    && /opt/venv/bin/pip install -e /opt/clearsignage/shared \
        -c /opt/clearsignage/device/constraints.txt

# Where the signed screen release scripts/build-screen-release.sh placed under src/ lands,
# and where the venue serves it from to the screens that joined it.
# Stated rather than left to the venue's default, so a change to where the source is copied
# is a change to this line and not a venue that quietly offers its screens nothing.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/opt/clearsignage:/opt/clearsignage/device:/opt/clearsignage/shared" \
    CLEARVENUE_SCREEN_RELEASE_DIR=/opt/clearsignage/screen-release

COPY rootfs/ /
RUN chmod a+x /etc/services.d/clearvenue/run /etc/services.d/clearvenue/finish

LABEL \
    io.hass.name="ClearVenue" \
    io.hass.description="Run ClearVenue inside Home Assistant" \
    io.hass.type="addon" \
    org.opencontainers.image.source="https://github.com/madeByJansen/clearsignage" \
    org.opencontainers.image.revision="${CLEARSIGNAGE_REF}"
