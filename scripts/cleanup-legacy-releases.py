#!/usr/bin/env python3
"""Retire the old clearsignage-ha package and unchannelled release tags (dry-run by default).

Requires main to contain all new manifests and every advertised image to be published.
Never targets the upstream ClearSignage repository or any new channel package.
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse

import yaml
import ghcr_api
from channels import CHANNELS, channel_settings

LEGACY_PACKAGE = "clearsignage-ha"
LEGACY_TAG = re.compile(r"^v\d+(?:\.\d+)+$")
OWNER = "madeByJansen"
OWNER_KIND = "organization"


def package_exists(package, token):
    try:
        read_json(ghcr_api.package_url(OWNER, package, OWNER_KIND), token)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    return True


def read_json(url, token):
    with ghcr_api.request(url, token) as response:
        return json.load(response)


def collection(url, token):
    items = []
    page = 1
    while True:
        batch = read_json(f"{url}?per_page=100&page={page}", token)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def replacement_ready(tree, manifests, tags_by_package):
    if "clearsignage/config.yaml" in tree:
        raise ValueError("main still advertises the old slug; finish the channel rollout first")
    for channel in CHANNELS:
        settings = channel_settings(channel)
        config = manifests[channel]
        if config.get("slug") != settings["slug"] or config.get("image") != settings["image"]:
            raise ValueError(f"main does not advertise the expected {channel} replacement")
        version = str(config["version"])
        required = {version, version + "-amd64", version + "-aarch64"}
        if not required <= tags_by_package[settings["package"]]:
            raise ValueError(f"Publish the advertised {channel} version {version} for both architectures first")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default="madeByJansen/ClearVenue-HA")
    parser.add_argument("--apply", action="store_true", help="delete only the displayed legacy artifacts")
    args = parser.parse_args()
    # A typo must not turn a cleanup into deletion from an unrelated repository.
    if args.repository not in {"madeByJansen/ClearVenue-HA", "madeByJansen/ClearSignage-HA"}:
        parser.error("only this HA packaging repository can be cleaned")
    token = os.environ.get("GHCR_TOKEN", "")
    if not token:
        parser.error("GHCR_TOKEN with contents write and read/delete:packages is required")
    api = "https://api.github.com/repos/" + args.repository
    main_sha = read_json(api + "/git/ref/heads/main", token)["object"]["sha"]
    tree_result = read_json(api + f"/git/trees/{main_sha}?recursive=1", token)
    if tree_result.get("truncated"):
        raise ValueError("Repository tree is truncated; refusing cleanup")
    tree = {item["path"] for item in tree_result["tree"]}
    manifests, tags = {}, {}
    for channel in CHANNELS:
        settings = channel_settings(channel)
        content = read_json(api + f"/contents/{settings['addon_dir']}/config.yaml?ref={main_sha}", token)
        manifests[channel] = yaml.safe_load(base64.b64decode(content["content"]))
        tags[settings["package"]] = {
            tag for version in ghcr_api.all_versions(OWNER, settings["package"], token, owner_kind=OWNER_KIND)
            for tag in ghcr_api.tags_of(version)
        }
    replacement_ready(tree, manifests, tags)
    actions = []
    releases = collection(api + "/releases", token)
    for release in releases:
        if LEGACY_TAG.fullmatch(release["tag_name"]):
            actions.append((f"GitHub release {release['tag_name']}", api + f"/releases/{release['id']}"))
    for tag in collection(api + "/tags", token):
        if LEGACY_TAG.fullmatch(tag["name"]):
            actions.append((f"Git tag {tag['name']}", api + "/git/refs/tags/" + urllib.parse.quote(tag["name"], safe="")))
    if package_exists(LEGACY_PACKAGE, token):
        actions.append((f"Entire GHCR package {LEGACY_PACKAGE} (including untagged layers)",
                        ghcr_api.package_url(OWNER, LEGACY_PACKAGE, OWNER_KIND)))
    for label, url in actions:
        print(f"{'Deleting' if args.apply else 'Would delete'}: {label}", flush=True)
        if args.apply:
            with ghcr_api.request(url, token, method="DELETE"):
                pass
    print(f"{'Deleted' if args.apply else 'Dry-run selected'} {len(actions)} legacy artifacts.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, urllib.error.URLError) as error:
        sys.exit(f"Legacy cleanup stopped: {error}")
