#!/usr/bin/env python3
"""Choose the app version for this build, and write it into the manifest.

`YYYYMMDD.NN` from the build's UTC date, the same scheme ClearSignage releases use — and
chosen the same way, from the artefacts that already exist rather than from a number
somebody remembered to edit. There the store is R2; here it is the GHCR package, whose
tags are the record of every version ever published.

Choosing from the registry is what makes this safe to run on every build. A counter kept
in the repository would be wrong the moment two builds ran from the same commit, or a
publish failed after its version was written down; the tags cannot disagree with
themselves. For the same reason a listing that fails for any reason other than "no tags
for today" aborts rather than falling back to `.01`, which would republish over a version
somebody is already running.

The version is then stamped into the selected channel's `config.yaml`, because that manifest is
where Home Assistant reads it: the Supervisor tracks an installed app by that literal and
pulls `image:<version>`. So the pipeline stamps it before building and commits it after
publishing — the file records what was published, rather than being edited in the hope
that something will be.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import urllib.error
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from channels import CHANNELS, channel_settings
import ghcr_api  # noqa: E402  (the module sits beside this command, not on the path)
import yaml  # noqa: E402

# The per-architecture tags the pipeline pushes beside the multi-architecture manifest, and
# the ClearVenue bundle it publishes in the same package (push-venue-bundle.py). They carry
# the same version, so they are the same release for counting.
ARCH_SUFFIXES = ("-aarch64", "-amd64", "-bundle")

# The manifest's own version line. Anchored to the start of a line so it cannot match
# `ingress_port` or anything else that merely ends in the word.
VERSION_LINE = re.compile(r"^version:.*$", re.MULTILINE)

MAX_COUNTER = 99


def utc_date(now: dt.datetime | None = None) -> dt.date:
    """Return the UTC calendar date, converting an explicitly supplied instant."""
    instant = now or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("the supplied datetime must be timezone-aware")
    return instant.astimezone(dt.timezone.utc).date()


def next_version(tags: Iterable[str], requested_date: dt.date) -> str:
    """Choose the next counter for ``requested_date`` from the tags already published.

    A tag that belongs to the day but does not carry a two-digit counter stops the build
    rather than being skipped: skipping it is how a version gets handed out twice, and the
    second one overwrites an image somebody is running.
    """
    date_text = requested_date.strftime("%Y%m%d")
    counters: list[int] = []
    for tag in tags:
        text = str(tag)
        for suffix in ARCH_SUFFIXES:
            if text.endswith(suffix):
                text = text[: -len(suffix)]
                break
        if not text.startswith(f"{date_text}."):
            continue
        counter = text[len(date_text) + 1 :]
        if len(counter) != 2 or not counter.isascii() or not counter.isdigit():
            raise ValueError(
                f"Malformed tag for {date_text}: {tag!r}; "
                "the counter must be exactly two ASCII digits"
            )
        counters.append(int(counter))

    greatest = max(counters, default=0)
    if greatest >= MAX_COUNTER:
        raise OverflowError(
            f"The counter for {date_text} is exhausted at .{greatest:02d}; "
            "the supported two-digit range cannot be incremented"
        )
    return f"{date_text}.{greatest + 1:02d}"


def stamp_version(manifest: str, version: str) -> str:
    """Return the manifest with its version line replaced, comments and all else intact."""
    stamped, replacements = VERSION_LINE.subn(f'version: "{version}"', manifest, count=1)
    if replacements != 1:
        raise ValueError("the manifest has no `version:` line to stamp")

    written = yaml.safe_load(stamped)
    if not isinstance(written, dict) or written.get("version") != version:
        raise ValueError(f"stamping {version} did not produce a manifest that states it")
    return stamped


def published_tags(owner: str, package: str, token: str) -> list[str]:
    """Return every tag on the package, so the day's counters can be read off them."""
    tags: list[str] = []
    for version in ghcr_api.all_versions(
        owner, package, token, owner_kind="organization", allow_missing=True
    ):
        tags.extend(ghcr_api.tags_of(version))
    return tags


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="the app manifest to stamp in place")
    parser.add_argument(
        "--stdin",
        action="store_true",
        help=(
            "read the manifest on stdin and write the stamped manifest to stdout, touching "
            "no file; how the recorder stamps the branch's manifest rather than the "
            "workspace's. Requires --set."
        ),
    )
    parser.add_argument("--owner", default="madeByJansen")
    parser.add_argument("--channel", choices=CHANNELS, default=os.environ.get("CHANNEL", "stable"))
    parser.add_argument(
        "--set",
        dest="chosen",
        default="",
        help="stamp this exact version instead of choosing one; the registry is not read",
    )
    parser.add_argument("--date", help="UTC date to choose for, as YYYYMMDD (default: today)")
    args = parser.parse_args(argv)

    if args.stdin and not args.chosen:
        parser.error("--stdin stamps a version that is already known; pass --set")
    settings = channel_settings(args.channel)
    if not args.stdin:
        expected_config = Path(__file__).resolve().parents[1] / settings["addon_dir"] / "config.yaml"
        if args.config and args.config.resolve() != expected_config:
            parser.error("--config does not match the selected channel")
        args.config = expected_config

    try:
        if args.chosen:
            version = args.chosen
        else:
            token = os.environ.get("GHCR_TOKEN", "")
            if not token:
                raise ValueError("GHCR_TOKEN is required to read the published versions")
            chosen_date = (
                dt.datetime.strptime(args.date, "%Y%m%d").date() if args.date else utc_date()
            )
            version = next_version(published_tags(args.owner, settings["package"], token), chosen_date)

        if args.stdin:
            # stdout is the manifest here, so the version is not printed: the caller
            # passed it in and anything else on this stream would end up in the commit.
            sys.stdout.write(stamp_version(sys.stdin.read(), version))
            return 0

        manifest = args.config.read_text(encoding="utf-8")
        args.config.write_text(stamp_version(manifest, version), encoding="utf-8")
    except (ValueError, OverflowError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        # Never guess a version from a failed listing: .01 would republish over a version
        # somebody is already running.
        print(f"ERROR: could not read the published versions: {error}", file=sys.stderr)
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
