"""
crowdsec_allowlist_sync — pure Python helper that mirrors the set-diff logic
from the Ansible tasks.  Used as a test vehicle; NOT a runtime dependency
(the Ansible tasks work without it).

The module is importable by tests without any Ansible runtime.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Mapping, Sequence


# CrowdSec 1.7.x ignores `-o json` for `cscli version` and always emits
# human-readable text. Regex parses both forms — the literal `"version":`
# substring in hypothetical JSON output still matches.
_VERSION_RE = re.compile(r'version["\s:]+v?(\d+\.\d+\.\d+)')

# Matches an IPv4 dotted-quad. Mirrors the Jinja `is match('^\d{1,3}...')`
# filter in tasks/main.yml; IPv6/CIDR values are accepted separately by the
# presence of a ':'. Inventory hostnames that are DNS names match neither and
# are dropped (cscli allowlists add takes IPs/CIDRs, not names).
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


# ---------------------------------------------------------------------------
# Low-level cscli wrappers
# ---------------------------------------------------------------------------


def cscli_version(run_fn=subprocess.run) -> tuple[int, str]:
    """Return (returncode, version_string_without_leading_v).

    Parses the `cscli version` output with a regex that works on both the
    human-readable form (`version: v1.7.6-...`) and the legacy JSON form
    (`{"version": "v1.7.6", ...}`). On failure rc is non-zero and
    version_string is ''.
    """
    result = run_fn(
        ["cscli", "version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return result.returncode, ""
    match = _VERSION_RE.search(result.stdout or "")
    if not match:
        return 1, ""
    return 0, match.group(1)


def version_gte(version_str: str, minimum: str) -> bool:
    """Return True if version_str >= minimum (semver comparison)."""
    if not version_str:
        return False

    def _parse(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".")[:3])
        except ValueError:
            return (0, 0, 0)

    return _parse(version_str) >= _parse(minimum)


def cscli_allowlist_create(name: str, run_fn=subprocess.run) -> int:
    """Create allowlist; returns 0 on success, ignores 'already exists'."""
    result = run_fn(
        ["cscli", "allowlists", "create", name, "-d", "Peer hosts from Ansible inventory"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        combined = (result.stderr or "") + (result.stdout or "")
        if "already exists" in combined:
            return 0
    return result.returncode


def cscli_allowlist_inspect(name: str, run_fn=subprocess.run) -> list[str]:
    """Return list of IPs currently in the named allowlist.

    Returns empty list on any error (not found, parse failure, cscli missing).
    """
    result = run_fn(
        ["cscli", "allowlists", "inspect", name, "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
        items = data.get("items") or []
        return [item["value"] for item in items if "value" in item]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def cscli_allowlist_add(name: str, ip: str, run_fn=subprocess.run) -> int:
    """Add a single IP to the allowlist. Returns cscli return code."""
    result = run_fn(
        ["cscli", "allowlists", "add", name, ip, "-d", f"Argo peer {ip}"],  # legacy-argo: live CrowdSec allowlist entry description on hosts
        capture_output=True,
        text=True,
    )
    return result.returncode


def cscli_allowlist_remove(name: str, ip: str, run_fn=subprocess.run) -> int:
    """Remove a single IP from the allowlist. Returns cscli return code."""
    result = run_fn(
        ["cscli", "allowlists", "remove", name, ip],
        capture_output=True,
        text=True,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# High-level sync logic (mirrors the Ansible task sequence)
# ---------------------------------------------------------------------------


def _looks_like_ip(value: str) -> bool:
    """True if value is an IPv4 dotted-quad or contains ':' (IPv6/CIDR)."""
    return bool(value) and (_IPV4_RE.match(value) is not None or ":" in value)


def _gateway_bind_ip(hostvars_entry) -> str:
    """Resolve one host's access-gateway overlay IP from its hostvars.

    Mirrors `bay_gateway_bind_ip` in filter_plugins/bay_filters.py. It is
    duplicated rather than imported because an Ansible library module runs in
    its own interpreter on the target and cannot import filter plugins. Both
    copies are allowlisted by tests/test_gateway_ratchet.py as backend-owned
    resolvers, and tests/test_gateway_contract.py asserts they agree.

    Precedence: the neutral `gateway_bind_ip` if a consumer set it in
    group_vars, else the incumbent `headscale_server_tailnet_ip` (honoured
    indefinitely so the M107 migration is zero-config-change), else "".
    """
    hv = hostvars_entry or {}
    for key in ("gateway_bind_ip", "headscale_server_tailnet_ip"):
        value = hv.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def compute_desired_ips(
    hostnames: Sequence[str],
    hostvars: Mapping[str, Mapping[str, str]],
) -> list[str]:
    """Compute the desired allowlist IPs from an inventory group.

    Mirrors the Jinja block in tasks/main.yml exactly. For every host name in
    `hostnames` — inventory_hostname INCLUDED, since a host must allowlist its
    own IPs or hairpin/self-traffic can make it ban itself (2026-07-01 infra
    self-ban incident) — collect in priority order:

      - hostvars[h]['ansible_host'] if set, else the host name h itself (in a
        bare-IP inventory the host name IS the public IP)
      - hostvars[h]['netplan_address'] if set (framework-canonical public IP)
      - the access-gateway overlay IP via _gateway_bind_ip (empty under
        `access_gateway: none`, so nothing is exempted)

    Keep only IP-shaped values (IPv4 dotted-quad or containing ':'), so DNS
    host names are dropped. Returns a deduplicated list preserving first-seen
    order.
    """
    ips: list[str] = []
    for h in hostnames:
        hv = hostvars.get(h) or {}
        candidates = [
            hv.get("ansible_host") or h,
            hv.get("netplan_address") or "",
            _gateway_bind_ip(hv),
        ]
        for cand in candidates:
            if _looks_like_ip(cand) and cand not in ips:
                ips.append(cand)
    return ips


def sync_allowlist(
    allowlist_name: str,
    desired_ips: Sequence[str],
    run_fn=subprocess.run,
) -> dict[str, list[str]]:
    """Full-sync the named CrowdSec allowlist.

    Returns a dict with keys 'added' and 'removed' listing IPs that were
    actually mutated.

    Raises RuntimeError if cscli is not found or version < 1.5.0.
    Raises RuntimeError if allowlist creation fails.
    """
    rc, version = cscli_version(run_fn=run_fn)
    if rc != 0 or not version_gte(version, "1.5.0"):
        raise RuntimeError(
            f"crowdsec_allowlist requires CrowdSec >= 1.5.0 "
            f"(got rc={rc}, version={version!r})"
        )

    create_rc = cscli_allowlist_create(allowlist_name, run_fn=run_fn)
    if create_rc != 0:
        raise RuntimeError(
            f"Failed to create allowlist '{allowlist_name}' (rc={create_rc})"
        )

    current_ips = cscli_allowlist_inspect(allowlist_name, run_fn=run_fn)
    desired_set = set(desired_ips)
    current_set = set(current_ips)

    to_add = sorted(desired_set - current_set)
    to_remove = sorted(current_set - desired_set)

    added: list[str] = []
    for ip in to_add:
        if cscli_allowlist_add(allowlist_name, ip, run_fn=run_fn) == 0:
            added.append(ip)

    removed: list[str] = []
    for ip in to_remove:
        if cscli_allowlist_remove(allowlist_name, ip, run_fn=run_fn) == 0:
            removed.append(ip)

    return {"added": added, "removed": removed}
