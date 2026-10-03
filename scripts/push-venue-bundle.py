#!/usr/bin/env python3
"""Publish the ClearVenue bundle and its signed manifest beside the image, in the same package.

Pushed to the channel's GHCR package as one OCI artifact of two files, under two tags:

    <version>-bundle   that release
    bundle             the newest release on the channel

A venue that is not run by Home Assistant reads the manifest, checks its signature against
the keys it already trusts, then downloads the bundle and checks its SHA-256. The registry is
storage: nothing about a release is trusted because of where it was found.

Plain HTTPS against the registry's own API (the OCI distribution protocol), so the pipeline
needs nothing it does not already have. GHCR_USERNAME and GHCR_TOKEN are the same login the
image push uses.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from channels import CHANNELS, channel_settings

REGISTRY = "ghcr.io"
OWNER = "madebyjansen"
LATEST_TAG = "bundle"
VERSION_TAG = "{version}-bundle"

MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.clearvenue.release.config.v1+json"
SIGNED_TYPE = "application/vnd.clearvenue.release.manifest.v1+json"
BUNDLE_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
TITLE = "org.opencontainers.image.title"
VERSION = re.compile(r"^\d{8}\.\d{2}$")


class PushError(Exception):
    """The registry refused or could not be reached; said without the token."""


def repository(channel: str) -> str:
    """Return the channel's package, as the registry names it: always lowercase."""
    return f"{OWNER}/{channel_settings(channel)['package']}".lower()


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def artifact(config: bytes, layers: list[tuple[str, str, bytes]]) -> bytes:
    """Return the OCI manifest naming ``config`` and each ``(title, media type, body)``."""
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_TYPE,
            "config": {"mediaType": CONFIG_TYPE, "digest": digest_of(config), "size": len(config)},
            "layers": [
                {
                    "mediaType": media_type,
                    "digest": digest_of(body),
                    "size": len(body),
                    "annotations": {TITLE: title},
                }
                for title, media_type, body in layers
            ],
        },
        indent=2,
        sort_keys=True,
    ).encode()


def release_files(folder: Path, channel: str, version: str) -> list[tuple[str, str, bytes]]:
    """Return the signed manifest and the bundle, checked to name the same release."""
    signed_path = folder / f"clearvenue-{channel}.json"
    bundle_path = folder / f"clearvenue-{version}.tar.gz"
    for path in (signed_path, bundle_path):
        if not path.is_file():
            raise PushError(f"{path.name} is missing from {folder}")
    signed = signed_path.read_bytes()
    bundle = bundle_path.read_bytes()
    said = json.loads(signed)
    expected = {
        "product": "clearvenue",
        "channel": channel,
        "version": version,
        "package": bundle_path.name,
        "sha256": hashlib.sha256(bundle).hexdigest(),
    }
    wrong = sorted(key for key, value in expected.items() if said.get(key) != value)
    if wrong or not said.get("signature"):
        raise PushError(f"{signed_path.name} does not describe {bundle_path.name}: {wrong or 'unsigned'}")
    return [(signed_path.name, SIGNED_TYPE, signed), (bundle_path.name, BUNDLE_TYPE, bundle)]


class Registry:
    """One package in the registry, written to with a push token."""

    def __init__(self, name: str, username: str, token: str, opener=None) -> None:
        self.name = name
        self._username = username
        self._token = token
        self._open = opener or urllib.request.urlopen
        self._bearer = ""

    def _call(self, method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None):
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with self._open(request) as reply:
                return reply.status, dict(reply.headers), reply.read()
        except urllib.error.HTTPError as answered:
            return answered.code, dict(answered.headers or {}), answered.read() if answered.fp else b""
        except urllib.error.URLError as failed:
            raise PushError(f"{REGISTRY} could not be reached: {failed.reason}") from None

    def _auth(self) -> dict[str, str]:
        if not self._bearer:
            pair = base64.b64encode(f"{self._username}:{self._token}".encode()).decode()
            query = urllib.parse.urlencode(
                {"service": REGISTRY, "scope": f"repository:{self.name}:pull,push"}
            )
            status, _, body = self._call(
                "GET", f"https://{REGISTRY}/token?{query}", {"Authorization": f"Basic {pair}"}
            )
            if status != 200:
                raise PushError(f"{REGISTRY} refused the login ({status}); it needs write:packages")
            self._bearer = str(json.loads(body)["token"])
        return {"Authorization": f"Bearer {self._bearer}"}

    def _url(self, path: str) -> str:
        return f"https://{REGISTRY}/v2/{self.name}/{path}"

    def put_blob(self, body: bytes) -> str:
        digest = digest_of(body)
        status, _, _ = self._call("HEAD", self._url(f"blobs/{digest}"), self._auth())
        if status == 200:
            return digest
        status, headers, _ = self._call("POST", self._url("blobs/uploads/"), self._auth(), b"")
        location = headers.get("Location") or headers.get("location")
        if status != 202 or not location:
            raise PushError(f"{REGISTRY} would not start an upload ({status})")
        target = urllib.parse.urljoin(self._url(""), location)
        separator = "&" if urllib.parse.urlsplit(target).query else "?"
        status, _, _ = self._call(
            "PUT",
            f"{target}{separator}digest={urllib.parse.quote(digest)}",
            {**self._auth(), "Content-Type": "application/octet-stream"},
            body,
        )
        if status != 201:
            raise PushError(f"{REGISTRY} refused a release file ({status})")
        return digest

    def put_manifest(self, tag: str, manifest: bytes) -> None:
        status, _, _ = self._call(
            "PUT",
            self._url(f"manifests/{tag}"),
            {**self._auth(), "Content-Type": MANIFEST_TYPE},
            manifest,
        )
        if status not in (200, 201):
            raise PushError(f"{REGISTRY} refused the tag {tag} ({status})")

    def get_manifest(self, tag: str) -> bytes:
        status, _, body = self._call(
            "GET", self._url(f"manifests/{tag}"), {**self._auth(), "Accept": MANIFEST_TYPE}
        )
        if status != 200:
            raise PushError(f"{REGISTRY} does not have {tag} ({status})")
        return body


def push(registry: Registry, channel: str, version: str, folder: Path) -> list[str]:
    """Publish the release in ``folder``; return the tags now naming it."""
    layers = release_files(folder, channel, version)
    config = b"{}"
    registry.put_blob(config)
    for _, _, body in layers:
        registry.put_blob(body)
    manifest = artifact(config, layers)
    tags = [VERSION_TAG.format(version=version), LATEST_TAG]
    for tag in tags:
        registry.put_manifest(tag, manifest)
    if digest_of(registry.get_manifest(tags[0])) != digest_of(manifest):
        raise PushError(f"{tags[0]} does not read back as the release that was pushed")
    return tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=CHANNELS, default=os.environ.get("CHANNEL", "stable"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--dir", type=Path, default=Path(__file__).resolve().parents[1] / "venue-release")
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.version):
        parser.error("--version must be YYYYMMDD.NN")
    if not args.dir.is_dir():
        print(f"No ClearVenue bundle was built for {args.version}; nothing to publish.")
        return 0
    username, token = os.environ.get("GHCR_USERNAME", ""), os.environ.get("GHCR_TOKEN", "")
    if not username or not token:
        print("GHCR_USERNAME and GHCR_TOKEN are required", file=sys.stderr)
        return 2
    try:
        name = repository(args.channel)
        tags = push(Registry(name, username, token), args.channel, args.version, args.dir)
    except (PushError, ValueError, KeyError) as error:
        message = str(error).replace(token, "REDACTED") if token else str(error)
        print(f"Publishing the ClearVenue bundle failed: {message}", file=sys.stderr)
        return 1
    print(f"Published ClearVenue {args.version} to {REGISTRY}/{name} as {', '.join(tags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
