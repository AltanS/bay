"""GitHub webhook interaction helpers for bay validate and service commands.

This module owns all GitHub webhook API calls, URL parsing, and probe logic
used by both ``bay validate --check-webhook-health`` and
``bay service prune-webhooks`` / ``bay service remove``.

Token values are NEVER included in any log or error message — only vault key
names are referenced.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import requests

# ── Repo URL parser ───────────────────────────────────────────────────────

_HTTPS_RE = re.compile(r"https?://github\.com/([^/]+)/([^/.]+?)(?:\.git)?/?$")
_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^/.]+?)(?:\.git)?/?$")


def parse_repo_owner_name(repo_url: str) -> tuple[str, str] | None:
    """Parse a GitHub repo URL into (owner, name).

    Handles:
    - https://github.com/Org/repo.git
    - https://github.com/Org/repo
    - git@github.com:Org/repo.git
    - git@github.com:Org/repo

    Returns None for non-github.com URLs or unparseable inputs.
    """
    if not repo_url:
        return None
    url = repo_url.strip()
    m = _HTTPS_RE.match(url) or _SSH_RE.match(url)
    if not m:
        return None
    return m.group(1), m.group(2)


# ── webhook_domain resolver ───────────────────────────────────────────────


def resolve_webhook_domain(root: Path, env: str = "production") -> str | None:
    """Read webhook_domain from the consumer's group_vars.

    Strategy:
    1. group_vars/all/main.yml
    2. Walk group_vars/<region>/main.yml for multi-region consumers — first found.

    Returns normalised value (trailing slash stripped, lowercase), or None.
    """
    try:
        from ruamel.yaml import YAML  # type: ignore[import-untyped]
    except ImportError:
        return None

    yaml = YAML()

    def _load(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            with path.open() as fh:
                data = yaml.load(fh)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    # 1. group_vars/all/main.yml
    data = _load(root / "group_vars" / "all" / "main.yml")
    if data and "webhook_domain" in data:
        return str(data["webhook_domain"]).rstrip("/").lower()

    # 2. Walk region subdirs
    group_vars = root / "group_vars"
    if group_vars.is_dir():
        for subdir in sorted(group_vars.iterdir()):
            if subdir.is_dir() and subdir.name != "all":
                data = _load(subdir / "main.yml")
                if data and "webhook_domain" in data:
                    return str(data["webhook_domain"]).rstrip("/").lower()

    return None


# ── GitHub API helpers ────────────────────────────────────────────────────


def list_repo_hooks(
    owner: str,
    name: str,
    token: str,
    *,
    _cache: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """GET /repos/{owner}/{name}/hooks and return the parsed JSON list.

    Results are cached in ``_cache`` (keyed by ``owner/name``) so that
    multi-service monorepos only make one API call per run.

    Returns an empty list on 404 (repo has no hooks or token lacks access
    at the hook level but repo exists).
    Raises ``requests.HTTPError`` for other non-2xx responses.
    """
    cache_key = f"{owner}/{name}"
    if _cache is not None and cache_key in _cache:
        return _cache[cache_key]

    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{name}/hooks",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )

    if resp.status_code == 404:
        result: list[dict[str, Any]] = []
    else:
        resp.raise_for_status()
        result = resp.json()

    if _cache is not None:
        _cache[cache_key] = result

    return result


# ── Probe result ──────────────────────────────────────────────────────────


@dataclasses.dataclass
class ProbeResult:
    """Result of probing a single service's webhook."""

    service: str
    status: str   # "ok" | "fail" | "warn" | "note"
    message: str


def _normalise_url(url: str) -> str:
    """Strip trailing slash and lowercase for comparison."""
    return url.rstrip("/").lower()


def probe_service_webhook(
    service_name: str,
    hook_url_pattern: str,
    hooks: list[dict[str, Any]],
) -> ProbeResult:
    """Probe a list of hooks to verify at least one matches the expected bay pattern.

    ``hook_url_pattern`` is the expected URL prefix:
    ``https://<webhook_domain>/webhook/<service_name>``.

    Semantics (per Issue #17 council deltas):
    - Repos can have multiple hooks (Slack, CI, other tools). We only care about
      hooks whose URL starts with our pattern — ignore all others.
    - If zero matching hooks → fail.
    - If one or more matching hooks → check last_response on the first match:
        * null / missing → informational note (non-blocking).
        * code 200 → ok.
        * any other integer → fail.
    """
    norm_pattern = _normalise_url(hook_url_pattern)

    matching = [
        h for h in hooks
        if _normalise_url(str(h.get("config", {}).get("url", ""))).startswith(norm_pattern)
    ]

    if not matching:
        return ProbeResult(
            service=service_name,
            status="fail",
            message=f"no webhook found for '{service_name}' matching {hook_url_pattern}",
        )

    hook = matching[0]
    last_response = hook.get("last_response")

    if last_response is None:
        return ProbeResult(
            service=service_name,
            status="note",
            message=(
                f"hook for '{service_name}' has never received a delivery "
                f"(last_response = null) — first push will validate end-to-end"
            ),
        )

    code = last_response.get("code") if isinstance(last_response, dict) else None

    if code is None:
        return ProbeResult(
            service=service_name,
            status="note",
            message=(
                f"hook for '{service_name}' has never received a delivery "
                f"(last_response = null) — first push will validate end-to-end"
            ),
        )

    if code == 200:
        return ProbeResult(
            service=service_name,
            status="ok",
            message=f"webhook for '{service_name}' is healthy (last HTTP {code})",
        )

    return ProbeResult(
        service=service_name,
        status="fail",
        message=(
            f"webhook for '{service_name}' returned HTTP {code} on last delivery "
            f"— check receiver logs"
        ),
    )


# ── Orphan detection ──────────────────────────────────────────────────────


def find_orphan_hooks(
    hooks: list[dict[str, Any]],
    webhook_domain: str,
    service_names: set[str],
) -> list[dict[str, Any]]:
    """Return hooks that belong to this consumer but have no matching service.

    An orphan hook satisfies ALL of:
    1. ``hook["config"]["url"]`` contains ``webhook_domain`` (this consumer's
       domain, case-insensitive). This scopes the search to this consumer —
       an Example Co hook on the same repo is NOT an orphan from demo's
       perspective.
    2. The URL path does NOT match any known service in ``service_names``
       (parse the segment after ``/webhook/``).
    """
    norm_domain = webhook_domain.rstrip("/").lower()
    orphans: list[dict[str, Any]] = []

    for hook in hooks:
        url = str(hook.get("config", {}).get("url", "")).rstrip("/").lower()

        # Only consider hooks that belong to this consumer's domain.
        if norm_domain not in url:
            continue

        # Extract the service name from /webhook/<name> path segment.
        # URL shape: https://webhook_domain/webhook/<service_name>
        # or:        https://webhook_domain/webhook/<service_name>/...
        path_match = re.search(r"/webhook/([^/?#]+)", url)
        if path_match:
            path_service = path_match.group(1)
            if path_service in service_names:
                continue  # known service — not an orphan

        # Belongs to our domain but no matching service found.
        orphans.append(hook)

    return orphans
