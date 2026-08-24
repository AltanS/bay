"""Tests for CLI flag paths in `bay setup`.

Verify that all-flags skips wizard, partial-flags pre-fills,
and no-flags preserves existing behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bay_cli.commands.framework import (
    _SetupFlags,
    _build_result_from_flags,
    _flags_to_prefill,
)
from bay_cli.errors import BayError
from bay_cli.wizard.models import WizardResult


# ── _SetupFlags ──────────────────────────────────────────────────────────


class TestSetupFlags:
    """Flag parsing and required-flag detection."""

    def _make(self, **kwargs) -> _SetupFlags:
        defaults = dict(
            name=None, server_ip=None, domain=None, gateway=None,
            headscale_domain=None, services=None, letsencrypt_email=None,
            multi_region=False, vpn_peer_ips=None,
        )
        defaults.update(kwargs)
        return _SetupFlags(**defaults)

    def test_has_any_false_when_empty(self) -> None:
        assert self._make().has_any() is False

    def test_has_any_true_with_name(self) -> None:
        assert self._make(name="myapp").has_any() is True

    def test_has_all_required_single_server(self) -> None:
        flags = self._make(name="myapp", server_ip="1.2.3.4", domain="example.com", gateway="none")
        assert flags.has_all_required() is True

    def test_has_all_required_headscale_needs_domain(self) -> None:
        flags = self._make(name="myapp", server_ip="1.2.3.4", domain="example.com", gateway="headscale")
        assert flags.has_all_required() is False

    def test_has_all_required_headscale_with_domain(self) -> None:
        flags = self._make(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="headscale", headscale_domain="hs.example.com",
        )
        assert flags.has_all_required() is True

    def test_has_all_required_missing_server_ip(self) -> None:
        flags = self._make(name="myapp", domain="example.com", gateway="none")
        assert flags.has_all_required() is False

    def test_parse_services_default(self) -> None:
        assert self._make().parse_services() == ["gatus"]

    def test_parse_services_comma_list(self) -> None:
        assert self._make(services="gatus,vaultwarden,postgres").parse_services() == [
            "gatus", "vaultwarden", "postgres",
        ]

    def test_parse_vpn_peer_ips(self) -> None:
        assert self._make(vpn_peer_ips="10.0.0.1,10.0.0.2").parse_vpn_peer_ips() == [
            "10.0.0.1", "10.0.0.2",
        ]


# ── _build_result_from_flags ────────────────────────────────────────────


class TestBuildResultFromFlags:
    """All-flags path: build WizardResult directly."""

    def test_basic_none_gateway(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="none", headscale_domain=None, services="gatus",
            letsencrypt_email="admin@example.com", multi_region=False,
            vpn_peer_ips=None,
        )
        result = _build_result_from_flags(flags)
        assert isinstance(result, WizardResult)
        assert result.project_name == "myapp"
        assert result.server_ip == "1.2.3.4"
        assert result.domain_base == "example.com"
        assert result.access_gateway == "none"
        assert result.selected_services == ["gatus"]

    def test_headscale_gateway(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="headscale", headscale_domain="hs.example.com",
            services="gatus,vaultwarden", letsencrypt_email=None,
            multi_region=False, vpn_peer_ips=None,
        )
        result = _build_result_from_flags(flags)
        assert result.access_gateway == "headscale"
        assert result.headscale_domain == "hs.example.com"
        assert "gatus" in result.selected_services
        assert "vaultwarden" in result.selected_services

    def test_auto_adds_dependencies(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="none", headscale_domain=None, services="plausible",
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        result = _build_result_from_flags(flags)
        assert "plausible" in result.selected_services
        assert "postgres" in result.selected_services

    def test_default_letsencrypt_email(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="none", headscale_domain=None, services=None,
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        result = _build_result_from_flags(flags)
        assert result.letsencrypt_email == "admin@example.com"

    def test_invalid_gateway_raises(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="invalid", headscale_domain=None, services=None,
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        with pytest.raises(BayError, match="Invalid gateway"):
            _build_result_from_flags(flags)

    def test_headscale_without_domain_raises(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="headscale", headscale_domain=None, services=None,
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        with pytest.raises(BayError, match="--headscale-domain is required"):
            _build_result_from_flags(flags)

    def test_unknown_service_raises(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="none", headscale_domain=None, services="gatus,unknown_svc",
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        with pytest.raises(BayError, match="Unknown service"):
            _build_result_from_flags(flags)

    def test_scaffold_from_flags(self, tmp_path: Path) -> None:
        """Full integration: flags → WizardResult → scaffold → valid YAML."""
        from bay_cli.wizard.scaffold import scaffold

        flags = _SetupFlags(
            name="testapp", server_ip="10.0.0.1", domain="test.com",
            gateway="none", headscale_domain=None, services="gatus,postgres",
            letsencrypt_email="ops@test.com", multi_region=False,
            vpn_peer_ips=None,
        )
        result = _build_result_from_flags(flags)
        scaffold(result, tmp_path)

        services_yml = tmp_path / "group_vars" / "all" / "services.yml"
        assert services_yml.exists()
        parsed = yaml.safe_load(services_yml.read_text())
        assert "gatus" in parsed["services"]
        assert "postgres" in parsed["accessories"]


# ── _flags_to_prefill ───────────────────────────────────────────────────


class TestFlagsToPrefill:
    """Partial-flags path: build prefill WizardResult."""

    def test_name_only(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip=None, domain=None, gateway=None,
            headscale_domain=None, services=None, letsencrypt_email=None,
            multi_region=False, vpn_peer_ips=None,
        )
        result = _flags_to_prefill(flags)
        assert result.project_name == "myapp"
        assert result.access_gateway == "headscale"  # default

    def test_domain_flag_sets_headscale_domain(self) -> None:
        flags = _SetupFlags(
            name=None, server_ip=None, domain="example.com", gateway=None,
            headscale_domain=None, services=None, letsencrypt_email=None,
            multi_region=False, vpn_peer_ips=None,
        )
        result = _flags_to_prefill(flags)
        assert result.domain_base == "example.com"
        assert result.headscale_domain == "hs.example.com"

    def test_gateway_none_prefill(self) -> None:
        flags = _SetupFlags(
            name="myapp", server_ip="1.2.3.4", domain="example.com",
            gateway="none", headscale_domain=None, services=None,
            letsencrypt_email=None, multi_region=False, vpn_peer_ips=None,
        )
        result = _flags_to_prefill(flags)
        assert result.access_gateway == "none"
