"""Tests for the gateway CLI command.

Covers input validation, inventory parsing, ansible output extraction,
config loading, headscale requirement checking, and — via a recording
backend stub — the arguments commands actually hand to headscale.
"""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.exceptions import Exit as ClickExit
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.commands import gateway
from bay_cli.commands.gateway import (
    _EXPIRATION_RE,
    _USERNAME_RE,
    _audit_acl_reachability,
    _extract_output_from_ansible,
    _find_acl_policy_file,
    _format_timestamp,
    _get_control_host,
    _get_gateway_config,
    _node_ip,
    _require_headscale,
    _strip_ansi,
    _validate_username,
)
from bay_cli.errors import BayError

runner = CliRunner()

# ── _validate_username ────────────────────────────────────────────────────


class TestValidateUsername:
    """_validate_username accepts alphanum/hyphen/underscore, rejects all else."""

    @pytest.mark.parametrize("name", [
        "alice",
        "bob-laptop",
        "user_1",
        "Admin",
        "device123",
        "a",
        "A-B_c-D",
    ])
    def test_valid_usernames(self, name: str) -> None:
        # Should not raise
        _validate_username(name)

    @pytest.mark.parametrize("name", [
        "has space",
        "semi;colon",
        "pipe|char",
        "dollar$sign",
        "back`tick",
        "with.dot",
        "slash/name",
        "at@sign",
        "",
    ])
    def test_invalid_usernames(self, name: str) -> None:
        with pytest.raises(ClickExit):
            _validate_username(name)


# ── _EXPIRATION_RE ────────────────────────────────────────────────────────


class TestUsernameRegex:
    """_USERNAME_RE gates values that get interpolated into shell commands."""

    @pytest.mark.parametrize("value", ["alice", "a-b_1", "NODE01", "x"])
    def test_valid_usernames(self, value: str) -> None:
        assert _USERNAME_RE.match(value) is not None

    @pytest.mark.parametrize("value", [
        "",
        "a b",
        "a;rm -rf /",
        "a$(id)",
        "a`id`",
        # `$` matched before a trailing newline, so this passed until \Z.
        "alice\n",
    ])
    def test_invalid_usernames(self, value: str) -> None:
        assert _USERNAME_RE.match(value) is None


class TestExpirationRegex:
    """_EXPIRATION_RE mirrors Prometheus ParseDuration, as headscale uses.

    The accept/reject sets below were verified against a live headscale v0.28
    (`preauthkeys create --expiration <v>` against a nonexistent user, so a
    duration error is distinguishable from "user not found").
    """

    @pytest.mark.parametrize("value", [
        "30d",
        "1h",
        "12m",
        "1y",
        "90d",
        "24h",
        "365d",
        # Units headscale accepts that the old ^\d+[dhmy]$ wrongly rejected.
        "2w",
        "1s",
        "1ms",
        # Compound, largest unit first.
        "1h30m",
        "2w3d",
        # ParseDuration special-cases a bare zero; we used to be stricter than
        # the server it validates for.
        "0",
        "0h",
    ])
    def test_valid_expirations(self, value: str) -> None:
        assert _EXPIRATION_RE.match(value) is not None

    @pytest.mark.parametrize("value", [
        "30",
        "d30",
        "abc",
        "",
        "1x",
        "d",
        "10 d",
        "1.5h",
        # headscale requires descending unit order.
        "30m1h",
        # Only a bare "0" is special-cased, not any run of zeros.
        "00",
        # Trailing newline: `$` accepted these, `\Z` does not. The value is
        # interpolated into a shell command, so the charset must be closed.
        "24h\n",
        "0\n",
    ])
    def test_invalid_expirations(self, value: str) -> None:
        assert _EXPIRATION_RE.match(value) is None


# ── _strip_ansi ───────────────────────────────────────────────────────────


class TestStripAnsi:
    """_strip_ansi removes ANSI escape sequences, preserves clean text."""

    def test_removes_color_codes(self) -> None:
        text = "\x1b[32mhello\x1b[0m world"
        assert _strip_ansi(text) == "hello world"

    def test_removes_bold_codes(self) -> None:
        text = "\x1b[1mbold\x1b[0m"
        assert _strip_ansi(text) == "bold"

    def test_preserves_clean_text(self) -> None:
        text = "no ansi here"
        assert _strip_ansi(text) == "no ansi here"

    def test_empty_string(self) -> None:
        assert _strip_ansi("") == ""

    def test_multiple_sequences(self) -> None:
        text = "\x1b[31mred\x1b[0m and \x1b[34mblue\x1b[0m"
        assert _strip_ansi(text) == "red and blue"


# ── _extract_output_from_ansible ──────────────────────────────────────────


class TestExtractOutputFromAnsible:
    """_extract_output_from_ansible extracts command output from ad-hoc responses."""

    def test_standard_success_output(self) -> None:
        stdout = "hostname | SUCCESS | rc=0 >>\nsome output"
        assert _extract_output_from_ansible(stdout) == "some output"

    def test_multiline_output(self) -> None:
        stdout = "hostname | SUCCESS | rc=0 >>\nline1\nline2\nline3"
        assert _extract_output_from_ansible(stdout) == "line1\nline2\nline3"

    def test_output_with_ansi_codes(self) -> None:
        stdout = "\x1b[32mhostname | SUCCESS | rc=0 >>\x1b[0m\nclean output"
        assert _extract_output_from_ansible(stdout) == "clean output"

    def test_no_marker_returns_cleaned_full_string(self) -> None:
        stdout = "just some plain text"
        assert _extract_output_from_ansible(stdout) == "just some plain text"

    def test_no_marker_with_ansi(self) -> None:
        stdout = "\x1b[31mcolored text\x1b[0m"
        assert _extract_output_from_ansible(stdout) == "colored text"

    def test_empty_output_after_marker(self) -> None:
        stdout = "hostname | SUCCESS | rc=0 >>\n"
        assert _extract_output_from_ansible(stdout) == ""

    def test_whitespace_stripping(self) -> None:
        stdout = "hostname | SUCCESS | rc=0 >>\n  spaced output  \n"
        assert _extract_output_from_ansible(stdout) == "spaced output"


# ── _get_control_host — inventory parsing ─────────────────────────────────


def _make_consumer(tmp_path: Path) -> Path:
    """Create a minimal consumer directory structure and return bay_dir."""
    bay_dir = tmp_path / ".bay"
    bay_dir.mkdir()
    (tmp_path / "hosts").mkdir()
    (tmp_path / "group_vars" / "all").mkdir(parents=True)
    return bay_dir


class TestGetControlHostSingleServer:
    """Single-server inventory — no children groups, returns None."""

    def test_single_server_returns_none(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(
            "[production]\n10.0.0.1\n"
        )
        assert _get_control_host(bay_dir) is None

    def test_single_server_with_region_returns_none(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(
            "[production]\n10.0.0.1\n"
        )
        assert _get_control_host(bay_dir, region="eu") is None

    def test_missing_inventory_returns_none(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        # No hosts/production file at all
        assert _get_control_host(bay_dir) is None


class TestGetControlHostMultiRegion:
    """Multi-region inventory with [production:children]."""

    _MULTI_REGION_INV = textwrap.dedent("""\
        [production:children]
        eu
        na

        [eu]
        1.2.3.4

        [na]
        5.6.7.8
    """)

    def test_no_region_no_config_returns_first_child(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        # No config file → no headscale_control_region → fallback to first child
        result = _get_control_host(bay_dir)
        assert result == "1.2.3.4"

    def test_region_eu_returns_eu_host(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        assert _get_control_host(bay_dir, region="eu") == "1.2.3.4"

    def test_region_na_returns_na_host(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        assert _get_control_host(bay_dir, region="na") == "5.6.7.8"

    def test_unknown_region_returns_none(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        assert _get_control_host(bay_dir, region="unknown") is None

    def test_headscale_control_region_in_config(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            "---\naccess_gateway: headscale\nheadscale_control_region: na\n"
        )
        # No explicit --region, should use config's headscale_control_region
        result = _get_control_host(bay_dir)
        assert result == "5.6.7.8"

    def test_explicit_region_overrides_config(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(self._MULTI_REGION_INV)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            "---\naccess_gateway: headscale\nheadscale_control_region: na\n"
        )
        # Explicit --region=eu overrides the config's na
        assert _get_control_host(bay_dir, region="eu") == "1.2.3.4"

    def test_children_before_group_defs(self, tmp_path: Path) -> None:
        """[production:children] appearing before group definitions works."""
        inv = textwrap.dedent("""\
            [production:children]
            west
            east

            [west]
            10.10.10.1

            [east]
            10.10.10.2
        """)
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(inv)
        assert _get_control_host(bay_dir, region="east") == "10.10.10.2"

    def test_multiple_hosts_per_group_uses_first(self, tmp_path: Path) -> None:
        """When a group has multiple hosts, the first is used."""
        inv = textwrap.dedent("""\
            [production:children]
            eu

            [eu]
            1.1.1.1
            2.2.2.2
            3.3.3.3
        """)
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "hosts" / "production").write_text(inv)
        assert _get_control_host(bay_dir, region="eu") == "1.1.1.1"


# ── _get_gateway_config ──────────────────────────────────────────────────


class TestGetGatewayConfig:
    """_get_gateway_config reads and merges config from consumer group_vars."""

    def test_defaults_when_no_files_exist(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        config = _get_gateway_config(bay_dir)
        assert config["access_gateway"] == "wireguard"
        assert config["vpn_allowed_ips"] == []
        assert config["headscale_domain"] == ""
        assert config["headscale_tailnet_cidr"] == ["100.64.0.0/10"]
        assert config["headscale_control_region"] == ""

    def test_config_from_access_gateway_yml(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            textwrap.dedent("""\
                ---
                access_gateway: headscale
                headscale_domain: vpn.example.com
                headscale_tailnet_cidr:
                  - 100.64.0.0/10
                  - fd7a:115c:a1e0::/48
                headscale_control_region: eu
            """)
        )
        config = _get_gateway_config(bay_dir)
        assert config["access_gateway"] == "headscale"
        assert config["headscale_domain"] == "vpn.example.com"
        assert config["headscale_tailnet_cidr"] == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
        assert config["headscale_control_region"] == "eu"

    def test_config_from_main_yml_fallback(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        # No access_gateway.yml, but main.yml has the key
        (tmp_path / "group_vars" / "all" / "main.yml").write_text(
            textwrap.dedent("""\
                ---
                access_gateway: headscale
                headscale_domain: gw.example.com
            """)
        )
        config = _get_gateway_config(bay_dir)
        assert config["access_gateway"] == "headscale"
        assert config["headscale_domain"] == "gw.example.com"

    def test_vpn_allowed_ips_from_vpn_access_yml(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "vpn_access.yml").write_text(
            textwrap.dedent("""\
                ---
                vpn_allowed_ips:
                  - 10.0.0.0/8
                  - 192.168.1.0/24
            """)
        )
        config = _get_gateway_config(bay_dir)
        assert config["vpn_allowed_ips"] == ["10.0.0.0/8", "192.168.1.0/24"]

    def test_access_gateway_yml_takes_precedence_over_main(self, tmp_path: Path) -> None:
        """access_gateway.yml is read first so its values win over main.yml."""
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            "---\naccess_gateway: headscale\nheadscale_domain: from-ag.example.com\n"
        )
        (tmp_path / "group_vars" / "all" / "main.yml").write_text(
            "---\naccess_gateway: wireguard\nheadscale_domain: from-main.example.com\n"
        )
        config = _get_gateway_config(bay_dir)
        # Both files are read in order; main.yml overwrites access_gateway.yml
        # because the loop iterates candidates sequentially and overwrites.
        # access_gateway.yml is first in the list, main.yml is second — main wins.
        assert config["access_gateway"] == "wireguard"
        assert config["headscale_domain"] == "from-main.example.com"

    def test_vpn_access_overrides_vpn_allowed_ips_from_other_files(self, tmp_path: Path) -> None:
        """vpn_access.yml always wins for vpn_allowed_ips regardless of other files."""
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            "---\nvpn_allowed_ips:\n  - 1.1.1.1/32\n"
        )
        (tmp_path / "group_vars" / "all" / "vpn_access.yml").write_text(
            "---\nvpn_allowed_ips:\n  - 2.2.2.2/32\n"
        )
        config = _get_gateway_config(bay_dir)
        # vpn_access.yml always overrides vpn_allowed_ips
        assert config["vpn_allowed_ips"] == ["2.2.2.2/32"]

    def test_empty_yaml_file_does_not_crash(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text("---\n")
        config = _get_gateway_config(bay_dir)
        # Should return defaults without crashing
        assert config["access_gateway"] == "wireguard"


# ── _require_headscale ────────────────────────────────────────────────────


class TestRequireHeadscale:
    """_require_headscale exits when gateway type is not headscale."""

    def test_headscale_config_passes(self) -> None:
        config = {"access_gateway": "headscale"}
        # Should not raise
        _require_headscale(config)

    def test_wireguard_config_exits(self) -> None:
        config = {"access_gateway": "wireguard"}
        with pytest.raises(ClickExit):
            _require_headscale(config)

    def test_missing_key_exits(self) -> None:
        config = {}
        with pytest.raises(ClickExit):
            _require_headscale(config)

    def test_none_value_exits(self) -> None:
        config = {"access_gateway": None}
        with pytest.raises(ClickExit):
            _require_headscale(config)

    def test_unknown_gateway_type_exits(self) -> None:
        config = {"access_gateway": "tailscale"}
        with pytest.raises(ClickExit):
            _require_headscale(config)


# ── _find_acl_policy_file ─────────────────────────────────────────────────


class TestFindAclPolicyFile:
    """_find_acl_policy_file locates the group_vars file defining a default-deny policy."""

    def test_none_when_no_policy(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "access_gateway.yml").write_text(
            "---\naccess_gateway: headscale\n"
        )
        assert _find_acl_policy_file(bay_dir) is None

    def test_finds_policy_in_dedicated_file(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        policy = tmp_path / "group_vars" / "all" / "headscale_acl.yml"
        policy.write_text(
            textwrap.dedent("""\
                ---
                headscale_acl_policy:
                  hosts:
                    infra: 100.64.0.5/32
                  acls:
                    - action: accept
                      src: ["*"]
                      dst: ["infra:*"]
            """)
        )
        assert _find_acl_policy_file(bay_dir) == policy

    def test_finds_policy_in_nested_env_dir(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        env_dir = tmp_path / "group_vars" / "production"
        env_dir.mkdir(parents=True)
        policy = env_dir / "main.yml"
        policy.write_text("---\nheadscale_acl_policy:\n  acls: []\n")
        assert _find_acl_policy_file(bay_dir) == policy

    def test_empty_policy_value_still_counts(self, tmp_path: Path) -> None:
        """Ansible keys off `is defined`, so an empty value is still default-deny."""
        bay_dir = _make_consumer(tmp_path)
        policy = tmp_path / "group_vars" / "all" / "acl.yml"
        policy.write_text("---\nheadscale_acl_policy:\n")
        assert _find_acl_policy_file(bay_dir) == policy

    def test_ignores_key_in_comment_or_string(self, tmp_path: Path) -> None:
        """The substring pre-filter must not promote a mention to a definition."""
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "notes.yml").write_text(
            "---\n# headscale_acl_policy is defined elsewhere\nother_var: true\n"
        )
        assert _find_acl_policy_file(bay_dir) is None

    def test_skips_vault_encrypted_files(self, tmp_path: Path) -> None:
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "secrets.yml").write_text(
            "$ANSIBLE_VAULT;1.1;AES256\nheadscale_acl_policy deadbeef\n"
        )
        assert _find_acl_policy_file(bay_dir) is None

    def test_survives_malformed_yaml(self, tmp_path: Path) -> None:
        """A syntactically broken file must not crash the scan of the others."""
        bay_dir = _make_consumer(tmp_path)
        (tmp_path / "group_vars" / "all" / "broken.yml").write_text(
            "---\nheadscale_acl_policy: [unclosed\n"
        )
        good = tmp_path / "group_vars" / "all" / "good.yml"
        good.write_text("---\nheadscale_acl_policy:\n  acls: []\n")
        assert _find_acl_policy_file(bay_dir) == good

    def test_none_when_no_group_vars_dir(self, tmp_path: Path) -> None:
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        assert _find_acl_policy_file(bay_dir) is None


# ── _node_ip / _format_timestamp ──────────────────────────────────────────


class TestNodeIp:
    """_node_ip reads headscale's snake_case ip_addresses, not camelCase."""

    def test_returns_first_ip(self) -> None:
        assert _node_ip({"ip_addresses": ["100.64.0.6", "fd7a:115c:a1e0::6"]}) == "100.64.0.6"

    def test_empty_when_no_ips(self) -> None:
        assert _node_ip({"ip_addresses": []}) == ""
        assert _node_ip({}) == ""

    def test_null_ip_addresses(self) -> None:
        assert _node_ip({"ip_addresses": None}) == ""

    def test_camelcase_key_is_not_read(self) -> None:
        """Regression: reading ipAddresses silently blanked the IP column."""
        assert _node_ip({"ipAddresses": ["100.64.0.6"]}) == ""


class TestFormatTimestamp:
    """_format_timestamp renders headscale's protobuf Timestamp dicts."""

    def test_protobuf_timestamp_dict(self) -> None:
        # Assert via round-trip rather than a fixed local string, so the test
        # doesn't depend on the runner's timezone.
        out = _format_timestamp({"seconds": 1784036432, "nanos": 977901636})
        expected = (
            datetime.fromtimestamp(1784036432, tz=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        assert out == expected

    def test_protobuf_zero_time_renders_empty(self) -> None:
        """headscale's 'never' (e.g. no expiry) must not render as year 1."""
        assert _format_timestamp({"seconds": -62135596800}) == ""

    def test_iso_string_still_supported(self) -> None:
        assert _format_timestamp("2026-07-14T13:42:58Z") == "2026-07-14 13:42:58"

    def test_none_and_junk(self) -> None:
        assert _format_timestamp(None) == ""
        assert _format_timestamp({}) == ""
        assert _format_timestamp({"seconds": "nope"}) == ""
        assert _format_timestamp(12345) == ""


# ── _audit_acl_reachability ───────────────────────────────────────────────


def _node(name: str, ip: str) -> dict:
    return {"given_name": name, "name": name, "ip_addresses": [ip]}


_HOSTS = {"eu": "100.64.0.1/32", "laptop": "100.64.0.6/32", "phone": "100.64.0.4/32"}


class TestAuditAclReachability:
    """_audit_acl_reachability classifies nodes against a default-deny policy."""

    def test_node_named_as_dst_is_reachable(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["eu:*"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("eu", "100.64.0.1")])
        assert rows[0]["status"] == "reachable"
        assert rows[0]["inbound"] == 1

    def test_node_absent_from_policy_is_unknown(self) -> None:
        """The dead-on-arrival case: enrolled, online, named nowhere."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["eu:*"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("newbox", "100.64.0.8")])
        assert rows[0]["status"] == "unknown"

    def test_src_only_node_is_client_only_not_unknown(self) -> None:
        """A phone that only initiates is deliberate, not an oversight."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["phone"], "dst": ["eu:*"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("phone", "100.64.0.4")])
        assert rows[0]["status"] == "client-only"
        assert rows[0]["inbound"] == 0

    def test_src_wildcard_does_not_rescue_an_unnamed_node(self) -> None:
        """`src: ["*"]` matches everything, so it must not count as intent —
        that would mask exactly the bug this audit exists to catch."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["*"], "dst": ["eu:*"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("newbox", "100.64.0.8")])
        assert rows[0]["status"] == "unknown"

    def test_dst_wildcard_makes_everything_reachable(self) -> None:
        policy = {"hosts": _HOSTS, "acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
        rows, *_ = _audit_acl_reachability(policy, [_node("newbox", "100.64.0.8")])
        assert rows[0]["status"] == "reachable"

    def test_port_range_dst_counts(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["phone"], "dst": ["laptop:1-8786"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("laptop", "100.64.0.6")])
        assert rows[0]["status"] == "reachable"

    def test_unresolvable_targets_are_reported(self) -> None:
        """group:/user@ have no IP answer here — say so rather than silently
        claiming a node is unreachable."""
        policy = {
            "hosts": _HOSTS,
            "acls": [
                {"action": "accept", "src": ["laptop"], "dst": ["group:ops:*"]},
                {"action": "accept", "src": ["laptop"], "dst": ["admin@example.com:*"]},
            ],
        }
        rows, unresolvable, inert = _audit_acl_reachability(
            policy, [_node("eu", "100.64.0.1")]
        )
        assert "group:ops" in unresolvable
        assert "admin@example.com" in unresolvable
        assert inert == []
        assert rows[0]["status"] == "unknown"

    def test_deny_rules_are_ignored(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "deny", "src": ["*"], "dst": ["eu:*"]}],
        }
        rows, *_ = _audit_acl_reachability(policy, [_node("eu", "100.64.0.1")])
        assert rows[0]["status"] == "unknown"

    def test_empty_policy(self) -> None:
        rows, *_ = _audit_acl_reachability({}, [_node("eu", "100.64.0.1")])
        assert rows[0]["status"] == "unknown"

    def test_ipv6_node_address_matches(self) -> None:
        policy = {
            "hosts": {"v6box": "fd7a:115c:a1e0::8/128"},
            "acls": [{"action": "accept", "src": ["*"], "dst": ["v6box:*"]}],
        }
        node = {"given_name": "v6box", "ip_addresses": ["fd7a:115c:a1e0::8"]}
        rows, *_ = _audit_acl_reachability(policy, [node])
        assert rows[0]["status"] == "reachable"


# ── tag: resolution against live headscale state ──────────────────────────
#
# Both node shapes below are transcribed from a real `headscale nodes list
# -o json` on v0.29.2. The trap they encode: the flat top-level `tags` array is
# the ONLY populated one — `forced_tags` / `valid_tags` come back null even for a
# node whose Tags column reads tag:agent, so an implementation keyed on either of
# those sees an untagged tailnet and reports every tagged node as NOT IN POLICY.


def _tagged_node_flat(name: str, ip: str, tags: list[str]) -> dict:
    """v0.29.2 shape: tags in the flat `tags` array, forced_/valid_ absent."""
    return {
        "given_name": name,
        "name": name,
        "ip_addresses": [ip],
        "user": {"id": 2147455555, "name": "tagged-devices"},
        "tags": tags,
    }


def _tagged_node_null_fields(name: str, ip: str, tags: list[str]) -> dict:
    """Same node with forced_tags/valid_tags explicitly null, as headscale emits
    them for a node that joined on a tag-stamped pre-auth key."""
    node = _tagged_node_flat(name, ip, tags)
    node["forced_tags"] = None
    node["valid_tags"] = None
    node["pre_auth_key"] = {"id": 11, "acl_tags": None}
    return node


class TestAuditTagResolution:
    """tag: dsts must count as naming the nodes that carry the tag."""

    def test_tag_dst_makes_tagged_node_reachable(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["tag:agent:*"]}],
        }
        nodes = [
            _tagged_node_flat("kaya-metal-1", "100.64.0.9", ["tag:agent"]),
            _node("untagged", "100.64.0.8"),
        ]
        rows, unresolvable, inert = _audit_acl_reachability(policy, nodes)
        assert rows[0]["status"] == "reachable"
        assert rows[0]["inbound"] == 1
        assert rows[1]["status"] == "unknown"
        assert unresolvable == []
        assert inert == []

    def test_null_forced_and_valid_tag_fields_still_resolve(self) -> None:
        """The shipped-blind case: keying on forced_tags/valid_tags sees nothing."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["tag:prod-app:*"]}],
        }
        nodes = [_tagged_node_null_fields("bay-eu", "100.64.0.1", ["tag:prod-app"])]
        rows, _, inert = _audit_acl_reachability(policy, nodes)
        assert rows[0]["status"] == "reachable"
        assert inert == []

    def test_tag_on_pre_auth_key_resolves(self) -> None:
        """Defensive union: if a headscale build ever echoes the key stamp into
        pre_auth_key.acl_tags instead of the node's tags, still resolve it."""
        node = {
            "given_name": "keyed",
            "ip_addresses": ["100.64.0.9"],
            "pre_auth_key": {"id": 11, "acl_tags": ["tag:agent"]},
        }
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["tag:agent:*"]}],
        }
        rows, _, inert = _audit_acl_reachability(policy, [node])
        assert rows[0]["status"] == "reachable"
        assert inert == []

    def test_tag_matching_no_node_is_reported_inert(self) -> None:
        """The tag-flavored dead-on-arrival case: valid rule, grants nothing."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["tag:ghost:*"]}],
        }
        nodes = [_tagged_node_flat("kaya-metal-1", "100.64.0.9", ["tag:agent"])]
        rows, unresolvable, inert = _audit_acl_reachability(policy, nodes)
        assert inert == ["tag:ghost"]
        assert "tag:ghost" not in unresolvable
        assert rows[0]["status"] == "unknown"

    def test_inert_tag_reported_once(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [
                {"action": "accept", "src": ["laptop"], "dst": ["tag:ghost:*"]},
                {"action": "accept", "src": ["phone"], "dst": ["tag:ghost:22"]},
            ],
        }
        _, _, inert = _audit_acl_reachability(policy, [_node("eu", "100.64.0.1")])
        assert inert == ["tag:ghost"]

    def test_tag_src_makes_node_client_only_not_unknown(self) -> None:
        """src-side resolution too, or a tagged laptop reads as NOT IN POLICY."""
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["tag:agent"], "dst": ["eu:*"]}],
        }
        nodes = [_tagged_node_flat("kaya-metal-1", "100.64.0.9", ["tag:agent"])]
        rows, _, inert = _audit_acl_reachability(policy, nodes)
        assert rows[0]["status"] == "client-only"
        assert rows[0]["inbound"] == 0
        assert inert == []

    def test_tag_resolution_only_adds(self) -> None:
        """A tag must never take reachability away from a hosts:-named node."""
        policy = {
            "hosts": _HOSTS,
            "acls": [
                {"action": "accept", "src": ["laptop"], "dst": ["eu:*"]},
                {"action": "accept", "src": ["laptop"], "dst": ["tag:ghost:*"]},
            ],
        }
        rows, _, inert = _audit_acl_reachability(policy, [_node("eu", "100.64.0.1")])
        assert rows[0]["status"] == "reachable"
        assert inert == ["tag:ghost"]

    def test_multiple_nodes_share_a_tag(self) -> None:
        policy = {
            "hosts": _HOSTS,
            "acls": [{"action": "accept", "src": ["laptop"], "dst": ["tag:prod-app:*"]}],
        }
        nodes = [
            _tagged_node_flat("bay-eu", "100.64.0.1", ["tag:prod-app"]),
            _tagged_node_flat("bay-na", "100.64.0.2", ["tag:prod-app"]),
            _tagged_node_flat("kaya-metal-1", "100.64.0.9", ["tag:agent"]),
        ]
        rows, _, inert = _audit_acl_reachability(policy, nodes)
        assert [r["status"] for r in rows] == ["reachable", "reachable", "unknown"]
        assert inert == []

    def test_node_tags_ignores_non_tag_junk(self) -> None:
        node = {"tags": ["tag:agent", "agent", ""], "forced_tags": "not-a-list"}
        assert gateway._node_tags(node) == ["tag:agent"]

    def test_resolve_target_without_index_keeps_tag_unresolvable(self) -> None:
        """No live node list, no answer — the old contract for other callers."""
        assert gateway._resolve_acl_target("tag:agent", _HOSTS) is None


# ── Backend call contract ─────────────────────────────────────────────────
#
# These commands drive a remote headscale via LocalHeadscaleBackend. The bugs
# they guard against were all "the CLI called the backend with the wrong thing"
# — invisible to helper-level tests, and each one shipped broken.


class _RecordingBackend:
    """Minimal LocalHeadscaleBackend stand-in that records how it was called."""

    def __init__(
        self, users: list[dict] | None = None, nodes: list[dict] | None = None
    ) -> None:
        # created_at is a protobuf Timestamp dict — the shape headscale actually
        # emits. An RFC3339 string here would hide a `dict[:19]` TypeError.
        self.users = users if users is not None else [
            {"id": 3, "name": "alice", "created_at": {"seconds": 1784036432, "nanos": 977901636}}
        ]
        self.nodes = nodes if nodes is not None else [
            {"id": 7, "name": "myphone", "given_name": "myphone"}
        ]
        self.calls: list[tuple] = []

    def list_users(self) -> list[dict]:
        self.calls.append(("list_users",))
        return self.users

    def list_nodes(self) -> list[dict]:
        self.calls.append(("list_nodes",))
        return self.nodes

    def get_user_id(self, name: str) -> str:
        self.calls.append(("get_user_id", name))
        for u in self.users:
            if u.get("name") == name:
                return str(u["id"])
        raise BayError(f"User '{name}' not found in headscale.")

    def rename_user(self, old_name: str, new_name: str) -> None:
        self.calls.append(("rename_user", old_name, new_name))

    def create_user(self, name: str) -> None:
        self.calls.append(("create_user", name))
        self.users.append(
            {"id": 9, "name": name, "created_at": {"seconds": 1784100000, "nanos": 0}}
        )

    def delete_user(self, user_id: str) -> None:
        self.calls.append(("delete_user", user_id))

    def delete_node(self, node_id: object) -> None:
        self.calls.append(("delete_node", node_id))

    def rename_node(self, node_id: object, new_name: str) -> None:
        self.calls.append(("rename_node", node_id, new_name))

    def generate_preauth_key(
        self,
        user_id: str,
        *,
        expiry: str = "",
        reusable: bool = False,
        tags: list[str] | None = None,
    ) -> str:
        self.calls.append(("generate_preauth_key", user_id, expiry, reusable, tags))
        return "nodekey:fake"


@pytest.fixture()
def gateway_stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    """Stub out config/host resolution so commands reach the backend."""
    backend = _RecordingBackend()
    monkeypatch.setattr(gateway.paths, "find_bay_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(
        gateway,
        "_get_gateway_config",
        lambda *a, **k: {"access_gateway": "headscale", "headscale_domain": "hs.example.com"},
    )
    monkeypatch.setattr(gateway, "_make_backend", lambda *a, **k: backend)
    monkeypatch.setattr(gateway, "_find_acl_policy_file", lambda *a, **k: None)
    return backend


class TestRenameUserBackendCall:
    def test_passes_name_not_id(self, gateway_stub: _RecordingBackend) -> None:
        """rename_user resolves the name itself — passing an id looks up a user named '3'."""
        result = runner.invoke(app, ["gateway", "rename-user", "alice", "bob"])
        assert result.exit_code == 0, result.output
        assert ("rename_user", "alice", "bob") in gateway_stub.calls
        assert not any(c[0] == "rename_user" and c[1] == "3" for c in gateway_stub.calls)

    def test_unknown_user_exits_nonzero(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["gateway", "rename-user", "ghost", "bob"])
        assert result.exit_code == 1
        assert not any(c[0] == "rename_user" for c in gateway_stub.calls)


class TestRenameNodeValidation:
    """`nodes rename` f-strings new_name into a root shell (gateway_backend.py).

    rename-node was the one name-taking command with no validation, while its
    rename-user sibling validated both names. `enroll` already gates node
    hostnames with this same charset, so nothing the CLI could create is
    rejected here.
    """

    def test_valid_rename_reaches_backend(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["gateway", "rename-node", "myphone", "phone"])
        assert result.exit_code == 0, result.output
        assert ("rename_node", 7, "phone") in gateway_stub.calls

    @pytest.mark.parametrize("payload", [
        "x; touch /tmp/pwned",
        "x && id",
        "x$(id)",
        "x`id`",
        "x | tee /etc/passwd",
        "x\nid",
        "x y",
    ])
    def test_shell_metacharacters_rejected_before_backend(
        self, gateway_stub: _RecordingBackend, payload: str
    ) -> None:
        result = runner.invoke(app, ["gateway", "rename-node", "myphone", payload])
        assert result.exit_code == 1
        assert not any(c[0] == "rename_node" for c in gateway_stub.calls), (
            f"payload {payload!r} reached the backend — it would be interpolated "
            "into `docker exec ... nodes rename` and run as root"
        )

    def test_old_name_is_not_charset_validated(
        self, gateway_stub: _RecordingBackend
    ) -> None:
        """The old name is a lookup key, never shell-bound.

        Validating it would block renaming a legacy node whose existing
        given_name predates this CLI's charset. It must fail as "not found",
        not as "invalid name".
        """
        gateway_stub.nodes = [{"id": 8, "name": "old.node", "given_name": "old.node"}]
        result = runner.invoke(app, ["gateway", "rename-node", "old.node", "newname"])
        assert result.exit_code == 0, result.output
        assert ("rename_node", 8, "newname") in gateway_stub.calls


class TestEnrollKeyExpiry:
    def test_default_expiry_is_forwarded(self, gateway_stub: _RecordingBackend) -> None:
        """The 24h default must reach headscale, which otherwise applies its own (1h)."""
        result = runner.invoke(app, ["--json", "gateway", "enroll", "--user", "alice"])
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert key_calls, "no pre-auth key generated"
        assert key_calls[0][2] == "24h"

    def test_explicit_expiry_is_forwarded(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(
            app, ["--json", "gateway", "enroll", "--user", "alice", "--expiry", "7d"]
        )
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert key_calls[0][2] == "7d"


class TestUserInfoCreatedField:
    def test_renders_created_at(self, gateway_stub: _RecordingBackend) -> None:
        """headscale emits snake_case `created_at` as a protobuf Timestamp dict.

        Reading camelCase rendered blank; reading the right key but slicing the
        dict raises TypeError. Only routing through _format_timestamp works.
        """
        result = runner.invoke(app, ["gateway", "user-info", "alice"])
        assert result.exit_code == 0, result.output
        # Round-trip rather than a fixed local string, so this is timezone-safe.
        expected = (
            datetime.fromtimestamp(1784036432, tz=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        assert expected in result.output


class TestEnrollExpiryValidation:
    def test_junk_expiry_rejected_locally(self, gateway_stub: _RecordingBackend) -> None:
        """Junk must fail here, not as an opaque headscale error over ansible."""
        result = runner.invoke(
            app, ["--json", "gateway", "enroll", "--user", "alice", "--expiry", "banana"]
        )
        assert result.exit_code == 1
        assert not any(c[0] == "generate_preauth_key" for c in gateway_stub.calls)


class TestDeleteUserBackendCommand:
    """`users destroy` takes --identifier and prompts without --force.

    A positional id is ignored by headscale v0.28, so the command failed at the
    last step — after --force had already deleted the user's nodes.
    """

    def _cmd_for(self, monkeypatch: pytest.MonkeyPatch) -> str:
        from bay_cli.commands.gateway_backend import LocalHeadscaleBackend

        sent: list[str] = []
        monkeypatch.setattr(
            LocalHeadscaleBackend,
            "_exec",
            lambda self, cmd, message="": sent.append(cmd) or "",
        )
        LocalHeadscaleBackend(bay_dir=Path("/tmp"), env="production").delete_user("3")
        return sent[0]

    def test_uses_identifier_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._cmd_for(monkeypatch)
        assert "--identifier 3" in cmd
        assert "users destroy 3" not in cmd

    def test_passes_force_to_skip_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert "--force" in self._cmd_for(monkeypatch)


# ── `gateway nodes --json` ────────────────────────────────────────────────
#
# The command printed its Rich table unconditionally, so `--json gateway nodes`
# emitted a box-drawn table where a payload belonged and json.load() failed on
# it. The documented workaround was to SSH to the control host and run
# `docker exec headscale headscale nodes list -o json` by hand (GH bay#29).


class TestNodesJsonOutput:
    @pytest.fixture(autouse=True)
    def _reset_json_mode(self):
        from bay_cli.console import output as console_output

        console_output.set_json_mode(False)
        console_output._message_buffer.clear()
        yield
        console_output.set_json_mode(False)
        console_output._message_buffer.clear()

    def test_json_output_is_parseable(self, gateway_stub: _RecordingBackend) -> None:
        gateway_stub.nodes = [
            {
                "id": 7,
                "name": "myphone",
                "given_name": "myphone",
                "ip_addresses": ["100.64.0.4", "fd7a:115c:a1e0::4"],
                "last_seen": {"seconds": 1784036432, "nanos": 0},
                "online": True,
                "user": {"id": 3, "name": "alice"},
            }
        ]
        result = runner.invoke(app, ["--json", "gateway", "nodes"])
        assert result.exit_code == 0, result.output

        payload = json.loads(result.output)  # the actual regression
        node = payload["data"]["nodes"][0]
        assert node["name"] == "myphone"
        assert node["ip"] == "100.64.0.4"
        assert node["user"] == "alice"
        assert node["online"] is True
        assert node["last_seen"]

    def test_json_output_has_no_table_glyphs(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["--json", "gateway", "nodes"])
        assert not set(result.output) & set("┏┳┓┡╇┩│└┴┘"), (
            "Rich table glyphs leaked into --json output"
        )

    def test_empty_node_list_still_emits_json(self, gateway_stub: _RecordingBackend) -> None:
        gateway_stub.nodes = []
        result = runner.invoke(app, ["--json", "gateway", "nodes"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["nodes"] == []

    def test_human_mode_still_renders_a_table(self, gateway_stub: _RecordingBackend) -> None:
        gateway_stub.nodes = [
            {
                "id": 7,
                "name": "myphone",
                "given_name": "myphone",
                "ip_addresses": ["100.64.0.4"],
                "online": True,
                "user": {"id": 3, "name": "alice"},
            }
        ]
        result = runner.invoke(app, ["gateway", "nodes"])
        assert result.exit_code == 0, result.output
        assert "myphone" in result.output
        assert "100.64.0.4" in result.output


# ── `enroll --tag` / `key --tag` ──────────────────────────────────────────
#
# Tagging an agent box used to be two commands: enroll here, then a manual
# `headscale nodes tag -i <id> -t tag:agent` on the control host — leaving a
# window where the node was online but ungranted. A pre-auth key can carry the
# tags itself, so the node joins already tagged.


class TestValidateTags:
    """Bad tags must fail locally, before a user has been created remotely."""

    @pytest.mark.parametrize("value", [
        "tag:agent",
        "tag:ci-runner",
        "tag:a",
        "tag:agent2",
        "tag:x-y-z",
        "tag:0",
    ])
    def test_valid_tags(self, value: str) -> None:
        assert gateway._validate_tags([value]) == [value]

    @pytest.mark.parametrize("value", [
        "agent",             # the recurring typo: no prefix at all
        "tags:agent",
        "tag:",
        "tag:-agent",        # headscale rejects a leading hyphen
        "tag:Agent",         # uppercase
        "tag:my_agent",      # underscore
        "tag:my agent",
        "tag:agent;id",
        "tag:agent,tag:two",  # one flag per tag; commas are ours to add
        "tag:agent\n",
        ":agent",
    ])
    def test_invalid_tags_exit(self, value: str) -> None:
        with pytest.raises(ClickExit):
            gateway._validate_tags([value])

    def test_none_and_empty_are_no_tags(self) -> None:
        assert gateway._validate_tags(None) == []
        assert gateway._validate_tags([]) == []

    def test_multiple_tags_preserved_in_order(self) -> None:
        tags = ["tag:agent", "tag:ci"]
        assert gateway._validate_tags(tags) == tags

    def test_repeated_tag_collapses_to_one(self) -> None:
        """`--tag tag:agent --tag tag:agent` otherwise stamps "tag:agent,tag:agent"."""
        assert gateway._validate_tags(["tag:agent", "tag:agent"]) == ["tag:agent"]

    def test_dedupe_keeps_first_occurrence_order(self) -> None:
        assert gateway._validate_tags(
            ["tag:ci", "tag:agent", "tag:ci", "tag:agent"]
        ) == ["tag:ci", "tag:agent"]

    def test_dedupe_does_not_hide_an_invalid_repeat(self) -> None:
        """Validation runs before the collapse, so a repeated typo still errors."""
        with pytest.raises(ClickExit):
            gateway._validate_tags(["nope", "nope"])

    def test_first_bad_tag_is_named(self, capsys: pytest.CaptureFixture) -> None:
        """With several --tag flags, "invalid tag" alone doesn't say which."""
        with pytest.raises(ClickExit):
            gateway._validate_tags(["tag:agent", "nope"])
        assert "nope" in capsys.readouterr().out


class TestPreauthKeyTagFlag:
    """The headscale invocation: tags go in ONE comma-joined --tags value."""

    def _cmd_for(self, monkeypatch: pytest.MonkeyPatch, **kwargs) -> str:
        from bay_cli.commands.gateway_backend import LocalHeadscaleBackend

        sent: list[str] = []
        monkeypatch.setattr(
            LocalHeadscaleBackend,
            "_exec",
            lambda self, cmd, message="": sent.append(cmd) or "nodekey:fake",
        )
        LocalHeadscaleBackend(bay_dir=Path("/tmp"), env="production").generate_preauth_key(
            "3", **kwargs
        )
        return sent[0]

    def test_single_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cmd = self._cmd_for(monkeypatch, tags=["tag:agent"])
        assert "preauthkeys create --user 3 --tags tag:agent" == cmd.split("headscale headscale ")[1]

    def test_multiple_tags_are_comma_joined_into_one_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = self._cmd_for(monkeypatch, tags=["tag:agent", "tag:ci"])
        assert "--tags tag:agent,tag:ci" in cmd
        assert cmd.count("--tags") == 1

    def test_deduped_tags_produce_one_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End of the pipe: a repeated --tag must not reach headscale twice."""
        cmd = self._cmd_for(
            monkeypatch, tags=gateway._validate_tags(["tag:agent", "tag:agent"])
        )
        assert "--tags tag:agent" in cmd
        assert cmd.count("tag:agent") == 1
        assert cmd.count("--tags") == 1

    def test_tags_coexist_with_expiry_and_reusable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cmd = self._cmd_for(monkeypatch, expiry="30d", reusable=True, tags=["tag:agent"])
        assert "--reusable" in cmd
        assert "--expiration 30d" in cmd
        assert "--tags tag:agent" in cmd

    @pytest.mark.parametrize("tags", [None, []])
    def test_no_tags_emits_no_flag(self, monkeypatch: pytest.MonkeyPatch, tags) -> None:
        """The untagged path must stay byte-identical to before the feature."""
        assert self._cmd_for(monkeypatch, expiry="24h", tags=tags) == (
            "docker exec headscale headscale preauthkeys create --user 3 --expiration 24h"
        )


class TestEnrollTagOption:
    def test_tag_reaches_the_backend(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(
            app, ["--json", "gateway", "enroll", "--user", "alice", "--tag", "tag:agent"]
        )
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert key_calls[0][4] == ["tag:agent"]

    def test_repeated_tag_flags_accumulate(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(
            app,
            [
                "--json", "gateway", "enroll", "--user", "alice",
                "--tag", "tag:agent", "--tag", "tag:ci",
            ],
        )
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert key_calls[0][4] == ["tag:agent", "tag:ci"]

    def test_no_tag_passes_no_tags(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["--json", "gateway", "enroll", "--user", "alice"])
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert not key_calls[0][4]

    def test_bad_tag_rejected_before_any_headscale_call(
        self, gateway_stub: _RecordingBackend
    ) -> None:
        """No user creation, no key — the whole point of validating locally."""
        result = runner.invoke(
            app, ["--json", "gateway", "enroll", "--user", "newbox", "--tag", "agent"]
        )
        assert result.exit_code == 1
        assert not gateway_stub.calls


class TestKeyTagOption:
    def test_tag_reaches_the_backend(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["gateway", "key", "alice", "--tag", "tag:agent"])
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert key_calls[0][4] == ["tag:agent"]

    def test_no_tag_is_unchanged(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["gateway", "key", "alice"])
        assert result.exit_code == 0, result.output
        key_calls = [c for c in gateway_stub.calls if c[0] == "generate_preauth_key"]
        assert not key_calls[0][4]

    def test_bad_tag_rejected_before_lookup(self, gateway_stub: _RecordingBackend) -> None:
        result = runner.invoke(app, ["gateway", "key", "alice", "--tag", "tag:BAD"])
        assert result.exit_code == 1
        assert not gateway_stub.calls


class TestEnrollEpilogue:
    """A tagged enrollment gets its own guidance; the untagged one is untouched."""

    @pytest.fixture()
    def _default_deny(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        acl = tmp_path / "group_vars" / "all" / "headscale_acl.yml"
        acl.parent.mkdir(parents=True, exist_ok=True)
        acl.write_text("---\nheadscale_acl_policy:\n  acls: []\n")
        monkeypatch.setattr(gateway, "_find_acl_policy_file", lambda *a, **k: acl)
        return acl

    def test_untagged_still_gets_the_dead_on_arrival_walkthrough(
        self, gateway_stub: _RecordingBackend, _default_deny: Path
    ) -> None:
        result = runner.invoke(app, ["gateway", "enroll", "--user", "alice"])
        assert result.exit_code == 0, result.output
        assert "not reachable yet" in _strip_ansi(result.output)
        assert "tagged-devices" not in result.output

    def test_tagged_replaces_it_with_tag_guidance(
        self, gateway_stub: _RecordingBackend, _default_deny: Path
    ) -> None:
        result = runner.invoke(
            app, ["gateway", "enroll", "--user", "alice", "--tag", "tag:agent"]
        )
        assert result.exit_code == 0, result.output
        out = _strip_ansi(result.output)
        # The per-device "add a hosts: alias" walkthrough would be wrong advice here.
        assert "not reachable yet" not in out
        for expected in ("tag:agent", "tagged-devices", "acl audit", "124"):
            assert expected in out, f"missing {expected!r} from tagged epilogue"

    def test_tagged_epilogue_appears_without_a_policy_file(
        self, gateway_stub: _RecordingBackend
    ) -> None:
        """The ownership + verification caveats hold on an allow-all tailnet too."""
        result = runner.invoke(
            app, ["gateway", "enroll", "--user", "alice", "--tag", "tag:agent"]
        )
        assert result.exit_code == 0, result.output
        assert "tag:agent" in _strip_ansi(result.output)
