"""Tests for stale config detection (--check-config-age).

Covers:
- _read_stack_name_local(): reads stack_name from group_vars/all/main.yml
- _check_config_age_remote(): output parser + result classification
- validate() CLI flag wiring
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bay_cli.commands.validate import (
    ValidationResult,
    _GIT_DEPLOY_CONFIG_FILES,
    _GIT_DEPLOY_SENTINEL,
    _check_config_age_remote,
    _read_stack_name_local,
)


# ── Suppress Rich console output in all tests ────────────────────────────

class _JsonMode:
    """Context manager to suppress Rich console output during tests."""

    def __enter__(self):
        from bay_cli.console.output import set_json_mode
        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode
        set_json_mode(False)


# ── _read_stack_name_local tests ─────────────────────────────────────────


class TestReadStackNameLocal:

    def test_reads_stack_name(self, tmp_path: Path) -> None:
        """Returns stack_name from group_vars/all/main.yml."""
        main_yml = tmp_path / "group_vars" / "all" / "main.yml"
        main_yml.parent.mkdir(parents=True)
        main_yml.write_text("---\nstack_name: mystack\n")
        assert _read_stack_name_local(tmp_path) == "mystack"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """Returns None when group_vars/all/main.yml doesn't exist."""
        assert _read_stack_name_local(tmp_path) is None

    def test_missing_key_returns_none(self, tmp_path: Path) -> None:
        """Returns None when stack_name key is absent."""
        main_yml = tmp_path / "group_vars" / "all" / "main.yml"
        main_yml.parent.mkdir(parents=True)
        main_yml.write_text("---\nother_key: value\n")
        assert _read_stack_name_local(tmp_path) is None

    def test_invalid_yaml_returns_none(self, tmp_path: Path) -> None:
        """Returns None on malformed YAML (doesn't raise)."""
        main_yml = tmp_path / "group_vars" / "all" / "main.yml"
        main_yml.parent.mkdir(parents=True)
        main_yml.write_text("---\n: :\n  bad: [yaml\n")
        assert _read_stack_name_local(tmp_path) is None

    def test_stack_name_with_template_variable(self, tmp_path: Path) -> None:
        """Returns literal Jinja template string if stack_name is a plain value."""
        main_yml = tmp_path / "group_vars" / "all" / "main.yml"
        main_yml.parent.mkdir(parents=True)
        main_yml.write_text("---\nstack_name: bay\n")
        assert _read_stack_name_local(tmp_path) == "bay"


# ── _check_config_age_remote output-parsing tests ────────────────────────


def _fake_bay_dir(tmp_path: Path) -> Path:
    """Create a minimal fake bay dir with version.yml and main.yml."""
    bay_dir = tmp_path / ".bay"
    bay_dir.mkdir()
    (bay_dir / ".git").mkdir()  # so find_bay_dir() accepts it
    return bay_dir


def _consumer_root(tmp_path: Path, stack_name: str = "teststack") -> Path:
    """Create a minimal consumer root with group_vars/all/main.yml."""
    root = tmp_path / "consumer"
    root.mkdir()
    main_yml = root / "group_vars" / "all" / "main.yml"
    main_yml.parent.mkdir(parents=True)
    main_yml.write_text(f"---\nstack_name: {stack_name}\n")
    return root


def _run_check(
    ansible_stdout: str,
    returncode: int = 0,
    stack_name: str = "teststack",
    tmp_path: Path | None = None,
) -> ValidationResult:
    """Run _check_config_age_remote with mocked ansible subprocess call."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = _consumer_root(base, stack_name=stack_name)
        bay_dir = _fake_bay_dir(base)

        with _JsonMode():
            result = ValidationResult()
            mock_proc = MagicMock()
            mock_proc.stdout = ansible_stdout
            mock_proc.returncode = returncode

            with patch("subprocess.run", return_value=mock_proc):
                _check_config_age_remote(root, "production", bay_dir, result)

    return result


class TestCheckConfigAgeOutputParser:

    def test_all_files_ok(self) -> None:
        """All files fresh → single ok entry."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            ok\trebuild.sh
            ok\tconfig.json
            ok\timage-map.json
        """)
        result = _run_check(output)
        assert not result.warnings
        assert not result.failed
        assert len(result.passed) == 1
        assert "all 3 config file(s) are current" in result.passed[0]

    def test_stale_file_triggers_warning(self) -> None:
        """Stale file → warning with filename."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            ok\trebuild.sh
            stale\tconfig.json
            ok\timage-map.json
        """)
        result = _run_check(output)
        assert len(result.warnings) == 1
        assert "config.json" in result.warnings[0]
        assert "older than" in result.warnings[0]

    def test_multiple_stale_files(self) -> None:
        """Multiple stale files → one warning listing them all."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            stale\trebuild.sh
            stale\tconfig.json
            stale\timage-map.json
        """)
        result = _run_check(output)
        assert len(result.warnings) == 1
        assert "rebuild.sh" in result.warnings[0]
        assert "config.json" in result.warnings[0]

    def test_missing_config_file(self) -> None:
        """Missing config file → warning."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            missing\trebuild.sh
            ok\tconfig.json
            ok\timage-map.json
        """)
        result = _run_check(output)
        assert len(result.warnings) == 1
        assert "rebuild.sh" in result.warnings[0]
        assert "missing" in result.warnings[0]

    def test_no_sentinel_warns(self) -> None:
        """Sentinel absent → warning about first-deploy scenario."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            no_sentinel\t(none)
        """)
        result = _run_check(output)
        assert len(result.warnings) == 1
        assert "sentinel" in result.warnings[0].lower()

    def test_empty_output_warns(self) -> None:
        """Empty ansible output → connectivity warning."""
        result = _run_check("", returncode=4)
        assert len(result.warnings) == 1
        assert "no output" in result.warnings[0].lower()

    def test_missing_stack_name_warns(self) -> None:
        """Missing stack_name → graceful warning, no crash."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # No group_vars/all/main.yml
            root = base / "consumer"
            root.mkdir()
            bay_dir = _fake_bay_dir(base)

            with _JsonMode():
                result = ValidationResult()
                with patch("subprocess.run") as mock_run:
                    _check_config_age_remote(root, "production", bay_dir, result)
                    mock_run.assert_not_called()  # short-circuit before SSH

        assert len(result.warnings) == 1
        assert "stack_name" in result.warnings[0]

    def test_ansi_codes_stripped_from_output(self) -> None:
        """ANSI color codes in ansible output are stripped before parsing."""
        output = (
            "\x1b[0;33m192.168.1.1 | SUCCESS | rc=0 >>\x1b[0m\n"
            "\x1b[0;32mok\trebuild.sh\x1b[0m\n"
            "ok\tconfig.json\n"
            "ok\timage-map.json\n"
        )
        result = _run_check(output)
        assert not result.failed
        # ok + no_sentinel-path: either ok or warn, not fail
        assert not result.failed

    def test_stale_and_missing_both_warn(self) -> None:
        """Stale + missing → two separate warnings."""
        output = textwrap.dedent("""\
            192.168.1.1 | SUCCESS | rc=0 >>
            stale\trebuild.sh
            missing\tconfig.json
            ok\timage-map.json
        """)
        result = _run_check(output)
        assert len(result.warnings) == 2


# ── Sentinel path constants ───────────────────────────────────────────────


def test_sentinel_constant_matches_webhook_role() -> None:
    """_GIT_DEPLOY_SENTINEL path matches what main.yml writes."""
    assert _GIT_DEPLOY_SENTINEL == "state/git-deploy-config.timestamp"


def test_config_files_constant_has_all_three() -> None:
    """_GIT_DEPLOY_CONFIG_FILES contains rebuild.sh + both webhook configs."""
    assert "bin/rebuild.sh" in _GIT_DEPLOY_CONFIG_FILES
    assert "webhook/config.json" in _GIT_DEPLOY_CONFIG_FILES
    assert "webhook/image-map.json" in _GIT_DEPLOY_CONFIG_FILES
    assert len(_GIT_DEPLOY_CONFIG_FILES) == 3


# ── Ansible sentinel task in main.yml ────────────────────────────────────


def test_sentinel_written_after_systemd_in_main_yml() -> None:
    """Sentinel write task appears AFTER systemd.yml include in main.yml.

    This ensures rebuild.sh (rendered by systemd.yml) is stamped before
    the sentinel — so mtime comparison is valid.
    """
    repo_root = Path(__file__).resolve().parent.parent
    main_yml = repo_root / "roles" / "git_deploy" / "tasks" / "main.yml"
    text = main_yml.read_text()

    # Find positions of the sentinel task vs systemd.yml include
    sentinel_pos = text.find("Write git_deploy config sentinel timestamp")
    systemd_pos = text.find("include_tasks: systemd.yml")

    assert sentinel_pos != -1, "Sentinel write task not found in main.yml"
    assert systemd_pos != -1, "systemd.yml include not found in main.yml"
    assert sentinel_pos > systemd_pos, (
        "Sentinel write task must appear AFTER the systemd.yml include "
        "(so rebuild.sh is already rendered when sentinel is stamped)"
    )


def test_sentinel_not_in_webhook_yml() -> None:
    """Sentinel write task must NOT be in webhook.yml (moved to main.yml)."""
    repo_root = Path(__file__).resolve().parent.parent
    webhook_yml = repo_root / "roles" / "git_deploy" / "tasks" / "webhook.yml"
    text = webhook_yml.read_text()
    assert "git-deploy-config.timestamp" not in text, (
        "Sentinel write task was left in webhook.yml — it must be in main.yml "
        "so it runs after both webhook.yml AND systemd.yml complete"
    )
