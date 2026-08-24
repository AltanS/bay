"""Unit tests for the ``service remove`` and ``server remove`` CLI commands.

Tests cover dependency blocking, dry-run mode, JSON output, idempotency,
and successful removal for both service and server remove commands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.config import StackConfig
from bay_cli.console import output as console_output
from bay_cli.inventory import InventoryConfig

runner = CliRunner()


# ── Filesystem helpers ────────────────────────────────────────────────────


def _write_services_yml(root: Path, content: str) -> Path:
    """Write services.yml and return the path."""
    svc_path = root / "group_vars" / "all" / "services.yml"
    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(content)
    return svc_path


def _write_main_yml(root: Path, domain_base: str = "example.com") -> None:
    """Write group_vars/production/main.yml with domain_base."""
    main_path = root / "group_vars" / "production" / "main.yml"
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(f"---\ndomain_base: {domain_base}\n")


def _write_inventory(root: Path, content: str) -> Path:
    """Write hosts/production and return the path."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(content)
    return inv_path


def _setup_consumer(
    root: Path,
    services_yml: str = "---\nservices: {}\naccessories: {}\n",
    domain_base: str = "example.com",
) -> None:
    """Set up a minimal consumer directory for testing."""
    _write_services_yml(root, services_yml)
    _write_main_yml(root, domain_base)
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text("[production]\n10.0.0.1\n")


# ── Monkeypatch helpers ──────────────────────────────────────────────────


def _patch_service_module(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    """Monkeypatch _get_config() and _get_catalog() in service.py."""
    from bay_cli.commands import service as service_mod

    def mock_get_config():
        return StackConfig(root)

    def mock_get_catalog():
        from bay_cli.catalog import _package_framework_root, load_catalog

        fw_root = _package_framework_root()
        return load_catalog(fw_root, root)

    monkeypatch.setattr(service_mod, "_get_config", mock_get_config)
    monkeypatch.setattr(service_mod, "_get_catalog", mock_get_catalog)


def _patch_server_module(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    """Monkeypatch _get_inventory() in the server module."""
    from bay_cli.commands import server as server_mod

    def mock_get_inventory(requested_env: str = "production") -> tuple[InventoryConfig, Path]:
        inv_path = root / "hosts" / requested_env
        if not inv_path.is_file():
            from bay_cli.errors import BayError

            raise BayError.config(
                f"Inventory file not found: {inv_path}",
                hint=f"Create {inv_path} or run 'bin/bay setup'",
            )
        inv = InventoryConfig()
        inv.load(inv_path)
        return inv, root

    monkeypatch.setattr(server_mod, "_get_inventory", mock_get_inventory)


@pytest.fixture(autouse=True)
def _reset_console_state():
    """Reset console module global state between tests."""
    console_output.set_json_mode(False)
    console_output.set_yes_mode(False)
    console_output._message_buffer.clear()
    yield
    console_output.set_json_mode(False)
    console_output.set_yes_mode(False)
    console_output._message_buffer.clear()


# ═══════════════════════════════════════════════════════════════════════════
# SERVICE REMOVE
# ═══════════════════════════════════════════════════════════════════════════


# ── 1. Dependency check blocks removal ────────────────────────────────────


class TestServiceRemoveDependencyBlock:
    """service remove postgres --yes raises DEPENDENCY_ERROR when n8n depends on it."""

    def test_dependency_blocks_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = (
            "---\n"
            "services:\n"
            "  n8n:\n"
            "    image: n8nio/n8n:latest\n"
            "    domains:\n"
            "      - n8n.example.com\n"
            "    access: vpn\n"
            "    depends_on:\n"
            "      - postgres\n"
            "accessories:\n"
            "  postgres:\n"
            "    image: postgres:16\n"
            "    port: '127.0.0.1:5432:5432'\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "service", "remove", "postgres"])

        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "DEPENDENCY_ERROR"


# ── 2. Dry-run shows diff without writing ────────────────────────────────


class TestServiceRemoveDryRun:
    """service remove gatus --dry-run shows diff without modifying files."""

    def test_dry_run_shows_diff_and_preserves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = (
            "---\n"
            "services:\n"
            "  gatus:\n"
            "    image: twinproduction/gatus:latest\n"
            "    domains:\n"
            "      - status.example.com\n"
            "    access: public\n"
            "accessories: {}\n"
        )
        svc_path = _write_services_yml(tmp_path, services_yml)
        _write_main_yml(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        original_bytes = svc_path.read_bytes()

        result = runner.invoke(app, ["service", "remove", "gatus", "--dry-run"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        # Diff output should mention gatus being removed
        assert "gatus" in result.stdout
        # File must be byte-identical to original
        assert svc_path.read_bytes() == original_bytes


# ── 3. Idempotent — nonexistent service ──────────────────────────────────


class TestServiceRemoveIdempotent:
    """Removing a service that does not exist is a no-op."""

    def test_nonexistent_service_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "service", "remove", "nonexistent"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        assert "nothing to remove" in result.stdout.lower()


# ── 4. Successful removal with --yes ─────────────────────────────────────


class TestServiceRemoveSuccess:
    """service remove gatus --yes removes gatus from services.yml."""

    def test_successful_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = (
            "---\n"
            "services:\n"
            "  gatus:\n"
            "    image: twinproduction/gatus:latest\n"
            "    domains:\n"
            "      - status.example.com\n"
            "    access: public\n"
            "accessories: {}\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "service", "remove", "gatus"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"

        # Verify gatus is removed from the config
        cfg = StackConfig(tmp_path)
        assert cfg.get_service("gatus") is None


# ── 5. JSON output ───────────────────────────────────────────────────────


class TestServiceRemoveJsonOutput:
    """--json service remove gatus --dry-run returns structured JSON with warnings."""

    def test_json_output_with_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = (
            "---\n"
            "services:\n"
            "  gatus:\n"
            "    image: twinproduction/gatus:latest\n"
            "    domains:\n"
            "      - status.example.com\n"
            "    access: public\n"
            "accessories: {}\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app, ["--json", "service", "remove", "gatus", "--dry-run"]
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["dry_run"] is True
        assert isinstance(data["data"]["warnings"], list)
        assert len(data["data"]["warnings"]) > 0


# ═══════════════════════════════════════════════════════════════════════════
# SERVER REMOVE
# ═══════════════════════════════════════════════════════════════════════════


# ── 6. Idempotent — nonexistent IP ───────────────────────────────────────


class TestServerRemoveIdempotent:
    """Removing an IP that is not in the inventory is a no-op."""

    def test_nonexistent_ip_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "server", "remove", "9.9.9.9"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        assert "nothing to remove" in result.stdout.lower()


# ── 7. Dry-run shows diff ───────────────────────────────────────────────


class TestServerRemoveDryRun:
    """server remove 1.2.3.4 --dry-run shows diff without modifying inventory."""

    def test_dry_run_shows_diff_and_preserves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv_path = _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        original_bytes = inv_path.read_bytes()

        result = runner.invoke(app, ["server", "remove", "1.2.3.4", "--dry-run"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        # Diff output should mention the IP
        assert "1.2.3.4" in result.stdout
        # Inventory file must be unchanged
        assert inv_path.read_bytes() == original_bytes


# ── 8. Successful removal with --yes ─────────────────────────────────────


class TestServerRemoveSuccess:
    """--yes server remove 1.2.3.4 removes the IP from inventory."""

    def test_successful_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "server", "remove", "1.2.3.4"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"

        # Verify the IP is removed from inventory
        inv = InventoryConfig()
        inv.load(tmp_path / "hosts" / "production")
        hosts = inv.list_hosts()
        ips = [h["ip"] for h in hosts]
        assert "1.2.3.4" not in ips
