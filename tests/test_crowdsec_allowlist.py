"""Tests for the crowdsec_allowlist role.

Exercises four scenarios via the crowdsec_allowlist_sync.py library module:

1. Empty peer list (sandbox degenerate case) — allowlist is created with no IPs.
2. Malformed cscli inspect JSON — treated as empty current list; all desired
   IPs are added.
3. cscli not found (rc != 0) — sync_allowlist raises RuntimeError with a
   descriptive message.
4. Stale-entry pruning — only missing IPs are added; only stale IPs are
   removed; unchanged IPs are untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

# The library module lives under roles/crowdsec_allowlist/library/
sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "roles"
        / "crowdsec_allowlist"
        / "library"
    ),
)

from crowdsec_allowlist_sync import (
    compute_desired_ips,
    cscli_allowlist_inspect,
    cscli_version,
    sync_allowlist,
    version_gte,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_run_fn(responses: list[tuple[int, str]]):
    """Return a callable that mimics subprocess.run, returning pre-canned results.

    responses is a list of (returncode, stdout) tuples consumed in order.
    """
    responses_iter = iter(responses)

    def _run(cmd, **kwargs):
        rc, stdout = next(responses_iter)
        result = MagicMock()
        result.returncode = rc
        result.stdout = stdout
        result.stderr = ""
        return result

    return _run


def _version_json(version: str = "v1.6.2") -> str:
    return json.dumps({"version": version, "build_date": "2024-01-01"})


def _inspect_json(ips: list[str]) -> str:
    return json.dumps(
        {
            "name": "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            "description": "Peer hosts from Ansible inventory",
            "items": [{"value": ip, "description": f"peer {ip}"} for ip in ips],
        }
    )


# ── cscli_version parse tests (plain-text + JSON) ─────────────────────────────


class TestCscliVersionParse:
    """CrowdSec 1.7.x ignores `-o json` on the version subcommand and always
    emits human-readable text. The parser must handle both forms."""

    def _fake_run(self, stdout: str, rc: int = 0):
        def _run(*args, **kwargs):
            r = type("R", (), {})()
            r.returncode = rc
            r.stdout = stdout
            r.stderr = ""
            return r
        return _run

    def test_parses_plaintext_output(self) -> None:
        stdout = (
            "version: v1.7.6-debian-pragmatic-amd64-eacc8192\n"
            "Codename: alphaga\n"
            "BuildDate: 2026-01-23_11:10:24\n"
        )
        rc, ver = cscli_version(run_fn=self._fake_run(stdout))
        assert rc == 0
        assert ver == "1.7.6"

    def test_parses_legacy_json_output(self) -> None:
        stdout = '{"version": "v1.6.2", "build_date": "2024-01-01"}'
        rc, ver = cscli_version(run_fn=self._fake_run(stdout))
        assert rc == 0
        assert ver == "1.6.2"

    def test_empty_stdout_returns_nonzero(self) -> None:
        rc, ver = cscli_version(run_fn=self._fake_run(""))
        assert rc != 0
        assert ver == ""

    def test_command_failure_propagates(self) -> None:
        rc, ver = cscli_version(run_fn=self._fake_run("", rc=127))
        assert rc == 127
        assert ver == ""


# ── version_gte unit tests ────────────────────────────────────────────────────


class TestVersionGte:
    def test_same_version(self) -> None:
        assert version_gte("1.5.0", "1.5.0") is True

    def test_newer(self) -> None:
        assert version_gte("1.6.2", "1.5.0") is True

    def test_older(self) -> None:
        assert version_gte("1.4.9", "1.5.0") is False

    def test_empty_string(self) -> None:
        assert version_gte("", "1.5.0") is False


# ── compute_desired_ips unit tests ────────────────────────────────────────────


class TestComputeDesiredIps:
    """Mirrors the Jinja block in tasks/main.yml: self-inclusion, the
    ansible_host→inventory-hostname fallback, netplan_address pickup, and the
    IP-shaped filter that drops DNS host names."""

    def test_empty_group(self) -> None:
        assert compute_desired_ips([], {}) == []

    def test_public_ips_extracted(self) -> None:
        hostnames = ["node-a", "node-b"]
        hostvars = {
            "node-a": {"ansible_host": "203.0.113.11"},
            "node-b": {"ansible_host": "203.0.113.12"},
        }
        result = compute_desired_ips(hostnames, hostvars)
        assert "203.0.113.11" in result
        assert "203.0.113.12" in result

    def test_tailnet_ips_extracted(self) -> None:
        hostnames = ["node-a"]
        hostvars = {
            "node-a": {
                "ansible_host": "203.0.113.11",
                "headscale_server_tailnet_ip": "100.64.0.1",
            },
        }
        result = compute_desired_ips(hostnames, hostvars)
        assert "203.0.113.11" in result
        assert "100.64.0.1" in result

    def test_bare_ip_inventory_fallback(self) -> None:
        """Bug #2: no ansible_host key (bare-IP inventory) → the inventory
        hostname, which IS the public IP, is used instead of being dropped."""
        hostnames = ["203.0.113.11"]
        hostvars = {"203.0.113.11": {}}
        assert compute_desired_ips(hostnames, hostvars) == ["203.0.113.11"]

    def test_ansible_host_wins_over_hostname_fallback(self) -> None:
        """Explicit ansible_host takes precedence over the hostname fallback."""
        hostnames = ["node-a"]
        hostvars = {"node-a": {"ansible_host": "203.0.113.7"}}
        result = compute_desired_ips(hostnames, hostvars)
        assert result == ["203.0.113.7"]
        assert "node-a" not in result

    def test_netplan_address_picked_up(self) -> None:
        """netplan_address (the IP that got banned in the incident) is
        collected even when it differs from the hostname."""
        hostnames = ["10.0.0.1"]
        hostvars = {"10.0.0.1": {"netplan_address": "203.0.113.14"}}
        result = compute_desired_ips(hostnames, hostvars)
        assert "203.0.113.14" in result

    def test_self_is_included(self) -> None:
        """Bug #1: inventory_hostname is a group member and its own IPs must
        land in the allowlist — otherwise the host can ban itself."""
        hostnames = ["203.0.113.14"]  # this host == inventory_hostname
        hostvars = {
            "203.0.113.14": {
                "netplan_address": "203.0.113.14",
                "headscale_server_tailnet_ip": "100.64.0.5",
            },
        }
        result = compute_desired_ips(hostnames, hostvars)
        assert "203.0.113.14" in result
        assert "100.64.0.5" in result

    def test_non_ip_hostname_skipped(self) -> None:
        """A DNS-name host with no explicit IP vars contributes nothing —
        cscli allowlists add takes IPs/CIDRs, not names."""
        hostnames = ["web-01.example.com"]
        hostvars = {"web-01.example.com": {}}
        assert compute_desired_ips(hostnames, hostvars) == []

    def test_dedupe_hostname_equals_netplan(self) -> None:
        """ansible_host fallback (the bare-IP hostname) == netplan_address for
        the same host: the shared value appears exactly once."""
        hostnames = ["203.0.113.14"]
        hostvars = {"203.0.113.14": {"netplan_address": "203.0.113.14"}}
        result = compute_desired_ips(hostnames, hostvars)
        assert result.count("203.0.113.14") == 1

    def test_ipv6_kept(self) -> None:
        """Values containing ':' (IPv6/CIDR) pass the filter."""
        hostnames = ["node-a"]
        hostvars = {"node-a": {"ansible_host": "2001:db8::1"}}
        assert compute_desired_ips(hostnames, hostvars) == ["2001:db8::1"]

    def test_acme_full_shape_all_six_ips(self) -> None:
        """Full demo-shaped bare-IP inventory ([eu]/[na]/[infra], each a
        single-IP host with netplan_address == hostname + a tailnet IP): all
        six IPs land, self included."""
        hostnames = ["203.0.113.11", "203.0.113.12", "203.0.113.14"]
        hostvars = {
            "203.0.113.11": {
                "netplan_address": "203.0.113.11",
                "headscale_server_tailnet_ip": "100.64.0.1",
            },
            "203.0.113.12": {
                "netplan_address": "203.0.113.12",
                "headscale_server_tailnet_ip": "100.64.0.2",
            },
            "203.0.113.14": {
                "netplan_address": "203.0.113.14",
                "headscale_server_tailnet_ip": "100.64.0.5",
            },
        }
        result = compute_desired_ips(hostnames, hostvars)
        for ip in (
            "203.0.113.14",
            "100.64.0.5",
            "203.0.113.11",
            "100.64.0.1",
            "203.0.113.12",
            "100.64.0.2",
        ):
            assert ip in result, f"{ip} missing from {result}"
        assert len(result) == 6


# ── Case 1: Empty peer list ───────────────────────────────────────────────────


class TestEmptyPeerList:
    """Sandbox degenerate case: single-host inventory, no peers."""

    def test_allowlist_created_with_zero_ips(self) -> None:
        """cscli allowlists create is called; cscli allowlists add is never called."""
        calls: list = []

        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            # version -> inspect -> (no add/remove)
            if "version" in cmd:
                result.stdout = _version_json()
            elif "inspect" in cmd:
                result.stdout = _inspect_json([])
            else:
                result.stdout = ""
            return result

        outcome = sync_allowlist("argo-inventory", desired_ips=[], run_fn=_run)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately

        assert outcome["added"] == []
        assert outcome["removed"] == []

        cmd_names = [c[2] if len(c) > 2 else "" for c in calls]
        assert "create" in cmd_names, "allowlists create was not called"
        assert "add" not in cmd_names, (
            f"cscli allowlists add should not be called with empty peer list, "
            f"but got calls: {cmd_names}"
        )


# ── Case 2: Malformed cscli JSON ─────────────────────────────────────────────


class TestMalformedJson:
    """cscli allowlists inspect returns garbage — role must not crash."""

    def test_malformed_inspect_treated_as_empty(self) -> None:
        """Malformed inspect JSON → current list treated as empty → all desired IPs added."""

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "version" in cmd:
                result.stdout = _version_json()
            elif "inspect" in cmd:
                result.stdout = "not valid json {"
            else:
                result.stdout = ""
            return result

        # Should not raise
        outcome = sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.5"],
            run_fn=_run,
        )
        assert "10.0.0.5" in outcome["added"], (
            "Expected 10.0.0.5 to be added when inspect JSON is malformed"
        )

    def test_empty_inspect_stdout_treated_as_empty(self) -> None:
        """Empty inspect stdout → current list treated as empty → desired IPs added."""

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "version" in cmd:
                result.stdout = _version_json()
            elif "inspect" in cmd:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        outcome = sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.6"],
            run_fn=_run,
        )
        assert "10.0.0.6" in outcome["added"]

    def test_inspect_nonzero_rc_treated_as_empty(self) -> None:
        """Non-zero inspect rc → current list treated as empty → desired IPs added."""

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.stderr = ""
            if "version" in cmd:
                result.returncode = 0
                result.stdout = _version_json()
            elif "inspect" in cmd:
                result.returncode = 1
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = ""
            return result

        outcome = sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.7"],
            run_fn=_run,
        )
        assert "10.0.0.7" in outcome["added"]

    def test_inspect_direct_parse_fail(self) -> None:
        """cscli_allowlist_inspect returns [] on bad JSON directly."""
        def _bad_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "{{{invalid"
            result.stderr = ""
            return result

        result = cscli_allowlist_inspect("argo-inventory", run_fn=_bad_run)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
        assert result == []


# ── Case 3: cscli not found ───────────────────────────────────────────────────


class TestCscliNotFound:
    """cscli not available — sync_allowlist raises RuntimeError."""

    def test_raises_on_nonzero_version_rc(self) -> None:
        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 127
            result.stdout = ""
            result.stderr = "cscli: command not found"
            return result

        with pytest.raises(RuntimeError, match="CrowdSec >= 1.5.0"):
            sync_allowlist("argo-inventory", desired_ips=["1.2.3.4"], run_fn=_run)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately

    def test_raises_on_old_version(self) -> None:
        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = _version_json("v1.4.9")
            result.stderr = ""
            return result

        with pytest.raises(RuntimeError, match="CrowdSec >= 1.5.0"):
            sync_allowlist("argo-inventory", desired_ips=["1.2.3.4"], run_fn=_run)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately

    def test_minimum_supported_version_passes(self) -> None:
        """CrowdSec 1.5.0 exactly should not raise."""

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "version" in cmd:
                result.stdout = _version_json("v1.5.0")
            elif "inspect" in cmd:
                result.stdout = _inspect_json([])
            else:
                result.stdout = ""
            return result

        # Should not raise
        sync_allowlist("argo-inventory", desired_ips=[], run_fn=_run)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately


# ── Case 4: Stale-entry pruning ───────────────────────────────────────────────


class TestStaleEntryPruning:
    """Current list has 10.0.0.1 and 10.0.0.2; desired has 10.0.0.2 and 10.0.0.3.

    Expected:
    - cscli allowlists remove argo-inventory 10.0.0.1   (stale)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
    - cscli allowlists add argo-inventory 10.0.0.3       (missing)  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
    - 10.0.0.2 is NOT touched (already present)
    """

    def setup_method(self):
        self._add_calls: list[str] = []
        self._remove_calls: list[str] = []

        def _run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if "version" in cmd:
                result.stdout = _version_json()
            elif "inspect" in cmd:
                result.stdout = _inspect_json(["10.0.0.1", "10.0.0.2"])
            elif "add" in cmd:
                # cmd is e.g. ["cscli", "allowlists", "add", "argo-inventory", "<ip>", ...]  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
                ip_idx = cmd.index("add") + 2
                self._add_calls.append(cmd[ip_idx])
                result.stdout = ""
            elif "remove" in cmd:
                ip_idx = cmd.index("remove") + 2
                self._remove_calls.append(cmd[ip_idx])
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        self._run_fn = _run

    def test_stale_ip_is_removed(self) -> None:
        sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.2", "10.0.0.3"],
            run_fn=self._run_fn,
        )
        assert "10.0.0.1" in self._remove_calls, (
            "Expected 10.0.0.1 (stale) to be removed"
        )

    def test_missing_ip_is_added(self) -> None:
        sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.2", "10.0.0.3"],
            run_fn=self._run_fn,
        )
        assert "10.0.0.3" in self._add_calls, (
            "Expected 10.0.0.3 (missing) to be added"
        )

    def test_unchanged_ip_not_touched(self) -> None:
        sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.2", "10.0.0.3"],
            run_fn=self._run_fn,
        )
        assert "10.0.0.2" not in self._add_calls, (
            "10.0.0.2 is already present — should not be added again"
        )
        assert "10.0.0.2" not in self._remove_calls, (
            "10.0.0.2 is in desired list — should not be removed"
        )

    def test_sync_returns_correct_sets(self) -> None:
        outcome = sync_allowlist(
            "argo-inventory",  # legacy-argo: live CrowdSec allowlist name on hosts, migrate separately
            desired_ips=["10.0.0.2", "10.0.0.3"],
            run_fn=self._run_fn,
        )
        assert "10.0.0.3" in outcome["added"]
        assert "10.0.0.1" in outcome["removed"]
        assert "10.0.0.2" not in outcome["added"]
        assert "10.0.0.2" not in outcome["removed"]
