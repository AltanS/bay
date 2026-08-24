"""Tests for M50 runtime commands: logs, restart.

Covers:
- _read_stack_name() — reads stack_name from group_vars
- _validate_service_name() — validates against services.yml
- _resolve_target_host() — resolves --region to ansible --limit
- _all_service_names() — lists all service and accessory names
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unittest.mock import MagicMock, patch

from bay_cli.commands.ops import (
    _all_service_names,
    _check_rig_state,
    _read_stack_name,
    _resolve_target_host,
    _validate_service_name,
)
from bay_cli.errors import BayError


# ── Filesystem helpers ──────────────────────────────────────────────────


def _write_services_yml(root: Path, content: str) -> None:
    """Write services.yml to group_vars/all/."""
    svc_path = root / "group_vars" / "all" / "services.yml"
    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(content)


def _write_main_yml(root: Path, stack_name: str = "myapp") -> None:
    """Write group_vars/all/main.yml with stack_name."""
    main_path = root / "group_vars" / "all" / "main.yml"
    main_path.parent.mkdir(parents=True, exist_ok=True)
    main_path.write_text(f"---\nstack_name: {stack_name}\n")


def _write_single_region_inventory(root: Path) -> None:
    """Write a single-server hosts/production INI file."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text("[production]\n10.0.0.1\n")


def _write_multi_region_inventory(root: Path) -> None:
    """Write a multi-region hosts/production INI file."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(
        "[production:children]\n"
        "eu\n"
        "na\n"
        "\n"
        "[eu]\n"
        "10.0.0.1\n"
        "\n"
        "[na]\n"
        "10.0.0.2\n"
    )


_SERVICES_YML = """\
---
services:
  gatus:
    image: twinproduction/gatus:latest
    port: 8080
    domains:
      - gatus.example.com
    access: public
  traefik:
    image: traefik:v3.6
    port: 80
    access: public

accessories:
  pgvector:
    image: pgvector/pgvector:pg16
    port: "127.0.0.1:5432:5432"
  beszel-agent:
    image: henrygd/beszel-agent:latest
"""


# ── _read_stack_name tests ───────────────────────────────────────────────


class TestGetStackName:
    """Tests for _read_stack_name()."""

    def test_reads_stack_name(self, tmp_path: Path) -> None:
        """Reads stack_name from main.yml."""
        _write_main_yml(tmp_path, "myapp")
        assert _read_stack_name(tmp_path) == "myapp"

    def test_raises_when_main_yml_missing(self, tmp_path: Path) -> None:
        """Raises BayError when main.yml is missing."""
        with pytest.raises(BayError, match="not found"):
            _read_stack_name(tmp_path)

    def test_raises_when_stack_name_key_missing(self, tmp_path: Path) -> None:
        """Raises BayError when main.yml exists but has no stack_name key."""
        main_path = tmp_path / "group_vars" / "all" / "main.yml"
        main_path.parent.mkdir(parents=True, exist_ok=True)
        main_path.write_text("---\ndomain_base: example.com\n")
        with pytest.raises(BayError, match="not defined"):
            _read_stack_name(tmp_path)


# ── _validate_service_name tests ────────────────────────────────────────


class TestValidateServiceName:
    """Tests for _validate_service_name()."""

    def test_valid_service(self, tmp_path: Path) -> None:
        """Accepts a service that exists in services.yml."""
        _write_services_yml(tmp_path, _SERVICES_YML)
        _validate_service_name("gatus", tmp_path)  # should not raise

    def test_valid_accessory(self, tmp_path: Path) -> None:
        """Accepts an accessory that exists in services.yml."""
        _write_services_yml(tmp_path, _SERVICES_YML)
        _validate_service_name("pgvector", tmp_path)  # should not raise

    def test_invalid_name_raises(self, tmp_path: Path) -> None:
        """Raises BayError for unknown service name."""
        _write_services_yml(tmp_path, _SERVICES_YML)
        with pytest.raises(BayError, match="not found in services.yml"):
            _validate_service_name("nonexistent", tmp_path)

    def test_error_lists_available(self, tmp_path: Path) -> None:
        """Error hint lists available service names."""
        _write_services_yml(tmp_path, _SERVICES_YML)
        with pytest.raises(BayError) as exc_info:
            _validate_service_name("nonexistent", tmp_path)
        assert exc_info.value.hint is not None
        assert "gatus" in exc_info.value.hint
        assert "pgvector" in exc_info.value.hint


# ── _resolve_target_host tests ──────────────────────────────────────────


class TestResolveTargetHost:
    """Tests for _resolve_target_host()."""

    def test_no_region_returns_none(self, tmp_path: Path) -> None:
        """Returns None when region is None."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        assert _resolve_target_host(bay_dir, None) is None

    def test_single_server_returns_none(self, tmp_path: Path) -> None:
        """Returns None for single-server inventory even with region."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_single_region_inventory(tmp_path)
        assert _resolve_target_host(bay_dir, "eu") is None

    def test_multi_region_resolves_eu(self, tmp_path: Path) -> None:
        """Resolves eu region to its host IP."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_multi_region_inventory(tmp_path)
        assert _resolve_target_host(bay_dir, "eu") == "10.0.0.1"

    def test_multi_region_resolves_na(self, tmp_path: Path) -> None:
        """Resolves na region to its host IP."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_multi_region_inventory(tmp_path)
        assert _resolve_target_host(bay_dir, "na") == "10.0.0.2"

    def test_unknown_region_returns_none(self, tmp_path: Path) -> None:
        """Returns None for an unknown region name."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        _write_multi_region_inventory(tmp_path)
        assert _resolve_target_host(bay_dir, "asia") is None

    def test_missing_inventory_returns_none(self, tmp_path: Path) -> None:
        """Returns None when hosts/production does not exist."""
        bay_dir = tmp_path / ".bay"
        bay_dir.mkdir()
        assert _resolve_target_host(bay_dir, "eu") is None


# ── _all_service_names tests ────────────────────────────────────────────


class TestAllServiceNames:
    """Tests for _all_service_names()."""

    def test_returns_all_names_sorted(self, tmp_path: Path) -> None:
        """Returns sorted list of all services and accessories."""
        _write_services_yml(tmp_path, _SERVICES_YML)
        names = _all_service_names(tmp_path)
        assert names == ["beszel-agent", "gatus", "pgvector", "traefik"]

    def test_empty_services(self, tmp_path: Path) -> None:
        """Returns empty list when services.yml has no entries."""
        _write_services_yml(tmp_path, "---\nservices: {}\naccessories: {}\n")
        names = _all_service_names(tmp_path)
        assert names == []


# ── _check_rig_state tests ────────────────────────────────────────────────


def _setup_rig_env(tmp_path: Path) -> tuple[Path, Path]:
    """Set up bay_dir and root with main.yml for rig state tests."""
    root = tmp_path
    bay_dir = tmp_path / ".bay"
    bay_dir.mkdir()
    _write_main_yml(root, "myapp")
    return bay_dir, root


class TestCheckRigState:
    """Tests for _check_rig_state()."""

    @patch("bay_cli.commands.ops._run_on_host")
    def test_unreachable_server_returns_true(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Returns True (rig needed) when server is unreachable."""
        bay_dir, root = _setup_rig_env(tmp_path)
        mock_run.side_effect = Exception("Connection refused")
        assert _check_rig_state("production", bay_dir, root) is True

    @patch("bay_cli.commands.ops._run_on_host")
    def test_missing_state_file_returns_true(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Returns True when state file doesn't exist (first deploy)."""
        bay_dir, root = _setup_rig_env(tmp_path)
        mock_run.return_value = MagicMock(
            stdout="host | SUCCESS | rc=0 >>\n__MISSING__\n"
        )
        assert _check_rig_state("production", bay_dir, root) is True

    @patch("bay_cli.commands.ops.subprocess.run")
    @patch("bay_cli.commands.ops.paths.read_installed_version", return_value="0.62.1")
    @patch("bay_cli.commands.ops._run_on_host")
    def test_matching_state_returns_false(
        self, mock_run_on_host: MagicMock, _mock_ver: MagicMock,
        mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Returns False (no rig needed) when bay version and consumer ref match."""
        bay_dir, root = _setup_rig_env(tmp_path)
        state = '{"bay_version": "0.62.1", "consumer_ref": "abc1234"}'
        mock_run_on_host.return_value = MagicMock(
            stdout=f"host | SUCCESS | rc=0 >>\n{state}\n"
        )
        mock_git.return_value = MagicMock(stdout="abc1234\n")
        assert _check_rig_state("production", bay_dir, root) is False

    @patch("bay_cli.commands.ops.paths.read_installed_version", return_value="0.63.0")
    @patch("bay_cli.commands.ops._run_on_host")
    def test_mismatched_bay_version_returns_true(
        self, mock_run_on_host: MagicMock, _mock_ver: MagicMock, tmp_path: Path
    ) -> None:
        """Returns True when framework version has changed."""
        bay_dir, root = _setup_rig_env(tmp_path)
        state = '{"bay_version": "0.62.1", "consumer_ref": "abc1234"}'
        mock_run_on_host.return_value = MagicMock(
            stdout=f"host | SUCCESS | rc=0 >>\n{state}\n"
        )
        assert _check_rig_state("production", bay_dir, root) is True

    @patch("bay_cli.commands.ops.subprocess.run")
    @patch("bay_cli.commands.ops.paths.read_installed_version", return_value="0.62.1")
    @patch("bay_cli.commands.ops._run_on_host")
    def test_mismatched_consumer_ref_returns_true(
        self, mock_run_on_host: MagicMock, _mock_ver: MagicMock,
        mock_git: MagicMock, tmp_path: Path
    ) -> None:
        """Returns True when consumer config has changed."""
        bay_dir, root = _setup_rig_env(tmp_path)
        state = '{"bay_version": "0.62.1", "consumer_ref": "abc1234"}'
        mock_run_on_host.return_value = MagicMock(
            stdout=f"host | SUCCESS | rc=0 >>\n{state}\n"
        )
        mock_git.return_value = MagicMock(stdout="def5678\n")
        assert _check_rig_state("production", bay_dir, root) is True

    @patch("bay_cli.commands.ops._run_on_host")
    def test_malformed_json_returns_true(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        """Returns True (safe fallback) when state file contains invalid JSON."""
        bay_dir, root = _setup_rig_env(tmp_path)
        mock_run.return_value = MagicMock(
            stdout="host | SUCCESS | rc=0 >>\n{broken json\n"
        )
        assert _check_rig_state("production", bay_dir, root) is True
