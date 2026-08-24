"""Tests for `headscale_extra_dns_records` — operator-defined manual A
records merged into the headscale role's split-DNS templates.

Split-DNS (`docs/access-gateways.md#split-dns-for-vpn-services`) is
auto-generated from two sources today: `access: vpn` services and
`tailnet_proxies`. `headscale_extra_dns_records` adds a third, manual
source for domains Bay can't derive on its own (e.g. an
externally-managed tailnet node).

Both templates build a `vpn_records` list from the same two collection
loops (+ now a third for manual records) using a shared `seen_domains`
dedup dict — first writer wins, so vpn-service and tailnet_proxies
records always take priority over a manual record for the same domain.

Covers:
  - manual record appears in `extra-records.json` output
  - manual record appears in `config.yaml`'s split-DNS block
  - dedup: a manual record for a domain already claimed by an
    `access: vpn` service is dropped, not appended
  - gate: `config.yaml`'s `dns.nameservers.split` + `extra_records_path`
    block renders even when manual records are the ONLY split-DNS
    source (the `{% if vpn_records | length > 0 %}` gate must see the
    manual-only append, which happens before the gate)
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from helpers import make_ansible_env

TEMPLATE_DIR = Path(__file__).parent.parent / "roles" / "headscale" / "templates"


def _base_context(**overrides) -> dict:
    """Minimal context both templates need to render without errors.

    `groups`/`hostvars`/`inventory_hostname` stand in for Ansible's
    real magic vars — the templates iterate `groups['all']` and read
    each host's `services` dict via `hostvars`.
    """
    base = {
        "ansible_managed": "test",
        "headscale_domain": "hs.example.com",
        "headscale_tailnet_cidr": ["100.64.0.0/10"],
        "headscale_magic_dns_domain": "test.tailnet.internal",
        "headscale_server_tailnet_ip": "100.64.0.1",
        "groups": {"all": ["control"]},
        "inventory_hostname": "control",
        "hostvars": {"control": {"region": "", "services": {}}},
        "tailnet_proxies": {},
        "headscale_extra_dns_records": [],
    }
    base.update(overrides)
    return base


def _render_extra_records(**overrides) -> str:
    env = make_ansible_env(TEMPLATE_DIR)
    return env.get_template("extra-records.json.j2").render(**_base_context(**overrides))


def _render_config(**overrides) -> str:
    env = make_ansible_env(TEMPLATE_DIR)
    return env.get_template("config.yaml.j2").render(**_base_context(**overrides))


# ── manual record appears in both rendered outputs ─────────────────────


class TestManualRecordAppears:
    def test_extra_records_json_contains_manual_record(self) -> None:
        rendered = _render_extra_records(
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        records = json.loads(rendered)
        assert {"name": "intranet.example.com", "type": "A", "value": "100.64.0.20"} in records

    def test_extra_records_json_respects_explicit_type(self) -> None:
        rendered = _render_extra_records(
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20", "type": "A"}
            ],
        )
        records = json.loads(rendered)
        assert records == [
            {"name": "intranet.example.com", "type": "A", "value": "100.64.0.20"}
        ]

    def test_config_yaml_split_dns_contains_manual_record(self) -> None:
        rendered = _render_config(
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        parsed = yaml.safe_load(rendered)
        assert parsed["dns"]["nameservers"]["split"]["intranet.example.com"] == [
            "100.100.100.100"
        ]


# ── dedup against an identical vpn-service domain ───────────────────────


class TestDedupAgainstVpnService:
    def _hostvars_with_vpn_service(self) -> dict:
        return {
            "control": {
                "region": "",
                "headscale_server_tailnet_ip": "100.64.0.9",
                "services": {
                    "myservice": {
                        "access": "vpn",
                        "domains": ["intranet.example.com"],
                    }
                },
            }
        }

    def test_extra_records_json_keeps_vpn_service_value(self) -> None:
        rendered = _render_extra_records(
            hostvars=self._hostvars_with_vpn_service(),
            headscale_server_tailnet_ip="100.64.0.9",
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        records = json.loads(rendered)
        matches = [r for r in records if r["name"] == "intranet.example.com"]
        assert len(matches) == 1, f"expected exactly one entry, got {matches}"
        assert matches[0]["value"] == "100.64.0.9", (
            "vpn-service record must win over the manual record for the "
            "same domain (first-writer-wins dedup)"
        )

    def test_config_yaml_split_dns_dedup_single_key(self) -> None:
        rendered = _render_config(
            hostvars=self._hostvars_with_vpn_service(),
            headscale_server_tailnet_ip="100.64.0.9",
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        # A duplicate YAML mapping key would either be silently
        # collapsed by yaml.safe_load or, in Headscale's stricter Go
        # YAML parser, a hard crash — assert only one `split:` occurrence.
        split_lines = [line for line in rendered.splitlines() if line.strip() == "intranet.example.com:"]
        assert len(split_lines) == 1, (
            f"expected exactly one split-DNS key for intranet.example.com, got {split_lines}"
        )
        parsed = yaml.safe_load(rendered)
        assert parsed["dns"]["nameservers"]["split"] == {"intranet.example.com": ["100.100.100.100"]}


# ── gate opens when only manual records exist ───────────────────────────


class TestGateOpensWithOnlyManualRecords:
    def test_no_manual_or_vpn_records_omits_split_block(self) -> None:
        rendered = _render_config()
        parsed = yaml.safe_load(rendered)
        # nameservers: has no children (no global, no split) → None
        assert not (parsed["dns"]["nameservers"] or {}).get("split")
        assert "extra_records_path" not in parsed["dns"]

    def test_manual_records_only_opens_split_block_and_path(self) -> None:
        rendered = _render_config(
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        parsed = yaml.safe_load(rendered)
        assert parsed["dns"]["nameservers"]["split"] == {"intranet.example.com": ["100.100.100.100"]}
        assert parsed["dns"]["extra_records_path"] == "/etc/headscale/extra-records.json"

    def test_manual_records_only_extra_records_json_nonempty(self) -> None:
        rendered = _render_extra_records(
            headscale_extra_dns_records=[
                {"name": "intranet.example.com", "value": "100.64.0.20"}
            ],
        )
        records = json.loads(rendered)
        assert records == [
            {"name": "intranet.example.com", "type": "A", "value": "100.64.0.20"}
        ]


# ── no manual records is a no-op (regression guard) ─────────────────────


class TestNoManualRecordsIsNoop:
    def test_extra_records_json_empty_array_by_default(self) -> None:
        rendered = _render_extra_records()
        assert json.loads(rendered) == []

    def test_config_yaml_valid_and_gate_closed_by_default(self) -> None:
        rendered = _render_config()
        parsed = yaml.safe_load(rendered)
        assert isinstance(parsed, dict)
        assert not (parsed["dns"]["nameservers"] or {}).get("split")
