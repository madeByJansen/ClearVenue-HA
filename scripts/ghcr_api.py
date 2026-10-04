"""Read and write the app's GHCR package through GitHub's packages API.

Two scripts need the same access — one chooses the next version from what is already
published, the other deletes what is too old to keep — and two copies of an API contract
is how they stop agreeing. Kept as a module rather than a command because it is only ever
called by those.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import urllib.response

PAGE_SIZE = 100
OWNER_ROUTES = {"organization": "orgs", "user": "users"}


def package_url(owner: str, package: str, owner_kind: str) -> str:
    """Return a package endpoint for an explicitly typed GitHub owner."""
    route = OWNER_ROUTES[owner_kind]
    return "https://api.github.com/{}/{}/packages/container/{}".format(
        route, urllib.parse.quote(owner, safe=""), urllib.parse.quote(package, safe="")
    )


def versions_url(owner: str, package: str, owner_kind: str) -> str:
    return package_url(owner, package, owner_kind) + "/versions"


def _sanitized_url(url: str) -> str:
    """Remove credentials and potentially sensitive query values from a diagnostic URL."""
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname += f":{parsed.port}"
    safe_query = urllib.parse.urlencode([
        (key, "REDACTED" if key.lower() in {"access_token", "token", "authorization"} else value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urllib.parse.urlunsplit((parsed.scheme, hostname, parsed.path, safe_query, parsed.fragment))


def request(url: str, token: str, method: str = "GET") -> urllib.response.addinfourl:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            github_error = json.loads(body)
        except json.JSONDecodeError:
            github_error = body
        reason = (
            f"{method} {_sanitized_url(url)} returned status {error.code}; "
            f"GitHub response: {json.dumps(github_error, ensure_ascii=False)}"
        )
        if token:
            reason = reason.replace(token, "REDACTED")
        raise urllib.error.HTTPError(
            _sanitized_url(url), error.code, reason, error.headers, None
        ) from None


def _next_url(response: urllib.response.addinfourl) -> str | None:
    """Extract GitHub's next-page target from an RFC 8288 Link header."""
    link = getattr(response, "headers", {}).get("Link")
    if not link:
        return None
    for value in link.split(","):
        target, *parameters = value.split(";")
        if any(parameter.strip() == 'rel="next"' for parameter in parameters):
            return target.strip().removeprefix("<").removesuffix(">")
    return None


def all_versions(owner: str, package: str, token: str, *, owner_kind: str, allow_missing=False) -> list[dict[str, object]]:
    """Return every version object in the package, following pagination."""
    base = versions_url(owner, package, owner_kind)
    versions: list[dict[str, object]] = []
    url: str | None = f"{base}?per_page={PAGE_SIZE}"
    while True:
        try:
            with request(url, token) as response:
                batch = json.load(response)
                url = _next_url(response)
        except urllib.error.HTTPError as error:
            # A new channel has no package yet. An invalid token is rejected with 401, not
            # 404, so a 404 on the first page means nothing is published. The owner package
            # listing cannot confirm this: GitHub answers it with 400 "Invalid argument"
            # for the workflow token.
            if error.code != 404 or not allow_missing or versions:
                raise
            return []
        versions.extend(batch)
        if url is None:
            break
    return versions


def tags_of(version: dict[str, object]) -> list[str]:
    """Return a version object's container tags, which an untagged manifest lacks."""
    metadata = version.get("metadata", {})
    container = metadata.get("container", {}) if isinstance(metadata, dict) else {}
    tags = container.get("tags", []) if isinstance(container, dict) else []
    return [str(tag) for tag in tags]
