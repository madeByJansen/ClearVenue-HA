#!/usr/bin/env python3
"""Delete GHCR image versions older than the current and previous releases, and their tags.

A release is its images, its ClearVenue bundle and its Git tag (`<channel>/v<version>`). When
its images are deleted the tag names something nobody can install any more, so it goes too —
as does every unqualified `v<version>` tag left from before the channels, whose images were in
a package that has been retired.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from channels import CHANNELS, channel_settings
import ghcr_api  # noqa: E402  (the module sits beside this command, not on the path)


RELEASE_TAG = re.compile(r"^(\d+(?:\.\d+)+)(?:-(aarch64|amd64|bundle))?$")
CHANNEL_GIT_TAG = re.compile(r"^(?P<channel>[a-z]+)/v(?P<release>\d+(?:\.\d+)+)$")
LEGACY_GIT_TAG = re.compile(r"^v\d+(?:\.\d+)+$")


def release_from_tags(tags: list[str]) -> str | None:
    """Return the release represented by a GHCR version's tags, if any."""
    releases = {match.group(1) for tag in tags if (match := RELEASE_TAG.fullmatch(tag))}
    if len(releases) > 1:
        raise ValueError(f"GHCR version unexpectedly contains several releases: {tags}")
    return next(iter(releases), None)


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def versions_to_delete(versions: list[dict[str, object]], current: str) -> list[int]:
    """Select tagged release objects while preserving N and N-1.

    Untagged objects are intentionally ignored: they can be platform manifests referenced
    by one of the two retained multi-architecture indexes.
    """
    releases = {
        release
        for item in versions
        if (release := release_from_tags(item.get("metadata", {}).get("container", {}).get("tags", [])))
    }
    if current not in releases:
        raise ValueError(f"new release {current} was not found in GHCR; refusing to prune")

    current_key = version_key(current)
    older = sorted(
        (release for release in releases if version_key(release) < current_key),
        key=version_key,
    )
    retained = {current}
    if older:
        retained.add(older[-1])
    doomed_releases = set(older) - retained

    selected = []
    for item in versions:
        tags = item.get("metadata", {}).get("container", {}).get("tags", [])
        release = release_from_tags(tags)
        if release in doomed_releases:
            selected.append(int(item["id"]))
    return selected


def published_releases(versions: list[dict[str, object]], deleted: list[int]) -> set[str]:
    """Return the releases still in the package once ``deleted`` versions are gone."""
    gone = set(deleted)
    return {
        release
        for item in versions
        if int(item["id"]) not in gone
        and (release := release_from_tags(item.get("metadata", {}).get("container", {}).get("tags", [])))
    }


def git_tags_to_delete(tags: list[str], channel: str, published: set[str], current: str) -> list[str]:
    """Select the Git tags whose images are gone.

    This channel's tags older than ``current`` whose release is no longer published, and every
    unqualified legacy tag. Another channel's tags, the current release's and anything newer
    are never selected.
    """
    current_key = version_key(current)
    doomed = []
    for tag in tags:
        if LEGACY_GIT_TAG.fullmatch(tag):
            doomed.append(tag)
            continue
        match = CHANNEL_GIT_TAG.fullmatch(tag)
        if not match or match["channel"] != channel:
            continue
        release = match["release"]
        if release not in published and version_key(release) < current_key:
            doomed.append(tag)
    return sorted(doomed, key=lambda tag: version_key(tag.rsplit("v", 1)[1]))


def remote_tags(remote: str) -> list[str]:
    """Return every tag on ``remote``; credentials come from GIT_ASKPASS, never the URL."""
    listed = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", remote],
        capture_output=True, text=True, check=True,
    )
    return [line.split("refs/tags/", 1)[1] for line in listed.stdout.splitlines() if "refs/tags/" in line]


def delete_remote_tags(remote: str, tags: list[str]) -> None:
    subprocess.run(
        ["git", "push", "--quiet", remote, "--delete", *(f"refs/tags/{tag}" for tag in tags)],
        capture_output=True, text=True, check=True,
    )


def prune_git_tags(remote: str, channel: str, published: set[str], current: str, apply: bool) -> None:
    """Delete the tags of releases whose images are gone; a failure is said, not fatal.

    The release itself is already published and recorded by now, so a tag left behind is
    untidy rather than wrong — the same judgement the recorder makes about creating one.
    """
    try:
        doomed = git_tags_to_delete(remote_tags(remote), channel, published, current)
        if doomed and apply:
            delete_remote_tags(remote, doomed)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Could not prune the release tags on {remote}: {error}", file=sys.stderr)
        return
    for tag in doomed:
        print(f"{'Deleted' if apply else 'Would delete'} tag {tag}")
    print(f"Selected {len(doomed)} tags whose images are gone")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="madeByJansen")
    parser.add_argument("--channel", choices=CHANNELS, default=os.environ.get("CHANNEL", "stable"))
    parser.add_argument("--current", required=True)
    parser.add_argument("--apply", action="store_true", help="delete selected versions; otherwise dry-run")
    parser.add_argument("--tags-remote", default="origin", help="where the release tags are (default: origin)")
    args = parser.parse_args()
    owner, package, current = args.owner, channel_settings(args.channel)["package"], args.current
    token = os.environ.get("GHCR_TOKEN")
    if not token:
        print("GHCR_TOKEN is required", file=sys.stderr)
        return 2

    owner_kind = "organization"
    base = ghcr_api.versions_url(owner, package, owner_kind)
    versions = ghcr_api.all_versions(owner, package, token, owner_kind=owner_kind)

    doomed = versions_to_delete(versions, current)
    for version_id in doomed:
        if args.apply:
            with ghcr_api.request(f"{base}/{version_id}", token, method="DELETE"):
                pass
        print(f"{'Deleted' if args.apply else 'Would delete'} {package} version {version_id}")
    print(f"Selected {len(doomed)} {package} versions; retained release {current} and its predecessor")
    prune_git_tags(args.tags_remote, args.channel, published_releases(versions, doomed), current, args.apply)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as error:
        print(f"GHCR pruning failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
