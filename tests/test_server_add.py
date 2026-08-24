"""Unit tests for the ``server add`` CLI command.

Tests cover dry-run diff, JSON output, idempotency, cross-region conflict
detection, auto-creation of region group_vars, and single-server replacement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.console import output as console_output
from bay_cli.inventory import InventoryConfig

runner = CliRunner()


# ── Filesystem helpers ────────────────────────────────────────────────────


def _write_inventory(root: Path, content: str) -> Path:
    """Write hosts/production and return the path."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(content)
    return inv_path


def _write_main_yml(root: Path, domain_base: str = "example.com") -> None:
    """Write group_vars/production/main.yml with domain_base."""
    main_path = root / "group_vars" / "production" / "main.yml"
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(f"---\ndomain_base: {domain_base}\n")


# ── Monkeypatch helpers ──────────────────────────────────────────────────


def _patch_server_module(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    env: str = "production",
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


# ── 1. Dry-run shows diff ────────────────────────────────────────────────


class TestDryRunShowsDiff:
    """server add 5.6.7.8 --dry-run shows diff without modifying inventory."""

    def test_dry_run_shows_diff_and_preserves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inv_path = _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        original_bytes = inv_path.read_bytes()

        result = runner.invoke(app, ["--yes", "server", "add", "5.6.7.8", "--dry-run"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        # Diff output should mention the new IP
        assert "5.6.7.8" in result.stdout
        # Inventory file must be unchanged
        assert inv_path.read_bytes() == original_bytes


# ── 2. JSON dry-run ──────────────────────────────────────────────────────


class TestJsonDryRun:
    """--json server add 5.6.7.8 --dry-run returns structured JSON."""

    def test_json_dry_run_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app, ["--json", "--yes", "server", "add", "5.6.7.8", "--dry-run"]
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["dry_run"] is True
        assert data["data"]["added"] is True


# ── 3. Idempotency — same IP already exists ──────────────────────────────


class TestIdempotency:
    """Adding an IP that already exists in the same group is a no-op."""

    def test_already_exists_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["server", "add", "1.2.3.4"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"
        assert "already" in result.stdout.lower()


# ── 4. Conflict — IP in different region ─────────────────────────────────


class TestConflictDifferentRegion:
    """Adding an IP that exists in a different region raises CONFLICT."""

    def test_conflict_exits_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        multi_region_content = (
            "[production:children]\neu\nna\n\n"
            "[eu]\n1.2.3.4\n\n"
            "[na]\n5.6.7.8\n"
        )
        _write_inventory(tmp_path, multi_region_content)
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["server", "add", "1.2.3.4", "--region", "na"])

        # BayError.conflict raises an exception with exit_code 2
        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "CONFLICT"


# ── 5. Auto-create group_vars for new region ─────────────────────────────


class TestAutoCreateGroupVars:
    """Adding a server with --region creates group_vars/<region>/main.yml."""

    def test_group_vars_created_with_domain_stub(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Single-server inventory; we'll add with a new region which will
        # cause the inventory to gain a new group section.
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path, domain_base="example.com")
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app, ["--yes", "server", "add", "9.8.7.6", "--region", "eu-west"]
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"

        region_main = tmp_path / "group_vars" / "eu-west" / "main.yml"
        assert region_main.is_file(), "group_vars/eu-west/main.yml should be created"

        content = region_main.read_text()
        assert "domain_base" in content
        assert "eu-west" in content


# ── 6. Single-server replacement warning ─────────────────────────────────


class TestSingleServerReplacement:
    """In single-server mode, adding a different IP replaces the old host."""

    def test_replacement_with_yes_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_inventory(tmp_path, "[production]\n1.2.3.4\n")
        _write_main_yml(tmp_path)
        _patch_server_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["--yes", "server", "add", "5.6.7.8"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nexc={result.exception}"

        # Verify the inventory now has the new IP and not the old one
        inv = InventoryConfig()
        inv.load(tmp_path / "hosts" / "production")
        hosts = inv.list_hosts()
        ips = [h["ip"] for h in hosts]
        assert "5.6.7.8" in ips
        assert "1.2.3.4" not in ips
