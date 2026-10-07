"""Unit tests for the ``server remove`` CLI command: idempotency, dry-run and removal."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.console import output as console_output
from bay_cli.inventory import InventoryConfig

runner = CliRunner()


# ── Filesystem helpers ────────────────────────────────────────────────────


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


def _patch_server_module(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    """Monkeypatch _get_inventory() in the server module."""
    from bay_cli.commands import server as server_mod

    def mock_get_inventory(requested_env: str = "production", cx=None) -> tuple[InventoryConfig, Path]:
        inv_path = root / "hosts" / requested_env
        if not inv_path.is_file():
            from bay_cli.errors import BayError

            raise BayError.config(
                f"Inventory file not found: {inv_path}",
                hint=f"Create {inv_path}",
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
