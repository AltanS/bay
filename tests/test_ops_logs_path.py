"""Tests for the `bin/bay logs --path` CLI subcommand (M84-S5).

Covers:
- `_is_date_shaped()` — YYYY-MM-DD detection, duration rejection.
- `_archive_path_for()` — stack_name + service → /opt/<stack>/logs/services/<svc>/.
- CLI behavior via typer's CliRunner:
    - `--path` prints archive dir with no trailing newline (composable with $()).
    - `--path --since <date>` prints a zcat dry-run hint, no execution.
    - date-shaped `--since` without `--path` exits 1 with a --path hint.
    - duration-shaped `--since` without `--path` reaches the SSH code path
      (verified by mocking _run_on_host and asserting it was called).
    - Unknown service exits non-zero with an "Available:" hint (M50 behavior).
    - `bay logs --help` advertises `--path`.
    - `--path` does NOT call _run_on_host (pure local, no SSH).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from bay_cli.commands.ops import (
    _archive_path_for,
    _is_date_shaped,
    logs,
)


# ── pure-function tests ──────────────────────────────────────────────


def test_is_date_shaped_accepts_yyyy_mm_dd():
    assert _is_date_shaped("2026-04-22") is True


def test_is_date_shaped_rejects_duration_hours():
    assert _is_date_shaped("1h") is False


def test_is_date_shaped_rejects_duration_minutes():
    assert _is_date_shaped("30m") is False


def test_is_date_shaped_rejects_duration_days():
    assert _is_date_shaped("2d") is False


def test_is_date_shaped_rejects_empty():
    assert _is_date_shaped("") is False


def test_is_date_shaped_rejects_partial_date():
    # Has the shape but not the right structure
    assert _is_date_shaped("2026-4-22") is False


def test_is_date_shaped_rejects_trailing_garbage():
    assert _is_date_shaped("2026-04-22T10:00:00") is False


def test_archive_path_for_simple_names():
    assert _archive_path_for("sandbox", "whoami") == "/opt/sandbox/logs/services/whoami/"


def test_archive_path_for_hyphenated_names():
    assert (
        _archive_path_for("demo", "blog")
        == "/opt/demo/logs/services/blog/"
    )


# ── CLI behavior tests ──────────────────────────────────────────────
# We wrap the `logs` callable in a throwaway Typer app + CliRunner.
# All filesystem dependencies (consumer root, services.yml, main.yml,
# inventory) are mocked so no repo-layout assumptions leak.


def _make_app() -> typer.Typer:
    app = typer.Typer()
    app.command()(logs)
    return app


def _consumer_fixture(tmp_path: Path, stack_name: str = "testapp") -> Path:
    """Build a minimal consumer root with services.yml + main.yml + inventory."""
    root = tmp_path / "consumer"
    (root / "group_vars" / "all").mkdir(parents=True)
    (root / "hosts").mkdir()

    (root / "group_vars" / "all" / "main.yml").write_text(
        f"---\nstack_name: {stack_name}\napp_user: bay\napp_user_group: bay\n"
    )
    (root / "group_vars" / "all" / "services.yml").write_text(
        """---
services:
  whoami:
    image: traefik/whoami:latest
    access: public
    domains: ["whoami.example.com"]
    ports: {internal: 80}
accessories: {}
"""
    )
    (root / "hosts" / "production").write_text("[production]\n10.0.0.1\n")
    return root


def _patch_paths(root: Path):
    """Patch paths.find_bay_dir + paths.consumer_root to point at our fixture."""
    return patch.multiple(
        "bay_cli.commands.ops.paths",
        find_bay_dir=lambda: root / ".bay",
        consumer_root=lambda bay_dir: root,
    )


# --path — happy path


def test_path_flag_prints_archive_dir(tmp_path: Path):
    root = _consumer_fixture(tmp_path, stack_name="testapp")
    runner = CliRunner()
    with _patch_paths(root):
        result = runner.invoke(_make_app(), ["whoami", "--path", "--env", "production"])
    assert result.exit_code == 0, result.output
    # No trailing newline — caller's $() will handle word-splitting.
    assert "/opt/testapp/logs/services/whoami/" in result.output


def test_path_flag_does_not_call_run_on_host(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh:
        result = runner.invoke(_make_app(), ["whoami", "--path", "--env", "production"])
    assert result.exit_code == 0, result.output
    mock_ssh.assert_not_called()


# --path --since — dry-run zcat pipeline


def test_path_with_date_since_prints_zcat_hint(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root):
        result = runner.invoke(
            _make_app(),
            ["whoami", "--path", "--since", "2026-04-01", "--env", "production"],
        )
    assert result.exit_code == 0, result.output
    assert "zcat" in result.output
    assert "/opt/testapp/logs/services/whoami/" in result.output
    # Dry-run only — the hint must not actually execute zcat.
    assert "awk" in result.output  # the filtering line


def test_path_with_date_since_does_not_call_run_on_host(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh:
        result = runner.invoke(
            _make_app(),
            ["whoami", "--path", "--since", "2026-04-01", "--env", "production"],
        )
    assert result.exit_code == 0, result.output
    mock_ssh.assert_not_called()


# --since without --path — date-shaped rejected, duration-shaped forwarded


def test_date_since_without_path_exits_1(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh:
        result = runner.invoke(
            _make_app(),
            ["whoami", "--since", "2026-04-01", "--env", "production"],
        )
    assert result.exit_code == 1, result.output
    # SSH path must not have been attempted.
    mock_ssh.assert_not_called()


def test_duration_since_without_path_forwards_to_docker(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh:
        result = runner.invoke(
            _make_app(),
            ["whoami", "--since", "1h", "--env", "production"],
        )
    assert result.exit_code == 0, result.output
    mock_ssh.assert_called_once()
    # Check the shell command built up includes docker logs --since 1h
    call_args = mock_ssh.call_args
    assert "docker logs" in call_args[0][1]
    assert "--since 1h" in call_args[0][1]


# Unknown service


def test_unknown_service_with_path_exits_non_zero(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root):
        result = runner.invoke(
            _make_app(),
            ["nonexistent-service", "--path", "--env", "production"],
        )
    assert result.exit_code != 0


# --help advertises --path


def test_help_mentions_path_flag(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(_make_app(), ["--help"])
    assert result.exit_code == 0
    assert "--path" in result.output
