"""Unit tests for bay_cli.wizard.models — validators and data models."""

from __future__ import annotations

import pytest

from bay_cli.errors import BayError
from bay_cli.wizard.models import (
    RegionConfig,
    SSHKey,
    WizardResult,
    defaults_result,
    load_existing_config,
    validate_domain,
    validate_ip,
    validate_project_name,
    validate_region_name,
)


# ── validate_project_name ────────────────────────────────────────────────


class TestValidateProjectName:
    """DNS-safe project name: lowercase alpha start, [a-z0-9-], max 63."""

    @pytest.mark.parametrize(
        "name",
        [
            "my-project",
            "a",
            "a123",
            "test-project-1",
        ],
    )
    def test_valid_names(self, name: str) -> None:
        assert validate_project_name(name) == name

    def test_empty_string_raises(self) -> None:
        with pytest.raises(BayError, match="cannot be empty"):
            validate_project_name("")

    def test_uppercase_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid project name"):
            validate_project_name("A")

    def test_starts_with_number_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid project name"):
            validate_project_name("1abc")

    def test_spaces_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid project name"):
            validate_project_name("my project")

    def test_too_long_raises(self) -> None:
        with pytest.raises(BayError, match="63 characters"):
            validate_project_name("a" * 64)

    def test_underscores_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid project name"):
            validate_project_name("my_project")

    def test_strips_whitespace(self) -> None:
        assert validate_project_name("  my-project  ") == "my-project"


# ── validate_ip ──────────────────────────────────────────────────────────


class TestValidateIp:
    """Accept IPv4, IPv6, or hostname-with-dot; reject everything else."""

    @pytest.mark.parametrize(
        "value",
        [
            "192.168.1.1",
            "10.0.0.1",
            "::1",
            "2001:db8::1",
            "server.example.com",
        ],
    )
    def test_valid_addresses(self, value: str) -> None:
        assert validate_ip(value) == value

    def test_empty_string_raises(self) -> None:
        with pytest.raises(BayError, match="cannot be empty"):
            validate_ip("")

    def test_no_dot_no_ip_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid address"):
            validate_ip("not-valid")

    def test_out_of_range_octets_accepted_as_hostname(self) -> None:
        # 999.999.999.999 fails ip_address() but matches _HOSTNAME_RE and
        # contains a dot, so the validator accepts it as a hostname.
        assert validate_ip("999.999.999.999") == "999.999.999.999"

    def test_truly_invalid_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid address"):
            validate_ip("bad!")  # fails IP parse and hostname regex

    def test_strips_whitespace(self) -> None:
        assert validate_ip("  10.0.0.1  ") == "10.0.0.1"


# ── validate_domain ──────────────────────────────────────────────────────


class TestValidateDomain:
    """Requires at least one dot, no protocol, no spaces; lowercases."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("example.com", "example.com"),
            ("sub.example.com", "sub.example.com"),
            ("EXAMPLE.COM", "example.com"),
        ],
    )
    def test_valid_domains(self, raw: str, expected: str) -> None:
        assert validate_domain(raw) == expected

    def test_empty_string_raises(self) -> None:
        with pytest.raises(BayError, match="cannot be empty"):
            validate_domain("")

    def test_no_dot_raises(self) -> None:
        with pytest.raises(BayError, match="must contain at least one dot"):
            validate_domain("localhost")

    def test_protocol_prefix_raises(self) -> None:
        with pytest.raises(BayError, match="should not include protocol"):
            validate_domain("https://example.com")

    def test_space_raises(self) -> None:
        with pytest.raises(BayError, match="cannot contain spaces"):
            validate_domain("example .com")

    def test_strips_whitespace(self) -> None:
        assert validate_domain("  example.com  ") == "example.com"


# ── validate_region_name ─────────────────────────────────────────────────


class TestValidateRegionName:
    """Lowercase alpha start, alphanumeric only; auto-lowercases."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("eu", "eu"),
            ("na", "na"),
            ("ap1", "ap1"),
            ("EU", "eu"),  # lowered before regex check
        ],
    )
    def test_valid_names(self, raw: str, expected: str) -> None:
        assert validate_region_name(raw) == expected

    def test_empty_string_raises(self) -> None:
        with pytest.raises(BayError, match="cannot be empty"):
            validate_region_name("")

    def test_special_chars_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid region name"):
            validate_region_name("eu!")

    def test_starts_with_number_raises(self) -> None:
        with pytest.raises(BayError, match="Invalid region name"):
            validate_region_name("1eu")

    def test_strips_whitespace(self) -> None:
        assert validate_region_name("  eu  ") == "eu"


# ── defaults_result ──────────────────────────────────────────────────────


class TestDefaultsResult:
    """defaults_result() returns a single-server WizardResult with sane defaults."""

    def test_basic_directory_name(self) -> None:
        result = defaults_result("my-project")
        assert result.project_name == "my-project"
        assert result.multi_region is False
        assert result.server_ip == "0.0.0.0"
        assert result.domain_base == "example.com"
        assert result.letsencrypt_email == "admin@example.com"
        assert result.ssh_keys == []
        assert result.selected_services == ["gatus"]
        assert result.access_gateway == "headscale"
        assert result.headscale_domain == "hs.example.com"

    def test_sanitizes_special_chars(self) -> None:
        result = defaults_result("My_Cool.Project!")
        # Uppercase → lower, non-[a-z0-9-] → hyphen, stripped
        name = result.project_name
        assert name.islower() or "-" in name
        assert "_" not in name
        assert "." not in name
        assert "!" not in name
        # Must still start with a letter
        assert name[0].isalpha()

    def test_numeric_start_gets_prefix(self) -> None:
        result = defaults_result("123project")
        assert result.project_name.startswith("p-")

    def test_empty_directory_name(self) -> None:
        # After stripping non-alpha chars, fallback to "my-project"
        result = defaults_result("!!!")
        assert result.project_name == "my-project"


# ── Dataclass construction ───────────────────────────────────────────────


class TestDataclasses:
    """Verify SSHKey, RegionConfig, WizardResult construction."""

    def test_ssh_key(self) -> None:
        key = SSHKey(username="alice", public_key="ssh-ed25519 AAAA...", source="github")
        assert key.username == "alice"
        assert key.source == "github"

    def test_region_config(self) -> None:
        region = RegionConfig(name="eu", server_ip="10.0.0.1")
        assert region.name == "eu"
        assert region.server_ip == "10.0.0.1"

    def test_wizard_result_defaults_none_gateway(self) -> None:
        result = WizardResult(project_name="test", multi_region=False, access_gateway="none")
        assert result.server_ip is None
        assert result.regions is None
        assert result.domain_base == ""
        assert result.letsencrypt_email == ""
        assert result.ssh_keys == []
        assert result.selected_services == ["gatus"]
        assert result.access_gateway == "none"
        assert result.vpn_enabled is False

    def test_wizard_result_headscale_gateway(self) -> None:
        result = WizardResult(
            project_name="test", multi_region=False,
            access_gateway="headscale", headscale_domain="hs.example.com",
        )
        assert result.access_gateway == "headscale"
        assert result.headscale_domain == "hs.example.com"
        assert result.vpn_enabled is True

    def test_wizard_result_wireguard_gateway(self) -> None:
        result = WizardResult(
            project_name="test", multi_region=False,
            access_gateway="wireguard", vpn_peer_ips=["10.0.0.2"],
        )
        assert result.access_gateway == "wireguard"
        assert result.vpn_enabled is True

    def test_wizard_result_headscale_requires_domain(self) -> None:
        with pytest.raises(BayError, match="headscale_domain is required"):
            WizardResult(project_name="test", multi_region=False, access_gateway="headscale")

    def test_wizard_result_multi_region(self) -> None:
        regions = [
            RegionConfig(name="eu", server_ip="10.0.0.1"),
            RegionConfig(name="na", server_ip="10.0.0.2"),
        ]
        result = WizardResult(
            project_name="myapp",
            multi_region=True,
            regions=regions,
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="wireguard",
        )
        assert result.multi_region is True
        assert len(result.regions) == 2
        assert result.regions[0].name == "eu"


# ── load_existing_config ────────────────────────────────────────────────


class TestLoadExistingConfig:
    """Load an existing project's config back into a WizardResult."""

    def test_returns_none_when_no_main_yml(self, tmp_path) -> None:
        assert load_existing_config(tmp_path) is None

    def test_loads_single_server_config(self, tmp_path) -> None:
        from bay_cli.wizard.scaffold import scaffold

        original = WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="10.0.0.1",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="headscale",
            headscale_domain="hs.example.com",
            selected_services=["gatus", "postgres"],
        )
        scaffold(original, tmp_path)

        loaded = load_existing_config(tmp_path)
        assert loaded is not None
        assert loaded.project_name == "testapp"
        assert loaded.multi_region is False
        assert loaded.server_ip == "10.0.0.1"
        assert loaded.domain_base == "example.com"
        assert loaded.access_gateway == "headscale"
        assert loaded.headscale_domain == "hs.example.com"
        assert "gatus" in loaded.selected_services
        assert "postgres" in loaded.selected_services

    def test_loads_multi_region_config(self, tmp_path) -> None:
        from bay_cli.wizard.scaffold import scaffold

        original = WizardResult(
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
            selected_services=["gatus"],
        )
        scaffold(original, tmp_path)

        loaded = load_existing_config(tmp_path)
        assert loaded is not None
        assert loaded.multi_region is True
        assert loaded.regions is not None
        assert len(loaded.regions) == 2
        assert loaded.regions[0].name == "eu"
        assert loaded.regions[0].server_ip == "10.0.0.1"

    def test_loads_none_gateway(self, tmp_path) -> None:
        from bay_cli.wizard.scaffold import scaffold

        original = WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="10.0.0.1",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="none",
            selected_services=[],
        )
        scaffold(original, tmp_path)

        loaded = load_existing_config(tmp_path)
        assert loaded is not None
        assert loaded.access_gateway == "none"
