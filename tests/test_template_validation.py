"""Template validation tests — render every gateway x service combination
and validate the output is valid YAML with expected structure.

Catches template regressions before they reach users.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from bay_cli.wizard.models import WizardResult
from bay_cli.wizard.scaffold import scaffold

# ── Jinja2 artifact pattern ───────────────────────────────────────────

_JINJA2_RE = re.compile(r"\{\{(?!\s*domain_base\s*\}\})|\{%|%\}")

# Files that legitimately contain Ansible Jinja2 expressions
# (These are rendered by Ansible at deploy time, not by the scaffold wizard)
_JINJA2_ALLOWED = {
    "group_vars/all/security.yml",
    "group_vars/all/main.yml",
    "group_vars/all/users.yml",
    "group_vars/all/services.yml",
    "group_vars/all/registry.yml",
    "group_vars/all/access_gateway.yml",
    "group_vars/production/main.yml",
}

# Subset of _JINJA2_ALLOWED that must still parse as valid YAML
# (their Jinja2 expressions are properly quoted for YAML compatibility)
_JINJA2_YAML_MUST_PARSE = {
    "group_vars/all/security.yml",
    "group_vars/all/registry.yml",
}

# ── Test matrix ──────────────────────────────────────────────────────

_GATEWAYS = [
    ("headscale", {"headscale_domain": "hs.example.com"}),
    ("wireguard", {"vpn_peer_ips": ["10.0.0.2"]}),
    ("none", {}),
]

_SERVICE_COMBOS = [
    (["gatus"], "minimal"),
    (["gatus", "postgres", "redis"], "gatus-with-accessories"),
    (["vaultwarden"], "vaultwarden-only"),
    (["n8n", "postgres"], "n8n-with-postgres"),
    (["plausible", "postgres"], "plausible-with-postgres"),
    (["umami", "postgres"], "umami-with-postgres"),
    (["gatus", "vaultwarden", "n8n", "plausible", "umami", "postgres", "redis", "mariadb"], "full-catalog"),
    ([], "blank"),
]


def _make_result(gateway: str, gateway_kwargs: dict, services: list[str]) -> WizardResult:
    kwargs = {
        "project_name": "testapp",
        "multi_region": False,
        "server_ip": "10.0.0.1",
        "domain_base": "example.com",
        "letsencrypt_email": "ops@example.com",
        "access_gateway": gateway,
        "selected_services": services,
        **gateway_kwargs,
    }
    return WizardResult(**kwargs)


# ── Parametrized validation ──────────────────────────────────────────


@pytest.mark.parametrize(
    "gateway,gateway_kwargs",
    _GATEWAYS,
    ids=[g[0] for g in _GATEWAYS],
)
@pytest.mark.parametrize(
    "services,combo_name",
    _SERVICE_COMBOS,
    ids=[c[1] for c in _SERVICE_COMBOS],
)
class TestTemplateMatrix:
    """Render and validate every gateway x service combination."""

    def test_renders_valid_yaml(
        self,
        tmp_path: Path,
        gateway: str,
        gateway_kwargs: dict,
        services: list[str],
        combo_name: str,
    ) -> None:
        result = _make_result(gateway, gateway_kwargs, services)
        scaffold(result, tmp_path)

        for yml_file in tmp_path.rglob("*.yml"):
            rel = str(yml_file.relative_to(tmp_path))
            if rel in _JINJA2_ALLOWED and rel not in _JINJA2_YAML_MUST_PARSE:
                continue
            content = yml_file.read_text()
            parsed = yaml.safe_load(content)
            assert parsed is None or isinstance(parsed, (dict, list)), (
                f"{rel} is not valid YAML (gateway={gateway}, services={combo_name})"
            )

    def test_no_jinja2_artifacts(
        self,
        tmp_path: Path,
        gateway: str,
        gateway_kwargs: dict,
        services: list[str],
        combo_name: str,
    ) -> None:
        result = _make_result(gateway, gateway_kwargs, services)
        scaffold(result, tmp_path)

        for yml_file in tmp_path.rglob("*.yml"):
            rel = str(yml_file.relative_to(tmp_path))
            if rel in _JINJA2_ALLOWED:
                continue
            content = yml_file.read_text()
            # Multi-region templates intentionally contain {{ domain_base }};
            # this test is single-server only, so nothing should remain
            matches = _JINJA2_RE.findall(content)
            assert not matches, (
                f"{rel} has unresolved Jinja2: {matches} "
                f"(gateway={gateway}, services={combo_name})"
            )

    def test_services_yml_structure(
        self,
        tmp_path: Path,
        gateway: str,
        gateway_kwargs: dict,
        services: list[str],
        combo_name: str,
    ) -> None:
        result = _make_result(gateway, gateway_kwargs, services)
        scaffold(result, tmp_path)

        content = (tmp_path / "group_vars/all/services.yml").read_text()
        parsed = yaml.safe_load(content)

        assert "services" in parsed
        assert "accessories" in parsed

        # Verify selected services appear
        known_services = {"gatus", "vaultwarden", "n8n", "plausible", "umami"}
        known_accessories = {"postgres", "redis", "mariadb"}

        for svc_id in services:
            if svc_id in known_services:
                assert svc_id in parsed["services"], (
                    f"expected service {svc_id} in output"
                )
            elif svc_id in known_accessories:
                assert svc_id in parsed["accessories"], (
                    f"expected accessory {svc_id} in output"
                )

    def test_access_gateway_yml(
        self,
        tmp_path: Path,
        gateway: str,
        gateway_kwargs: dict,
        services: list[str],
        combo_name: str,
    ) -> None:
        result = _make_result(gateway, gateway_kwargs, services)
        scaffold(result, tmp_path)

        content = (tmp_path / "group_vars/all/access_gateway.yml").read_text()
        assert f"access_gateway: {gateway}" in content

        if gateway == "headscale":
            assert "headscale_domain:" in content
        elif gateway == "wireguard":
            assert "headscale_domain" not in content


# ── Multi-region template validation ─────────────────────────────────


class TestMultiRegionTemplates:
    """Verify multi-region templates use domain_base variable."""

    @pytest.mark.parametrize(
        "services",
        [
            ["gatus"],
            ["vaultwarden"],
            ["gatus", "n8n", "plausible", "umami", "postgres"],
        ],
        ids=["gatus", "vaultwarden", "full"],
    )
    def test_multi_region_uses_domain_variable(
        self, tmp_path: Path, services: list[str]
    ) -> None:
        from bay_cli.wizard.models import RegionConfig

        result = WizardResult(
            project_name="testapp",
            multi_region=True,
            regions=[
                RegionConfig(name="eu", server_ip="10.0.0.1"),
                RegionConfig(name="na", server_ip="10.0.0.2"),
            ],
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="headscale",
            headscale_domain="hs.example.com",
            selected_services=services,
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/services.yml").read_text()
        parsed = yaml.safe_load(content)

        if parsed["services"] and parsed["services"] != {}:
            # All domains should use {{ domain_base }} variable
            assert "{{ domain_base }}" in content
