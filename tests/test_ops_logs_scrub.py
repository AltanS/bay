"""Tests for `bin/bay logs --scrub` (M84-S6 GDPR erasure).

Covers:
- `--scrub` requires `--pattern`; exits 1 without it.
- Dry-run mode (no `--yes`): runs _scrub_count_matches, prints report, exits 0.
- Execute mode (`--yes`): invokes _scrub_execute exactly once.
- Zero-match dry-run + `--yes` short-circuits (no _scrub_execute call).
- Unknown service exits non-zero.
- `--help` advertises `--scrub`, `--pattern`, `--yes`.
- `_operator_identity()` falls back to 'unknown' when git isn't available.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from bay_cli.commands.ops import (
    _operator_identity,
    logs,
)


def _make_app() -> typer.Typer:
    app = typer.Typer()
    app.command()(logs)
    return app


def _consumer_fixture(tmp_path: Path, stack_name: str = "testapp") -> Path:
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
    return patch.multiple(
        "bay_cli.commands.ops.paths",
        find_bay_dir=lambda: root / ".bay",
        consumer_root=lambda bay_dir: root,
    )


# ── --pattern is required ────────────────────────────────────────────


def test_scrub_requires_pattern(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh:
        result = runner.invoke(
            _make_app(), ["whoami", "--scrub", "--env", "production"]
        )
    assert result.exit_code == 1, result.output
    # No SSH attempted — we bailed before the dry-run grep.
    mock_ssh.assert_not_called()


# ── dry-run path ────────────────────────────────────────────────────


def test_scrub_dry_run_prints_counts(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._scrub_count_matches",
        return_value={"live.log": 3, "2026-04-21.log.gz": 1, "2026-04-20.log.gz": 0},
    ), patch("bay_cli.commands.ops._scrub_execute") as mock_exec:
        result = runner.invoke(
            _make_app(),
            ["whoami", "--scrub", "--pattern", "user@example.com", "--env", "production"],
        )
    assert result.exit_code == 0, result.output
    # Preview must list the two affected files and their counts.
    assert "live.log" in result.output
    assert "2026-04-21.log.gz" in result.output
    # Unaffected file is rolled up into a summary ("1 file(s) scanned, no matches").
    assert "2026-04-20.log.gz" not in result.output or "0 matching" not in result.output
    # Execute MUST NOT have been called without --yes.
    mock_exec.assert_not_called()


def test_scrub_dry_run_zero_matches_prints_none_found(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._scrub_count_matches",
        return_value={"live.log": 0, "2026-04-21.log.gz": 0},
    ), patch("bay_cli.commands.ops._scrub_execute") as mock_exec:
        result = runner.invoke(
            _make_app(),
            ["whoami", "--scrub", "--pattern", "nomatch", "--env", "production"],
        )
    assert result.exit_code == 0, result.output
    mock_exec.assert_not_called()


# ── --yes path ──────────────────────────────────────────────────────


def test_scrub_with_yes_calls_execute(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._scrub_count_matches",
        return_value={"live.log": 3, "2026-04-21.log.gz": 2},
    ), patch("bay_cli.commands.ops._scrub_execute") as mock_exec, patch(
        "bay_cli.commands.ops._run_on_host"
    ) as mock_ssh, patch(
        "bay_cli.commands.ops._operator_identity", return_value="ops@example.com"
    ):
        result = runner.invoke(
            _make_app(),
            [
                "whoami",
                "--scrub",
                "--pattern",
                "user@example.com",
                "--yes",
                "--env",
                "production",
            ],
        )
    assert result.exit_code == 0, result.output
    mock_exec.assert_called_once()
    # Positional args to _scrub_execute: env, bay_dir, stack, svc, pattern, op, limit
    args = mock_exec.call_args[0]
    assert args[2] == "testapp"  # stack
    assert args[3] == "whoami"  # service
    assert args[4] == "user@example.com"  # pattern
    assert args[5] == "ops@example.com"  # operator


def test_scrub_with_yes_zero_matches_skips_execute(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root), patch(
        "bay_cli.commands.ops._scrub_count_matches",
        return_value={"live.log": 0, "2026-04-21.log.gz": 0},
    ), patch("bay_cli.commands.ops._scrub_execute") as mock_exec:
        result = runner.invoke(
            _make_app(),
            [
                "whoami",
                "--scrub",
                "--pattern",
                "nomatch",
                "--yes",
                "--env",
                "production",
            ],
        )
    assert result.exit_code == 0, result.output
    mock_exec.assert_not_called()


# ── unknown service ─────────────────────────────────────────────────


def test_scrub_unknown_service_exits_nonzero(tmp_path: Path):
    root = _consumer_fixture(tmp_path)
    runner = CliRunner()
    with _patch_paths(root):
        result = runner.invoke(
            _make_app(),
            ["nonexistent", "--scrub", "--pattern", "x", "--env", "production"],
        )
    assert result.exit_code != 0


# ── --help advertises scrub flags ───────────────────────────────────


def test_help_mentions_scrub_flags(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(_make_app(), ["--help"])
    assert result.exit_code == 0
    assert "--scrub" in result.output
    assert "--pattern" in result.output
    assert "--yes" in result.output


# ── _operator_identity ──────────────────────────────────────────────


def test_operator_identity_returns_git_email():
    with patch("bay_cli.commands.ops.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "dev@example.com\n"
        assert _operator_identity() == "dev@example.com"


def test_operator_identity_fallback_on_git_missing():
    import subprocess

    with patch(
        "bay_cli.commands.ops.subprocess.run",
        side_effect=FileNotFoundError(),
    ):
        assert _operator_identity() == "unknown"


def test_operator_identity_fallback_on_unset():
    import subprocess

    with patch(
        "bay_cli.commands.ops.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert _operator_identity() == "unknown"


def test_operator_identity_fallback_on_empty():
    with patch("bay_cli.commands.ops.subprocess.run") as mock_run:
        mock_run.return_value.stdout = "\n"
        assert _operator_identity() == "unknown"
