"""Integration tests for bay_cli.wizard.scaffold — template rendering."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from bay_cli.wizard.models import RegionConfig, SSHKey, WizardResult
from bay_cli.wizard.scaffold import copy_examples, scaffold

# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def single_server_result() -> WizardResult:
    """A minimal single-server WizardResult for testing."""
    return WizardResult(
        project_name="testapp",
        multi_region=False,
        server_ip="10.0.0.1",
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="headscale",
        headscale_domain="hs.example.com",
        selected_services=["gatus", "postgres", "redis"],
    )


@pytest.fixture()
def multi_region_result() -> WizardResult:
    """A multi-region WizardResult with two regions."""
    return WizardResult(
        project_name="testapp",
        multi_region=True,
        regions=[
            RegionConfig(name="eu", server_ip="10.0.0.1"),
            RegionConfig(name="na", server_ip="10.0.0.2"),
        ],
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="headscale",
        headscale_domain="hs.example.com",
        selected_services=["gatus", "postgres", "redis"],
    )


# ── Expected files ───────────────────────────────────────────────────────

# The 18 files rendered from _TEMPLATES for every scaffold run
_COMMON_FILES = [
    "group_vars/all/main.yml",
    "group_vars/all/services.yml",
    "group_vars/all/users.yml",
    "group_vars/all/security.yml",
    "group_vars/all/vpn_access.yml",
    "group_vars/all/access_gateway.yml",
    "group_vars/production/main.yml",
    "group_vars/production/domains.yml",
    "group_vars/production/secrets.yml",
    "hosts/production",
    "ansible.cfg",
    "deploy.yml",
    "provision.yml",
    "restore.yml",
    "Makefile",
    ".gitignore",
    "README.md",
    "tests/test_infra.sh",
]


# ── Single-server scaffold ───────────────────────────────────────────────


class TestSingleServerScaffold:
    """Scaffold output for a single-server configuration."""

    def test_creates_all_expected_files(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        created = scaffold(single_server_result, tmp_path)
        # 20 rendered templates + gatus's catalog config file
        assert len(created) == 21
        for rel in _COMMON_FILES:
            assert (tmp_path / rel).exists(), f"missing: {rel}"

    def test_stack_name_in_main(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        main = (tmp_path / "group_vars/all/main.yml").read_text()
        assert "stack_name: testapp" in main

    def test_server_ip_in_inventory(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        inventory = (tmp_path / "hosts/production").read_text()
        assert "10.0.0.1" in inventory

    def test_letsencrypt_email_in_domains(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        domains = (tmp_path / "group_vars/production/domains.yml").read_text()
        assert "letsencrypt_email: ops@example.com" in domains

    def test_domain_base_in_domains_single_server(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        domains = (tmp_path / "group_vars/production/domains.yml").read_text()
        assert "domain_base: example.com" in domains

    def test_no_multi_region_files(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        # No per-region group_vars directories should exist
        assert not (tmp_path / "group_vars/eu").exists()
        assert not (tmp_path / "group_vars/na").exists()

    def test_all_yaml_files_parseable(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        # Some generated YAML files contain Ansible Jinja2 expressions
        # (e.g., {{ vpn_allowed_ips + [...] }}) that are not valid strict
        # YAML.  Skip those files in the parse check.
        _JINJA2_FILES = {"group_vars/all/security.yml"}

        scaffold(single_server_result, tmp_path)
        for rel in _COMMON_FILES:
            path = tmp_path / rel
            if path.suffix == ".yml" and rel not in _JINJA2_FILES:
                content = path.read_text()
                parsed = yaml.safe_load(content)
                # safe_load returns None for comment-only files, dict for
                # variable files, or list for Ansible playbooks
                assert parsed is None or isinstance(parsed, (dict, list)), (
                    f"{rel} did not parse as valid YAML: {type(parsed)}"
                )


# ── Multi-region scaffold ────────────────────────────────────────────────


class TestMultiRegionScaffold:
    """Scaffold output for a multi-region configuration."""

    def test_creates_all_files(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        created = scaffold(multi_region_result, tmp_path)
        # 20 common + 2 per-region main.yml + gatus's catalog config file
        assert len(created) == 23

    def test_per_region_group_vars_created(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        scaffold(multi_region_result, tmp_path)
        eu_main = tmp_path / "group_vars/eu/main.yml"
        na_main = tmp_path / "group_vars/na/main.yml"
        assert eu_main.exists()
        assert na_main.exists()

    def test_per_region_domain_base(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        scaffold(multi_region_result, tmp_path)
        eu_content = (tmp_path / "group_vars/eu/main.yml").read_text()
        na_content = (tmp_path / "group_vars/na/main.yml").read_text()
        assert "domain_base: eu.example.com" in eu_content
        assert "domain_base: na.example.com" in na_content

    def test_inventory_has_children_structure(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        scaffold(multi_region_result, tmp_path)
        inventory = (tmp_path / "hosts/production").read_text()
        assert "[production:children]" in inventory
        assert "[eu]" in inventory
        assert "[na]" in inventory
        assert "10.0.0.1" in inventory
        assert "10.0.0.2" in inventory

    def test_domain_base_not_in_production_domains(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        scaffold(multi_region_result, tmp_path)
        domains = (tmp_path / "group_vars/production/domains.yml").read_text()
        # domain_base should NOT be set at production level for multi-region
        # (it is set per-region instead)
        assert "domain_base:" not in domains

    def test_services_use_domain_base_variable(
        self, tmp_path: Path, multi_region_result: WizardResult
    ) -> None:
        scaffold(multi_region_result, tmp_path)
        services = (tmp_path / "group_vars/all/services.yml").read_text()
        # Multi-region services reference the variable, not a literal domain
        assert "{{ domain_base }}" in services


# ── Service presets ──────────────────────────────────────────────────────


class TestServiceSelection:
    """Different selected_services lists produce different services.yml content."""

    def _render_services(self, tmp_path: Path, selected: list[str]) -> str:
        result = WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="10.0.0.1",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="none",
            selected_services=selected,
        )
        scaffold(result, tmp_path)
        return (tmp_path / "group_vars/all/services.yml").read_text()

    def test_gatus_with_accessories(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["gatus", "postgres", "redis"])
        parsed = yaml.safe_load(content)
        assert "gatus" in parsed["services"]
        assert "postgres" in parsed["accessories"]
        assert "redis" in parsed["accessories"]

    def test_gatus_only(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["gatus"])
        parsed = yaml.safe_load(content)
        assert "gatus" in parsed["services"]
        assert parsed.get("accessories") is None or parsed["accessories"] == {}

    def test_blank_selection(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, [])
        parsed = yaml.safe_load(content)
        assert parsed["services"] == {}
        assert parsed["accessories"] == {}

    def test_mariadb_accessory(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["mariadb"])
        parsed = yaml.safe_load(content)
        assert parsed["services"] == {}
        assert "mariadb" in parsed["accessories"]

    def test_vaultwarden_service(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["vaultwarden"])
        parsed = yaml.safe_load(content)
        assert "vaultwarden" in parsed["services"]
        svc = parsed["services"]["vaultwarden"]
        assert svc["image"] == "vaultwarden/server:latest"
        assert svc["ports"]["internal"] == 80

    def test_n8n_with_postgres(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["n8n", "postgres"])
        parsed = yaml.safe_load(content)
        assert "n8n" in parsed["services"]
        assert "postgres" in parsed["accessories"]
        assert parsed["services"]["n8n"]["depends_on"] == ["postgres"]

    def test_plausible_with_postgres(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["plausible", "postgres"])
        parsed = yaml.safe_load(content)
        assert "plausible" in parsed["services"]
        assert parsed["services"]["plausible"]["access"] == "public"
        assert "postgres" in parsed["accessories"]

    def test_umami_with_postgres(self, tmp_path: Path) -> None:
        content = self._render_services(tmp_path, ["umami", "postgres"])
        parsed = yaml.safe_load(content)
        assert "umami" in parsed["services"]
        assert parsed["services"]["umami"]["access"] == "public"
        assert "postgres" in parsed["accessories"]

    def test_vaultwarden_vpn_access_with_gateway(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="10.0.0.1",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="headscale",
            headscale_domain="hs.example.com",
            selected_services=["vaultwarden"],
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/services.yml").read_text()
        parsed = yaml.safe_load(content)
        assert parsed["services"]["vaultwarden"]["access"] == "vpn"

    def test_all_services_together(self, tmp_path: Path) -> None:
        content = self._render_services(
            tmp_path,
            ["gatus", "vaultwarden", "n8n", "plausible", "umami", "postgres", "redis"],
        )
        parsed = yaml.safe_load(content)
        for svc in ("gatus", "vaultwarden", "n8n", "plausible", "umami"):
            assert svc in parsed["services"], f"missing service: {svc}"
        for acc in ("postgres", "redis"):
            assert acc in parsed["accessories"], f"missing accessory: {acc}"


# ── Idempotency ──────────────────────────────────────────────────────────


class TestIdempotency:
    """Pre-existing files must NOT be overwritten."""

    def test_existing_files_not_overwritten(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        # Create a file that would normally be generated
        main_path = tmp_path / "group_vars/all/main.yml"
        main_path.parent.mkdir(parents=True)
        sentinel = "# ORIGINAL CONTENT\n"
        main_path.write_text(sentinel)

        created = scaffold(single_server_result, tmp_path)

        # The pre-existing file should not be in the created list
        assert main_path not in created
        # Content must be preserved
        assert main_path.read_text() == sentinel

    def test_second_run_creates_nothing(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        second = scaffold(single_server_result, tmp_path)
        assert second == []


# ── SSH keys ─────────────────────────────────────────────────────────────


class TestSSHKeys:
    """SSH key rendering in users.yml."""

    def test_keys_appear_when_provided(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp",
            multi_region=False,
            server_ip="10.0.0.1",
            domain_base="example.com",
            letsencrypt_email="ops@example.com",
            access_gateway="none",
            ssh_keys=[
                SSHKey(
                    username="alice",
                    public_key="ssh-ed25519 AAAAC3Nz... alice@laptop",
                    source="github",
                ),
            ],
            selected_services=["gatus", "postgres", "redis"],
        )
        scaffold(result, tmp_path)
        users = (tmp_path / "group_vars/all/users.yml").read_text()
        assert "ssh-ed25519 AAAAC3Nz... alice@laptop" in users

    def test_placeholder_when_no_keys(
        self, tmp_path: Path, single_server_result: WizardResult
    ) -> None:
        scaffold(single_server_result, tmp_path)
        users = (tmp_path / "group_vars/all/users.yml").read_text()
        # When no keys are provided, the template leaves a comment placeholder
        assert "# Add GitHub username" in users or "[]" in users


# ── copy_examples ────────────────────────────────────────────────────────


class TestCopyExamples:
    """copy_examples() copies from framework example/ into target."""

    @pytest.fixture()
    def bay_dir(self) -> Path:
        """Return the real framework root (this repo's checkout) so example/ is available."""
        framework_dir = Path(__file__).resolve().parent.parent
        assert (framework_dir / "example").is_dir(), (
            "Cannot find the framework repo's example/ directory"
        )
        return framework_dir

    def test_copies_all_example_files(
        self, tmp_path: Path, bay_dir: Path
    ) -> None:
        copy_examples(bay_dir, tmp_path)
        example_dir = bay_dir / "example"
        for root, _dirs, files in os.walk(example_dir):
            root_path = Path(root)
            for fname in files:
                rel = (root_path / fname).relative_to(example_dir)
                assert (tmp_path / rel).exists(), f"missing copied file: {rel}"

    def test_skips_existing_files(
        self, tmp_path: Path, bay_dir: Path
    ) -> None:
        # Pre-create one file that exists in example/
        deploy = tmp_path / "deploy.yml"
        deploy.write_text("# ORIGINAL\n")

        copy_examples(bay_dir, tmp_path)

        # The pre-existing file should be untouched
        assert deploy.read_text() == "# ORIGINAL\n"


# ── Access gateway templates ────────────────────────────────────────


class TestGatewayTemplates:
    """Verify access gateway template rendering for each gateway type."""

    def test_headscale_access_gateway_yml(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="headscale", headscale_domain="hs.example.com",
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/access_gateway.yml").read_text()
        assert "access_gateway: headscale" in content
        assert "headscale_domain: hs.example.com" in content

    def test_headscale_vpn_access_yml(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="headscale", headscale_domain="hs.example.com",
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/vpn_access.yml").read_text()
        assert "100.64.0.0/10" in content

    def test_headscale_security_ports(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="headscale", headscale_domain="hs.example.com",
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/security.yml").read_text()
        assert "41641" in content
        assert "3478" in content

    def test_wireguard_access_gateway_yml(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="wireguard", vpn_peer_ips=["10.0.0.2"],
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/access_gateway.yml").read_text()
        assert "access_gateway: wireguard" in content
        assert "headscale_domain" not in content

    def test_wireguard_vpn_access_yml(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="wireguard", vpn_peer_ips=["10.0.0.2", "10.0.0.3"],
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/vpn_access.yml").read_text()
        assert "10.0.0.2" in content
        assert "10.0.0.3" in content
        assert "100.64.0.0/10" not in content

    def test_none_access_gateway_yml(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="none",
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/access_gateway.yml").read_text()
        assert "access_gateway: none" in content

    def test_none_security_no_headscale_ports(self, tmp_path: Path) -> None:
        result = WizardResult(
            project_name="testapp", multi_region=False, server_ip="10.0.0.1",
            domain_base="example.com", letsencrypt_email="ops@example.com",
            access_gateway="none",
        )
        scaffold(result, tmp_path)
        content = (tmp_path / "group_vars/all/security.yml").read_text()
        assert "41641" not in content
