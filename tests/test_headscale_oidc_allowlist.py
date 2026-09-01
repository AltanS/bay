"""Headscale OIDC enrolment allowlist (`allowed_domains|users|groups`).

Headscale applies no allowlist of its own, so an issuer with all three
lists empty lets any account that issuer authenticates enrol a node. The
validator therefore hard-fails on that combination, and warns separately
when OIDC is on while `headscale_acl_policy` is undefined (open enrolment
into an allow-all tailnet).

Covers the validator in both directions and the template rendering of the
three keys.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bay_cli.commands.validate import _validate_headscale_oidc
from helpers import make_ansible_env

TEMPLATE_DIR = Path(__file__).parent.parent / "roles" / "headscale" / "templates"


class _FakeResult:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.oks: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self, msg: str) -> None:
        self.oks.append(msg)


def _run(**group_vars) -> _FakeResult:
    r = _FakeResult()
    _validate_headscale_oidc({"group_vars/all/vpn.yml": dict(group_vars)}, r)
    return r


# ── the validator ──────────────────────────────────────────────────────


class TestOidcAllowlistGate:
    def test_issuer_with_no_allowlist_fails(self):
        r = _run(headscale_oidc_issuer="https://accounts.google.com")
        assert len(r.failures) == 1
        assert "allowlist" in r.failures[0]

    def test_failure_names_the_open_enrolment_consequence(self):
        r = _run(headscale_oidc_issuer="https://accounts.google.com")
        msg = r.failures[0]
        assert "enrol" in msg
        assert "internet" in msg

    def test_issuer_with_empty_lists_still_fails(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_domains=[],
            headscale_oidc_allowed_users=[],
            headscale_oidc_allowed_groups=[],
        )
        assert len(r.failures) == 1

    def test_domains_populated_passes(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_domains=["example.com"],
        )
        assert r.failures == []

    def test_users_populated_passes(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_users=["ops@example.com"],
        )
        assert r.failures == []

    def test_groups_populated_passes(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_groups=["engineering"],
        )
        assert r.failures == []

    def test_no_issuer_passes_and_warns_nothing(self):
        r = _run(headscale_oidc_issuer="")
        assert r.failures == []
        assert r.warnings == []

    def test_issuer_key_absent_passes(self):
        r = _run(stack_name="demo")
        assert r.failures == []
        assert r.warnings == []


class TestAclWarning:
    def test_missing_acl_policy_warns_once(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_domains=["example.com"],
        )
        assert len(r.warnings) == 1
        assert "headscale_acl_policy" in r.warnings[0]
        assert "ALLOW-ALL" in r.warnings[0]
        assert r.failures == []

    def test_acl_policy_defined_does_not_warn(self):
        r = _run(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_allowed_domains=["example.com"],
            headscale_acl_policy={"acls": [{"action": "accept"}]},
        )
        assert r.warnings == []
        assert r.failures == []

    def test_acl_warning_is_independent_of_the_allowlist_failure(self):
        """Both problems at once → exactly one fail and one warn."""
        r = _run(headscale_oidc_issuer="https://accounts.google.com")
        assert len(r.failures) == 1
        assert len(r.warnings) == 1


# ── template rendering ─────────────────────────────────────────────────


def _base_context(**overrides) -> dict:
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
        "headscale_oidc_issuer": "",
        "headscale_oidc_client_id": "",
        "headscale_oidc_client_secret": "",
        "headscale_oidc_allowed_domains": [],
        "headscale_oidc_allowed_users": [],
        "headscale_oidc_allowed_groups": [],
    }
    base.update(overrides)
    return base


def _render_config(**overrides) -> str:
    env = make_ansible_env(TEMPLATE_DIR)
    return env.get_template("config.yaml.j2").render(**_base_context(**overrides))


class TestConfigRendering:
    def test_populated_lists_emit_all_three_keys(self):
        rendered = _render_config(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_client_id="cid",
            headscale_oidc_client_secret="csec",
            headscale_oidc_allowed_domains=["example.com"],
            headscale_oidc_allowed_users=["ops@example.com"],
            headscale_oidc_allowed_groups=["engineering"],
        )
        oidc = yaml.safe_load(rendered)["oidc"]
        assert oidc["allowed_domains"] == ["example.com"]
        assert oidc["allowed_users"] == ["ops@example.com"]
        assert oidc["allowed_groups"] == ["engineering"]

    def test_empty_lists_emit_no_keys(self):
        rendered = _render_config(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_client_id="cid",
            headscale_oidc_client_secret="csec",
        )
        oidc = yaml.safe_load(rendered)["oidc"]
        assert set(oidc) == {"issuer", "client_id", "client_secret"}

    def test_one_populated_list_emits_only_that_key(self):
        rendered = _render_config(
            headscale_oidc_issuer="https://accounts.google.com",
            headscale_oidc_client_id="cid",
            headscale_oidc_client_secret="csec",
            headscale_oidc_allowed_domains=["example.com", "other.example"],
        )
        oidc = yaml.safe_load(rendered)["oidc"]
        assert oidc["allowed_domains"] == ["example.com", "other.example"]
        assert "allowed_users" not in oidc
        assert "allowed_groups" not in oidc

    def test_no_issuer_emits_no_oidc_block(self):
        rendered = _render_config(headscale_oidc_allowed_domains=["example.com"])
        assert "oidc" not in yaml.safe_load(rendered)
