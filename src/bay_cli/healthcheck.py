"""Post-deploy reachability audit.

Hits every public service's `domains:` with an HTTPS GET in parallel and
reports 2xx/3xx as pass, 4xx/5xx/timeout/TLS-error as fail. Designed to
run right after `ansible-playbook` exits zero, so `bin/bay deploy` can
bubble up user-visible outages that ansible's container-level health
doesn't catch.

Probes retry inside a wall-clock budget sized by *how* the attempt failed —
see the readiness-window comment below. A healthy service passes on attempt 1,
so a green deploy costs nothing extra.

This module is the pure-logic layer. The CLI wiring (bay healthcheck)
lives in `src/bay_cli/commands/healthcheck.py`.
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    RequestException,
    SSLError,
    Timeout,
)


# Connect timeout. Read timeout stays low too — a healthy service's root
# path should respond quickly; waiting 30s for a slow upstream just hides
# a slow backend.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 5.0

# ── Readiness windows ─────────────────────────────────────────────────
#
# A probe retries until its *budget of wall-clock* is spent, not for a fixed
# number of attempts. The budget is the number an operator can reason about
# ("a dead service is reported within ~90s") and it stays honest whether each
# attempt returns in 5ms or burns 8s on a timeout.
#
# The budget depends on WHAT the failure says, because "not listening yet" and
# "answering wrongly" are different problems and deserve different patience:
#
#   starting   — no TCP connection at all, or 502/503/504. Traefik is up and
#                has a router for the host, but the upstream is not accepting
#                connections: that is precisely the shape of a container that
#                is still booting. Gets the full startup budget.
#   definitive — the app itself answered (4xx, or a 5xx that isn't a gateway
#                error) or TLS failed. Waiting rarely changes any of these, but
#                a recreate does briefly drop the Traefik router (→ 404), so a
#                short retry is kept. This is the legacy ~10s window.
#   fatal      — the hostname does not resolve. No amount of waiting invents a
#                DNS record; retrying only burns the window for nothing.
#
# 90s is not arbitrary: it is `git_deploy_health_check_timeout`, the framework's
# existing answer to "how long may a service take to come up after a restart"
# (roles/git_deploy/defaults/main.yml, docs/build-pipeline.md). The post-deploy
# probe and rebuild.sh's health poll are asking the same question, so they use
# the same default and the same per-service override.
STARTUP_BUDGET_S = 90.0
DEFINITIVE_BUDGET_S = 10.0

# Backoff grows 2 → 4 → 8 → 10 → 10 … so a slow starter is re-probed sooner
# than the old fixed 5s (a service ready at t=3s now passes at t=2s instead of
# t=5s), while a service down for the whole window costs ~11 requests, not ~18.
RETRY_BACKOFF_S = 2.0
RETRY_BACKOFF_MAX_S = 10.0

# Belt-and-braces termination guard, not the real bound — the budget is. This
# only binds if the clock barely moves between attempts. It is a floor, not a
# ceiling: `_attempt_cap` scales it with the budget so a widened window
# (health_check_timeout: 240) can't hit the cap before it spends its time.
MAX_ATTEMPTS = 20

# Traefik answers 502/503/504 when it has a router for the host but the
# upstream refuses/never answers. 500 is deliberately NOT here: that is the
# app itself erroring, which is a definitive answer.
_STARTING_STATUSES = frozenset({502, 503, 504})


@dataclass
class CheckResult:
    service: str
    domain: str
    status: int | None  # HTTP status, or None if the request never completed
    ok: bool
    redirect_chain: list[str] = field(default_factory=list)
    error: str | None = None
    attempts: int = 1
    elapsed_ms: int = 0
    skipped: bool = False
    skip_reason: str | None = None
    probed_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "domain": self.domain,
            "probed_url": self.probed_url,
            "status": self.status,
            "ok": self.ok,
            "redirect_chain": self.redirect_chain,
            "error": self.error,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


def display_label(result: "CheckResult") -> str:
    """Operator-facing identifier for a probe result.

    Bare domain when the probed path is `/` (or unset) — preserves the
    previous layout. Full URL when `healthcheck_path` puts the probe on
    a non-root route, so operators see exactly which path was checked."""
    if result.probed_url is None:
        return result.domain
    parsed = urlparse(result.probed_url)
    if parsed.path in ("", "/"):
        return result.domain
    return result.probed_url


def readiness_note(result: CheckResult) -> str:
    """Suffix for a service that needed the retry window to come up.

    Empty for the normal case (passed on attempt 1). The window is wide enough
    (90s) to swallow a cold start silently; printing what it swallowed keeps a
    service creeping toward the ceiling visible, instead of letting it read as
    instantly-green until the day it tips over."""
    if result.attempts <= 1:
        return ""
    return f" (ready after {result.elapsed_ms / 1000:.0f}s, {result.attempts} attempts)"


def _path_covered_by_public_routes(path: str, public_routes: list[str]) -> bool:
    """Return True iff `path` is publicly reachable through any route.
    Traefik matches public_routes with PathPrefix, so /foo covers /foo
    and /foo/bar but not /food."""
    for route in public_routes:
        route = route.rstrip("/") or "/"
        if path == route:
            return True
        prefix = route if route.endswith("/") else route + "/"
        if path.startswith(prefix):
            return True
    return False


def should_skip_vpn_only(service: dict[str, Any], include_vpn: bool) -> tuple[bool, str | None]:
    """Return (skip?, reason). Skip VPN-gated probes unless --include-vpn.

    A service with `access: vpn` AND `public_routes` exposes only those
    specific paths publicly. The probe target (healthcheck_path or "/")
    must be covered by one of those prefixes to be reachable from
    outside the VPN — otherwise Traefik's IPAllowList returns 403 and
    the CLI reports a false-positive failure."""
    if include_vpn:
        return False, None
    access = service.get("access")
    if access != "vpn":
        return False, None
    public_routes = service.get("public_routes") or []
    if not public_routes:
        return True, "VPN-only"
    probe_path = service.get("healthcheck_path") or "/"
    if _path_covered_by_public_routes(probe_path, public_routes):
        return False, None
    return True, "VPN-only (healthcheck_path not in public_routes)"


# requests folds DNS failure into ConnectionError, so the only way to tell
# "host does not exist" from "host refused the connection" is the wrapped
# resolver message. Matching is deliberately one-way safe: an unrecognised
# message falls through to the *retrying* classification, so a new libc
# wording costs a wasted wait, never a false FAIL.
_DNS_FAILURE_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated with hostname",
    "nameresolutionerror",
    "getaddrinfo failed",
)


def _check_once(url: str, *, session: requests.Session) -> tuple[int | None, list[str], str | None]:
    """Single HTTP GET attempt. Returns (status, redirect_chain, error).
    Status is None only if no response was received.

    Error strings carry a stable `<kind>: <detail>` prefix — `classify_failure`
    matches on it, and the CLI renders `error.split(":")[0]` as the badge."""
    try:
        resp = session.get(
            url,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            allow_redirects=True,
        )
        chain = [h.url for h in resp.history] + ([] if not resp.history else [resp.url])
        return resp.status_code, chain, None
    except Timeout:
        return None, [], "timeout"
    except SSLError as e:
        return None, [], f"TLS error: {e}"
    except RequestsConnectionError as e:
        text = str(e).lower()
        if any(marker in text for marker in _DNS_FAILURE_MARKERS):
            return None, [], f"DNS failure: {e}"
        return None, [], f"connection refused: {e}"
    except RequestException as e:
        return None, [], f"request error: {e}"


def classify_failure(status: int | None, error: str | None) -> str:
    """Bucket a failed attempt into `starting` / `definitive` / `fatal`.

    Only ever called on a non-2xx/3xx outcome. See the readiness-window
    comment at the top of this module for why each bucket exists."""
    if status is not None:
        return "starting" if status in _STARTING_STATUSES else "definitive"
    kind = (error or "").split(":", 1)[0].strip().lower()
    if kind == "dns failure":
        return "fatal"
    if kind in ("timeout", "connection refused"):
        return "starting"
    # "TLS error", "request error", and anything unforeseen: the connection got
    # far enough to fail in a specific way, so give it the short window only.
    return "definitive"


def _backoff_for(attempt: int) -> float:
    """Sleep before attempt N+1. Doubles from RETRY_BACKOFF_S, capped."""
    return min(RETRY_BACKOFF_S * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX_S)


def _attempt_cap(startup_budget_s: float) -> int:
    """Hard attempt ceiling for one probe.

    Sized to stay comfortably above the number of attempts the elapsed-time
    budget can actually fund (budget / max backoff), so it never truncates a
    legitimate wait — it exists only so the loop is provably finite."""
    return MAX_ATTEMPTS + int(max(startup_budget_s, 0.0) / RETRY_BACKOFF_MAX_S)


def check_domain(
    service_name: str,
    domain: str,
    path: str = "/",
    *,
    startup_budget_s: float = STARTUP_BUDGET_S,
) -> CheckResult:
    """Probe a single domain with retry. Pass = last attempt returned 2xx/3xx.

    `path` is appended verbatim to `https://{domain}` — it must already start
    with a slash (the schema enforces `^/.+` for `healthcheck_path`).

    Retries are bounded by elapsed wall-clock, and the bound depends on how the
    attempt failed: `startup_budget_s` for a still-booting upstream, a fixed
    ~10s for a definitive answer, none at all for an unresolvable host. A
    healthy service returns on attempt 1 and costs nothing extra."""
    url = f"https://{domain}{path}"
    start = time.monotonic()
    session = requests.Session()
    # A vanilla User-Agent avoids triggering WAF rules on some services.
    session.headers["User-Agent"] = "bay-healthcheck/1.0"

    status: int | None = None
    chain: list[str] = []
    error: str | None = None
    attempts = 0

    for attempt in range(1, _attempt_cap(startup_budget_s) + 1):
        attempts = attempt
        status, chain, error = _check_once(url, session=session)
        if status is not None and 200 <= status < 400:
            break
        kind = classify_failure(status, error)
        if kind == "fatal":
            break
        # Re-read the budget every attempt: a probe that starts as
        # "connection refused" and later gets a 404 has stopped being a
        # cold start, and the shorter window then applies retroactively.
        budget = startup_budget_s if kind == "starting" else DEFINITIVE_BUDGET_S
        backoff = _backoff_for(attempt)
        if (time.monotonic() - start) + backoff >= budget:
            break
        time.sleep(backoff)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    ok = status is not None and 200 <= status < 400
    return CheckResult(
        service=service_name,
        domain=domain,
        status=status,
        ok=ok,
        redirect_chain=chain,
        error=error,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        probed_url=url,
    )


# `{{ domain_base }}` is the documented multi-region idiom (services.yml tells
# operators to use it, with the value set per-region in group_vars/<region>/
# main.yml). But healthcheck parses services.yml as plain YAML — nothing here
# renders Jinja — so the template used to reach `check_domain` verbatim and get
# probed as `https://status.{{ domain_base }}/`, a guaranteed DNS failure
# reported as a red. Substitution is a literal regex ON PURPOSE: standing up a
# Jinja2 Environment to resolve these would drag in the filter-shadowing
# footgun (ansible's `default` shadows Jinja's and returns empty) for no gain.
#
# ANY per-region variable is resolved, not just domain_base. Hardcoding the one
# name meant a consumer who introduced a second per-region domain variable got
# its endpoints silently skipped: a consumer added `domain_base_next` for the
# domain migration, and `status.{{ domain_base_next }}` — a real, live,
# public endpoint — reported "unresolved template" on every deploy instead of
# being probed. A skipped check reads like a passing one at a glance, which is
# the worst way for a health probe to fail.
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_UNRENDERED_RE = re.compile(r"\{\{")

_UNRESOLVED_REASON = "unresolved template in domain"


def expand_domain(
    domain: str,
    svc_regions: list[str] | None,
    region_vars: dict[str, dict[str, str]],
) -> list[str]:
    """Expand `{{ var }}` references into one domain per applicable region.

    A domain that references no variable comes back unchanged — it must never
    be duplicated just because the service spans regions. A service with no
    `regions:` key deploys everywhere (docs/services.md), so it expands across
    every region that defines the variables it needs.

    A region that cannot resolve every reference in the domain is skipped, so a
    variable defined for `eu` but not `na` yields the eu domain rather than a
    half-substituted string. Anything still templated is returned as-is and
    caught by the caller's unresolved-template backstop, which skips rather
    than probes it.
    """
    names = set(_TEMPLATE_VAR_RE.findall(domain))
    if not names:
        return [domain]

    regions = list(svc_regions) if svc_regions else list(region_vars)
    expanded: list[str] = []
    for region in regions:
        values = region_vars.get(region)
        if not values or not names.issubset(values):
            continue
        candidate = _TEMPLATE_VAR_RE.sub(lambda m: values[m.group(1)], domain)
        if candidate not in expanded:
            expanded.append(candidate)
    return expanded or [domain]


def _collect_targets(
    services: dict[str, Any],
    include_vpn: bool,
    only: str | None = None,
    region_vars: dict[str, dict[str, str]] | None = None,
) -> list[tuple[str, str, str, bool, str | None]]:
    """Flatten services.yml into (service_name, domain, path, should_skip, skip_reason) tuples.

    `path` defaults to `/` when the service has no `healthcheck_path` set —
    preserving the previous behaviour. When set, it's the operator-declared
    route that exercises the dynamic backend."""
    rvars = region_vars or {}
    targets: list[tuple[str, str, str, bool, str | None]] = []
    for name, svc in services.items():
        if only is not None and name != only:
            continue
        if not isinstance(svc, dict):
            continue
        domains = svc.get("domains") or []
        if not domains:
            continue
        path = svc.get("healthcheck_path") or "/"
        skip, reason = should_skip_vpn_only(svc, include_vpn)
        regions = svc.get("regions")
        for domain in domains:
            for candidate in expand_domain(domain, regions, rvars):
                # Backstop: a typo, or a region that defines none of the
                # referenced variables, still arrives here templated.
                # Skipping keeps a config defect visible without inventing a
                # network failure for a hostname that never existed.
                if _UNRENDERED_RE.search(candidate):
                    targets.append((name, candidate, path, True, _UNRESOLVED_REASON))
                    continue
                targets.append((name, candidate, path, skip, reason))
    return targets


def startup_budget_for(service: dict[str, Any]) -> float:
    """Per-service readiness budget for the URL probe.

    Reuses `health_check_timeout` — the same services.yml knob rebuild.sh uses
    for its post-restart health poll (docs/build-pipeline.md). A service that
    declares 180 there because it is JVM/DB-warmup heavy is exactly the service
    whose URL needs more than 90s after a recreate, so a second, near-homonym
    key (`healthcheck_timeout`) would be a permanent typo trap for no gain.

    The override can only WIDEN the window. A consumer that set a *small* value
    for rebuild.sh's rollback poll must not silently make the post-deploy probe
    stricter than the framework default — that would re-introduce exactly the
    false-positive this budget exists to prevent."""
    raw = service.get("health_check_timeout")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return STARTUP_BUDGET_S
    return max(STARTUP_BUDGET_S, float(raw))


def run_healthcheck(
    services: dict[str, Any],
    *,
    include_vpn: bool = False,
    only: str | None = None,
    max_workers: int = 16,
    region_vars: dict[str, dict[str, str]] | None = None,
) -> list[CheckResult]:
    """Run the healthcheck in parallel. Returns one CheckResult per
    (service, domain) pair, in the order targets were collected.

    `region_vars` maps region -> its group_vars scalars
    (StackConfig.resolve_region_vars); without it, templated domains can't
    resolve and are skipped rather than probed.
    """
    targets = _collect_targets(
        services, include_vpn=include_vpn, only=only, region_vars=region_vars
    )
    if not targets:
        return []

    to_check = [(name, domain, path) for (name, domain, path, skip, _) in targets if not skip]

    budgets = {
        name: startup_budget_for(svc)
        for name, svc in services.items()
        if isinstance(svc, dict)
    }

    probe_by_domain: dict[tuple[str, str], CheckResult] = {}
    if to_check:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    check_domain,
                    name,
                    domain,
                    path,
                    startup_budget_s=budgets.get(name, STARTUP_BUDGET_S),
                ): (name, domain)
                for name, domain, path in to_check
            }
            for fut in concurrent.futures.as_completed(futures):
                name, domain = futures[fut]
                probe_by_domain[(name, domain)] = fut.result()

    # Rebuild in original target order so output is deterministic.
    out: list[CheckResult] = []
    for name, domain, _path, skip, reason in targets:
        if skip:
            out.append(
                CheckResult(
                    service=name,
                    domain=domain,
                    status=None,
                    ok=True,
                    skipped=True,
                    skip_reason=reason,
                )
            )
        else:
            out.append(probe_by_domain[(name, domain)])
    return out


def summarize(results: list[CheckResult]) -> dict[str, int]:
    total = len(results)
    skipped = sum(1 for r in results if r.skipped)
    passed = sum(1 for r in results if r.ok and not r.skipped)
    failed = sum(1 for r in results if not r.ok)
    return {"total": total, "passed": passed, "failed": failed, "skipped": skipped}
