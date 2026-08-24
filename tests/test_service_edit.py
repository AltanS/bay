"""Unit tests for the ``service edit`` CLI command.

Tests cover access changes, image edits, not-found errors, domain collision
detection, region edits, idempotency, no-flags validation, and key preservation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bay_cli.cli import app
from bay_cli.config import StackConfig
from bay_cli.console import output as console_output

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


def _write_single_region_inventory(root: Path) -> None:
    """Write a single-region hosts/production INI file."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text("[production]\n10.0.0.1\n")


def _setup_consumer(
    root: Path,
    services_yml: str = "---\nservices: {}\naccessories: {}\n",
    domain_base: str = "example.com",
) -> None:
    """Set up a minimal consumer directory for testing."""
    _write_services_yml(root, services_yml)
    _write_main_yml(root, domain_base)
    _write_single_region_inventory(root)


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
        return {}

    monkeypatch.setattr(service_mod, "_get_config", mock_get_config)
    monkeypatch.setattr(service_mod, "_get_catalog", mock_get_catalog)


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


# ── Shared services.yml content ──────────────────────────────────────────

GATUS_YML = """\
---
services:
  gatus:
    image: twinproduction/gatus:latest
    domains:
      - status.example.com
    access: public
    port: 8080
accessories: {}
"""

TWO_SERVICES_YML = """\
---
services:
  service-a:
    image: org/service-a:latest
    domains:
      - a.example.com
    access: public
  service-b:
    image: org/service-b:latest
    domains:
      - b.example.com
    access: vpn
accessories: {}
"""

GATUS_RICH_YML = """\
---
services:
  gatus:
    image: twinproduction/gatus:latest
    domains:
      - status.example.com
    access: public
    port: 8080
    middleware:
      rate_limit:
        average: 100
        burst: 50
    volumes:
      - /data/gatus:/data
accessories: {}
"""


# ── 1. Edit access with dry-run ──────────────────────────────────────────


class TestEditAccessDryRun:
    """service edit gatus --access vpn --dry-run shows diff without saving."""

    def test_dry_run_shows_diff_and_preserves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        svc_path = tmp_path / "group_vars" / "all" / "services.yml"
        original_bytes = svc_path.read_bytes()

        result = runner.invoke(
            app, ["--yes", "service", "edit", "gatus", "--access", "vpn", "--dry-run"]
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        # The diff output should mention the access change
        assert "vpn" in result.stdout
        # File must be byte-identical to original
        assert svc_path.read_bytes() == original_bytes


# ── 2. Edit image ────────────────────────────────────────────────────────


class TestEditImage:
    """service edit gatus --image neworg/newimage:v2 updates services.yml."""

    def test_image_edit_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            ["service", "edit", "gatus", "--image", "neworg/newimage:v2"],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        cfg = StackConfig(tmp_path)
        svc = cfg.get_service("gatus")
        assert svc is not None
        assert svc["image"] == "neworg/newimage:v2"


# ── 3. Not found ─────────────────────────────────────────────────────────


class TestEditNotFound:
    """service edit nonexistent --access vpn raises NOT_FOUND."""

    def test_not_found_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app, ["service", "edit", "nonexistent", "--access", "vpn"]
        )

        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "NOT_FOUND"


# ── 4. Domain collision ──────────────────────────────────────────────────


class TestEditDomainCollision:
    """service edit service-b --domain a.example.com raises CONFLICT."""

    def test_domain_collision_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=TWO_SERVICES_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            ["service", "edit", "service-b", "--domain", "a.example.com"],
        )

        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "CONFLICT"


# ── 5. Region edit ───────────────────────────────────────────────────────


class TestEditRegion:
    """service edit gatus --region eu sets regions: [eu] on gatus."""

    def test_region_edit_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            ["service", "edit", "gatus", "--region", "eu"],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        cfg = StackConfig(tmp_path)
        svc = cfg.get_service("gatus")
        assert svc is not None
        assert list(svc["regions"]) == ["eu"]


# ── 6. Idempotent edit (no changes needed) ───────────────────────────────


class TestEditIdempotent:
    """service edit gatus --image <same> reports 'No changes'."""

    def test_no_changes_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        svc_path = tmp_path / "group_vars" / "all" / "services.yml"
        original_bytes = svc_path.read_bytes()

        result = runner.invoke(
            app,
            ["service", "edit", "gatus", "--image", "twinproduction/gatus:latest"],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "No changes" in result.stdout
        # File must be byte-identical to original
        assert svc_path.read_bytes() == original_bytes


# ── 7. No flags error ────────────────────────────────────────────────────


class TestEditNoFlags:
    """service edit gatus (no edit flags) raises CONFIG_ERROR."""

    def test_no_flags_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["service", "edit", "gatus"])

        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "CONFIG_ERROR"
        assert "flag" in str(result.exception).lower()


# ── 8. Preserves other keys ──────────────────────────────────────────────


class TestEditPreservesOtherKeys:
    """service edit gatus --image newimage:v2 preserves middleware and volumes."""

    def test_other_keys_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path, services_yml=GATUS_RICH_YML)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            ["service", "edit", "gatus", "--image", "newimage:v2"],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        cfg = StackConfig(tmp_path)
        svc = cfg.get_service("gatus")
        assert svc is not None
        # Image was updated
        assert svc["image"] == "newimage:v2"
        # Other keys are preserved
        assert "middleware" in svc
        assert svc["middleware"]["rate_limit"]["average"] == 100
        assert svc["middleware"]["rate_limit"]["burst"] == 50
        assert "volumes" in svc
        assert "/data/gatus:/data" in svc["volumes"]
        # Domain and access also preserved
        assert svc["access"] == "public"
        assert "status.example.com" in svc["domains"]
