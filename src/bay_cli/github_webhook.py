"""GitHub webhook interaction helpers for bay validate and service commands.

This module owns all GitHub webhook API calls, URL parsing, and probe logic
used by both ``bay validate --check-webhook-health`` and
``bay service prune-webhooks``.

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


# ── Extra services of one bay.toml ────────────────────────────────────────


def push_sibling_groups(services: dict[str, Any]) -> list[list[str]]:
    """The groups of services that one push builds together, two or more each.

    Same rule as ``bay_push_siblings`` in filter_plugins/bay_filters.py, which
    writes ``siblings`` into the webhook receiver's config: a ``build`` with
    the same repo and branch (default ``main``), the same project (the same
    non-empty ``bay_toml_path``, or else the same non-empty ``bay_project``),
    and no ``shared_from``. The receiver fans a push posted for any member out
    to the others, so the group needs one GitHub hook, on any member. A
    local alias (``bay_push_siblings`` leaves it out of the fan-out, as the
    alias fan-out of rebuild.sh reaches it) is still a member: a hook on it
    builds its primary and the rest. Each group is sorted; the groups are
    sorted by their first name.
    """
    groups: dict[tuple[str, ...], list[str]] = {}
    for name, svc in (services or {}).items():
        build = svc.get("build") if isinstance(svc, dict) else None
        if not isinstance(build, dict) or build.get("shared_from"):
            continue
        repo = str(build.get("repo") or "")
        path = str(build.get("bay_toml_path") or "")
        project = str(build.get("bay_project") or "")
        which = ("toml", path) if path else ("project", project) if project else None
        if not repo or which is None:
            continue
        key = (repo, str(build.get("branch", "main")), *which)
        groups.setdefault(key, []).append(str(name))
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)


def hooks_for_service(hook_url: str, hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The hooks whose URL is ``hook_url`` exactly, or that URL plus a path or query.

    Stricter than the prefix match of ``probe_service_webhook``: the hook of
    ``shop-app`` is not a hook of ``shop`` here. The group check needs to
    know which member a hook belongs to.
    """
    want = _normalise_url(hook_url)
    out = []
    for hook in hooks:
        url = _normalise_url(str(hook.get("config", {}).get("url", "")))
        if url == want or url.startswith(want + "/") or url.startswith(want + "?"):
            out.append(hook)
    return out


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


# ── Creating and repairing the push hook (bay service add-webhook, 2.27.0) ─
#
# Secret handling. The token goes only into the Authorization header and the
# webhook secret only into the JSON body of a POST or PATCH to
# https://api.github.com. No error text carries a request body, a header or
# a response body: GitHub's own error messages are scrubbed of both values
# before they are quoted. Every call has a timeout and follows no redirect.

GITHUB_API = "https://api.github.com"
API_TIMEOUT = 10
#: The push and the ping the verb sends; the receiver ignores any event
#: without a ``ref`` (a ping included) after the signature check.
HOOK_EVENTS = ["push"]

WRITE_ACCESS_HINT = (
    "The github_admin_token in the vault needs write access to the webhooks of the repo: "
    "a classic token needs the scope admin:repo_hook, a fine-grained token needs the "
    "repository permission Webhooks: Read and write."
)

#: Verification words (the ``verification`` field of a hook row).
VERIFIED = "verified"
UNKNOWN_SERVICE = "receiver does not know the service"
SIGNATURE_REFUSED = "signature refused"
NOT_VERIFIED = "not verified yet"


def webhook_url(domain: str, container: str) -> str:
    """The one payload URL of a push hook: ``https://<domain>/webhook/<container>``."""
    return f"https://{domain.rstrip('/').lower()}/webhook/{container}"


def hook_config(url: str, secret: str) -> dict[str, str]:
    """The FULL config of a push hook. A PATCH replaces the whole config object.

    A PATCH with only ``url`` deletes the secret and resets ``content_type``
    to form: the hook still delivers and the receiver answers 401. So every
    write sends all four keys.
    """
    return {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}


def _domain_overrides(fleet_doc: dict[str, Any]) -> dict[str, str]:
    """``{group: webhook domain}`` from the boxes' ``webhook_domain``, as the compiler reads it."""
    out: dict[str, str] = {}
    for _name, box in sorted((fleet_doc.get("boxes") or {}).items()):
        if not isinstance(box, dict):
            continue
        own, group = box.get("webhook_domain"), box.get("group")
        if own and group:
            out.setdefault(str(group), str(own))
    return out


def _fleet_webhook(fleet_doc: dict[str, Any]) -> dict[str, Any]:
    from bay_cli.errors import BayError

    hook = fleet_doc.get("webhook")
    if not isinstance(hook, dict) or not hook.get("domain"):
        raise BayError.config(
            "bay.fleet.toml has no [webhook] domain, so no box runs a webhook receiver",
            hint='Add [webhook] with domain = "deploy.acme.example" and secret = '
            '"WEBHOOK_SECRET", then run bay up.',
        )
    return hook


def domain_for_groups(fleet_doc: dict[str, Any], groups: list[str]) -> str:
    """The webhook domain of a host in ``groups``, as the receiver's Traefik router picks it.

    The compiler renders the domain per host: the first group (sorted by
    name) of the host that has a box with ``webhook_domain``, else
    ``[webhook] domain``. Same rule here.
    """
    from bay_cli.errors import BayError

    default = str(_fleet_webhook(fleet_doc)["domain"])
    overrides = _domain_overrides(fleet_doc)
    chosen = next((d for g, d in sorted(overrides.items()) if g in groups), default)
    if "{{" in chosen:
        raise BayError.config(
            f"the webhook domain {chosen} is a Jinja value; Bay cannot tell the payload URL",
            hint="Write the domain itself in bay.fleet.toml.",
        )
    return chosen.rstrip("/").lower()


def domain_for_box(fleet_doc: dict[str, Any], box: str) -> str:
    """The webhook domain of the receiver on ``box``.

    The domain of the box's group, else ``[webhook] domain``.
    """
    entry = (fleet_doc.get("boxes") or {}).get(box) or {}
    group = entry.get("group") if isinstance(entry, dict) else None
    return domain_for_groups(fleet_doc, [str(group)] if group else [])


@dataclasses.dataclass
class HookTarget:
    """One hook that a push must reach: one per build container, one per sibling group."""

    #: The container the hook URL names (the first member with a hook, or the main one).
    container: str
    #: Every container this hook builds (a sibling group, else the container alone).
    members: list[str]
    owner: str
    name: str
    domain: str
    #: The box env whose vault holds the secret of the receiver that the hook reaches.
    secret_env: str

    @property
    def repo(self) -> str:
        return f"{self.owner}/{self.name}"

    def url(self, container: str | None = None) -> str:
        return webhook_url(self.domain, container or self.container)


def hook_problems(hook: dict[str, Any]) -> list[str]:
    """What is wrong with the settings of a Bay push hook. Empty when they are right.

    The secret value cannot be read back (GitHub shows ``********``): only a
    delivery that the receiver answers with 200 proves it.
    """
    config = hook.get("config") or {}
    out: list[str] = []
    if hook.get("active") is not True:
        out.append("the hook is not active")
    events = hook.get("events") or []
    if "push" not in events and "*" not in events:
        out.append("push is not in its events")
    if config.get("content_type") != "json":
        out.append(f"content type is {config.get('content_type') or 'form'}, not json")
    if str(config.get("insecure_ssl", "0")) != "0":
        out.append("the TLS check is off (insecure_ssl)")
    if not config.get("secret"):
        out.append("it has no secret")
    return out


def last_code(hook: dict[str, Any]) -> int | None:
    """``last_response.code`` of a hook; None before the first delivery."""
    last = hook.get("last_response")
    code = last.get("code") if isinstance(last, dict) else None
    return code if isinstance(code, int) else None


def verification_of(code: int | None) -> str:
    """The verification word for the receiver's HTTP answer to a delivery."""
    if code is None or code == 0:
        return NOT_VERIFIED
    if code == 200:
        return VERIFIED
    if code == 404:
        return UNKNOWN_SERVICE
    if code in (401, 403):
        # 401: GitHub sent no signature (no secret); 403: the signature does not match.
        return SIGNATURE_REFUSED
    return f"receiver answered HTTP {code}"


def _scrub(text: str, values: tuple[str, ...]) -> str:
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text


class GitHubHooks:
    """The few calls to the GitHub REST API that the verb makes.

    ``http`` is the ``requests`` module (tests pass a fake with the same
    ``request`` function). ``secrets`` are the values no error text may hold.
    """

    def __init__(
        self,
        token: str,
        *,
        http: Any = None,
        timeout: int = API_TIMEOUT,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self._token = token
        self._http = http if http is not None else requests
        self._timeout = timeout
        self._secrets = (token, *secrets)

    def _call(
        self, method: str, path: str, *, repo: str, body: dict[str, Any] | None = None
    ) -> Any:
        from bay_cli.errors import BayError, ErrorCode

        write = method != "GET"
        what = f"{method} {path}"
        try:
            resp = self._http.request(
                method,
                GITHUB_API + path,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=body,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # The exception text may quote the request; only its type is safe.
            # Raised outside this block, so no __context__ keeps the original.
            failed = type(exc).__name__
        else:
            failed = None
        if failed is not None:
            raise BayError(
                f"the GitHub API call {what} failed ({failed})",
                code=ErrorCode.REMOTE_ERROR,
                hint="Check the network and the GitHub status, then run it again.",
            )
        code = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= code < 300:
            if code == 204 or not getattr(resp, "content", b""):
                return None
            try:
                return resp.json()
            except ValueError:
                raise BayError(
                    f"the GitHub API answered {what} with no JSON", code=ErrorCode.REMOTE_ERROR
                ) from None
        if 300 <= code < 400:
            raise BayError(
                f"the GitHub API answered {what} with a redirect (HTTP {code}); Bay does not "
                "follow it with the token",
                code=ErrorCode.REMOTE_ERROR,
                hint=f"Check that the repo {repo} was not moved or renamed, and use its new name.",
            )
        if code == 401:
            raise BayError(
                f"GitHub refused the github_admin_token (HTTP 401) on {repo}",
                code=ErrorCode.CONFIG_ERROR,
                hint="The token is not valid or expired. " + WRITE_ACCESS_HINT,
            )
        if code in (403, 404):
            verb = "write" if write else "read"
            raise BayError(
                f"the github_admin_token cannot {verb} the webhooks of {repo} (HTTP {code})",
                code=ErrorCode.CONFIG_ERROR,
                hint="GitHub answers 404 also for a repo the token cannot see. "
                + WRITE_ACCESS_HINT,
            )
        if code == 422:
            raise BayError(
                f"GitHub refused the hook for {repo} (HTTP 422: {self._reasons(resp)})",
                code=ErrorCode.REMOTE_ERROR,
            )
        raise BayError(
            f"the GitHub API answered {what} with HTTP {code}", code=ErrorCode.REMOTE_ERROR
        )

    def _reasons(self, resp: Any) -> str:
        """GitHub's validation messages, scrubbed of the token and the secret."""
        try:
            data = resp.json()
        except ValueError:
            return "no details"
        if not isinstance(data, dict):
            return "no details"
        parts = [str(data.get("message") or "")]
        for err in data.get("errors") or []:
            if isinstance(err, dict) and err.get("message"):
                parts.append(str(err["message"]))
            elif isinstance(err, str):
                parts.append(err)
        text = "; ".join(p for p in parts if p) or "no details"
        return _scrub(text, self._secrets)[:300]

    def list_hooks(self, owner: str, name: str) -> list[dict[str, Any]]:
        repo = f"{owner}/{name}"
        data = self._call("GET", f"/repos/{repo}/hooks?per_page=100", repo=repo)
        return [h for h in data or [] if isinstance(h, dict)]

    def create(self, owner: str, name: str, url: str, secret: str) -> dict[str, Any]:
        body = {
            "name": "web",
            "active": True,
            "events": list(HOOK_EVENTS),
            "config": hook_config(url, secret),
        }
        repo = f"{owner}/{name}"
        return self._call("POST", f"/repos/{repo}/hooks", repo=repo, body=body) or {}

    def repair(
        self, owner: str, name: str, hook: dict[str, Any], url: str, secret: str
    ) -> dict[str, Any]:
        """ONE PATCH with the full config, ``active`` and ``events`` (push added)."""
        events = sorted({*(str(e) for e in hook.get("events") or []), *HOOK_EVENTS})
        body = {"active": True, "events": events, "config": hook_config(url, secret)}
        repo = f"{owner}/{name}"
        return self._call("PATCH", f"/repos/{repo}/hooks/{hook['id']}", repo=repo, body=body) or {}

    def ping(self, owner: str, name: str, hook_id: Any) -> None:
        self._call("POST", f"/repos/{owner}/{name}/hooks/{hook_id}/pings", repo=f"{owner}/{name}")

    def get(self, owner: str, name: str, hook_id: Any) -> dict[str, Any]:
        repo = f"{owner}/{name}"
        return self._call("GET", f"/repos/{repo}/hooks/{hook_id}", repo=repo) or {}


#: How often and how long the verb waits for the ping's answer: 10 x 1 s.
PING_POLLS = 10
PING_INTERVAL = 1.0


def verify_ping(
    client: GitHubHooks,
    target: HookTarget,
    hook_id: Any,
    *,
    sleep: Any,
    polls: int = PING_POLLS,
    interval: float = PING_INTERVAL,
) -> int | None:
    """Ping the hook, then poll it until ``last_response.code`` holds an answer.

    The PATCH and the POST that come before the ping leave no delivery, so a
    code read after the ping is the ping's answer. A repaired hook still
    shows the code of its last delivery before the ping, so a code that is
    the same as that one is taken only on the last poll. Returns None when
    no answer came in time.
    """
    before = last_code(client.get(target.owner, target.name, hook_id))
    client.ping(target.owner, target.name, hook_id)
    code: int | None = None
    for attempt in range(polls):
        sleep(interval)
        code = last_code(client.get(target.owner, target.name, hook_id))
        if code is not None and (code != before or before is None):
            return code
        if attempt == polls - 1:
            return code
    return code


def _exact(hooks: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    want = _normalise_url(url)
    return [h for h in hooks if _normalise_url(str((h.get("config") or {}).get("url", ""))) == want]


def _stale_urls(hooks: list[dict[str, Any]], target: HookTarget) -> list[str]:
    """Other hooks that name ``/webhook/<member>`` on another domain (an old domain)."""
    wanted = {_normalise_url(target.url(m)) for m in target.members}
    out = []
    for hook in hooks:
        url = str((hook.get("config") or {}).get("url", ""))
        match = re.search(r"/webhook/([^/?#]+)/?$", url)
        if match and match.group(1) in target.members and _normalise_url(url) not in wanted:
            out.append(url)
    return sorted(out)


def _next_for(verification: str, project: str, env: str) -> str | None:
    if verification == UNKNOWN_SERVICE:
        return (
            f"the receiver does not know the service yet: run `bay up {env} --project {project}` "
            "first, then run this again (the hook stays)"
        )
    if verification == SIGNATURE_REFUSED:
        return (
            "the receiver refused the signature; the box may run an older secret: run "
            f"`bay up {env} --project {project}`, then run this again"
        )
    if verification.startswith("receiver answered"):
        return "read `docker logs bay-webhook` on the box that the domain points at"
    return None


def ensure_hook(
    client: GitHubHooks,
    target: HookTarget,
    secret: str,
    hooks: list[dict[str, Any]],
    *,
    project: str,
    env: str,
    check: bool,
    sleep: Any,
    polls: int = PING_POLLS,
) -> dict[str, Any]:
    """Check, create or repair the hook of one target. Returns its row (no secret in it).

    ``hooks`` is the GET of the repo's hooks. A sibling group takes the first
    member with a hook; with none, the hook goes on ``target.container``.
    """
    found: dict[str, Any] | None = None
    extra: list[str] = []
    for member in target.members:
        own = _exact(hooks, target.url(member))
        if own and found is None:
            found, target = own[0], dataclasses.replace(target, container=member)
            extra += [target.url(member)] * (len(own) - 1)
        elif own:
            extra += [target.url(member)] * len(own)
    url = target.url()
    row: dict[str, Any] = {
        "container": target.container,
        "members": list(target.members),
        "repo": target.repo,
        "url": url,
        "hook_id": found.get("id") if found else None,
        "action": None,
        "verification": None,
        "problems": [],
        "next": None,
        "detail": None,
        "redundant_hooks": extra,
        "stale_hooks": _stale_urls(hooks, target),
    }
    problems = hook_problems(found) if found else []
    code = last_code(found) if found else None
    if found is not None and code != 200:
        problems.append(
            "it has no delivery yet" if code is None else f"its last delivery got HTTP {code}"
        )
    row["problems"] = problems

    if check:
        if found is None:
            row["action"] = "missing"
            row["next"] = f"run `bay service add-webhook {project} --env {env}`"
            return row
        row["action"] = "broken" if problems else "ok"
        row["verification"] = verification_of(code)
        if problems:
            # Right settings and a 404: only bay up helps. Anything else: the verb repairs it.
            fixed_by_up = not hook_problems(found) and row["verification"] == UNKNOWN_SERVICE
            row["next"] = (
                _next_for(UNKNOWN_SERVICE, project, env)
                if fixed_by_up
                else f"run `bay service add-webhook {project} --env {env}`"
            )
        return row

    if found is not None and not problems:
        row["action"] = "ok"
        row["verification"] = VERIFIED
        return row
    if found is None:
        made = client.create(target.owner, target.name, url, secret)
        row["action"] = "created"
    else:
        made = client.repair(target.owner, target.name, found, url, secret)
        row["action"] = "repaired"
    hook_id = made.get("id", found.get("id") if found else None)
    row["hook_id"] = hook_id
    answer = verify_ping(client, target, hook_id, sleep=sleep, polls=polls)
    row["verification"] = verification_of(answer)
    row["next"] = _next_for(row["verification"], project, env)
    if row["verification"] == NOT_VERIFIED:
        row["detail"] = "no answer to the ping in time; the first push will show it"
    return row


#: Strategies whose push reaches a Bay receiver. ``registry``: an outside CI builds.
PUSH_STRATEGIES = ("local", "remote")


def hook_targets(
    fleet_doc: dict[str, Any],
    services: dict[str, Any],
    containers: list[str],
    *,
    main: str | None,
    box: str,
    box_env: str,
    strategy_of: Any,
    build_server: Any,
) -> tuple[list[HookTarget], list[dict[str, str]]]:
    """``(targets, skipped)``: the hooks the build containers of one project need.

    ``strategy_of(entry)`` is the resolved build strategy (None: a Jinja
    value). ``build_server()`` returns ``(groups of the build server host,
    its box env)``; it is called only for a ``remote`` build.

    Where a push must land:

    * ``local``: the receiver of the box that runs the container. It writes
      the trigger, and the box builds.
    * ``remote``: the receiver of the build server. Only there is
      ``IS_BUILD_SERVER`` set: it builds, pushes to the registry and sends
      the pull signal to the boxes.

    The extra services of one bay.toml (``push_sibling_groups``) need one
    hook: the receiver fans a push out to the siblings.
    """
    from bay_cli.errors import BayError

    eligible: dict[str, dict[str, Any]] = {}
    places: dict[str, tuple[str, str]] = {}
    repos: dict[str, tuple[str, str]] = {}
    skipped: list[dict[str, str]] = []
    server: tuple[list[str], str] | None = None
    for name in sorted(containers):
        entry = services.get(name)
        if not isinstance(entry, dict):
            continue
        build = entry.get("build")
        if not isinstance(build, dict):
            continue
        if build.get("shared_from"):
            skipped.append(
                {"container": name, "reason": f"it shares the build of {build['shared_from']}; "
                 "the hook of that container builds it"}
            )
            continue
        strategy = strategy_of(entry)
        if strategy is None:
            skipped.append(
                {"container": name, "reason": "its build strategy is a Jinja value, so Bay "
                 "cannot tell where a push must land"}
            )
            continue
        if strategy not in PUSH_STRATEGIES:
            skipped.append(
                {"container": name, "reason": f"strategy {strategy}: an outside CI builds the "
                 "image, so a push needs no Bay hook"}
            )
            continue
        repo_url = str(build.get("repo") or "")
        parsed = parse_repo_owner_name(repo_url)
        if parsed is None:
            raise BayError.config(
                f"{name} builds from {repo_url or 'no repo'}: only github.com repos are "
                "supported",
                hint="Register the hook of another git host by hand: payload URL "
                "https://<webhook domain>/webhook/<container>, the [webhook] secret, "
                "JSON, push events.",
            )
        if strategy == "remote":
            if server is None:
                server = build_server()
            groups, env = server
            places[name] = (domain_for_groups(fleet_doc, groups), env)
        else:
            places[name] = (domain_for_box(fleet_doc, box), box_env)
        eligible[name] = entry
        repos[name] = parsed

    targets: list[HookTarget] = []
    grouped: set[str] = set()
    for group in push_sibling_groups(eligible):
        if len({places[m] for m in group}) > 1:
            raise BayError.config(
                f"the services {', '.join(group)} of one bay.toml build from one push, but "
                "their pushes must land on different receivers (a local and a remote build)",
                hint="Give them one build strategy, or register their hooks by hand.",
            )
        first = main if main in group else group[0]
        domain, env = places[first]
        owner, repo = repos[first]
        members = [first, *[m for m in group if m != first]]
        targets.append(HookTarget(first, members, owner, repo, domain, env))
        grouped.update(group)
    for name in sorted(eligible):
        if name in grouped:
            continue
        domain, env = places[name]
        owner, repo = repos[name]
        targets.append(HookTarget(name, [name], owner, repo, domain, env))
    targets.sort(key=lambda t: t.container)
    return targets, skipped
