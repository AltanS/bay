"""Unit tests for the ``service add`` CLI command.

Tests cover catalog-based adds, custom services, dry-run mode, JSON output,
idempotency, dependency resolution, domain/port collision detection, and
secret key reporting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bay_cli.catalog import CatalogEntry
from bay_cli.cli import app
from bay_cli.config import StackConfig
from bay_cli.console import output as console_output

runner = CliRunner()

# ── Catalog fixtures ──────────────────────────────────────────────────────

_FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent


def _load_real_catalog() -> dict[str, CatalogEntry]:
    """Load the real framework catalog from the framework repo's catalog/."""
    from bay_cli.catalog import load_catalog

    return load_catalog(_FRAMEWORK_ROOT, _FRAMEWORK_ROOT)


@pytest.fixture()
def real_catalog() -> dict[str, CatalogEntry]:
    return _load_real_catalog()


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


def _write_multi_region_inventory(root: Path, regions: list[str]) -> None:
    """Write a multi-region hosts/production INI file."""
    inv_path = root / "hosts" / "production"
    inv_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[production:children]"]
    for r in regions:
        lines.append(r)
    lines.append("")
    for r in regions:
        lines.append(f"[{r}]")
        lines.append("10.0.0.1")
        lines.append("")
    inv_path.write_text("\n".join(lines))


def _setup_consumer(
    root: Path,
    services_yml: str = "---\nservices: {}\naccessories: {}\n",
    domain_base: str = "example.com",
    multi_region: bool = False,
    regions: list[str] | None = None,
) -> None:
    """Set up a minimal consumer directory for testing."""
    _write_services_yml(root, services_yml)
    _write_main_yml(root, domain_base)
    if multi_region and regions:
        _write_multi_region_inventory(root, regions)
    else:
        _write_single_region_inventory(root)


# ── Monkeypatch helpers ──────────────────────────────────────────────────


def _patch_service_module(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    catalog: dict[str, CatalogEntry] | None = None,
) -> None:
    """Monkeypatch _get_config() and _get_catalog() in service.py."""
    from bay_cli.commands import service as service_mod

    def mock_get_config():
        return StackConfig(root)

    def mock_get_catalog():
        if catalog is not None:
            return catalog
        return _load_real_catalog()

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


# ── 1. Dry-run with catalog entry ────────────────────────────────────────


class TestDryRunCatalogEntry:
    """service add gatus --dry-run shows diff without modifying files."""

    def test_dry_run_shows_diff_and_preserves_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = "---\nservices: {}\naccessories: {}\n"
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        svc_path = tmp_path / "group_vars" / "all" / "services.yml"
        original_bytes = svc_path.read_bytes()

        result = runner.invoke(app, ["service", "add", "gatus", "--dry-run"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        # The diff output should mention gatus
        assert "gatus" in result.stdout
        # File must be byte-identical to original
        assert svc_path.read_bytes() == original_bytes


# ── 2. JSON dry-run ──────────────────────────────────────────────────────


class TestJsonDryRun:
    """--json service add gatus --dry-run returns structured JSON."""

    def test_json_dry_run_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app, ["--json", "service", "add", "gatus", "--dry-run"]
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        data = json.loads(result.stdout)
        assert data["ok"] is True
        assert data["data"]["dry_run"] is True
        assert "gatus" in data["data"]["added"]


# ── 3. Idempotency — already exists ─────────────────────────────────────


class TestIdempotencyAlreadyExists:
    """Adding a service that already exists is a no-op."""

    def test_already_exists_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        services_yml = (
            "---\nservices:\n  gatus:\n    image: twinproduction/gatus:latest\n"
            "    domains:\n      - status.example.com\n    access: public\n"
            "accessories: {}\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["service", "add", "gatus"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "already exists" in result.stdout


# ── 4. Custom service add ────────────────────────────────────────────────


class TestCustomServiceAdd:
    """Adding a custom service with --image and --port."""

    def test_custom_service_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            [
                "service", "add", "--name", "custom-app",
                "--image", "myorg/myapp:latest",
                "--port", "8080",
            ],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        # Verify the service was written to services.yml
        cfg = StackConfig(tmp_path)
        svc = cfg.get_service("custom-app")
        assert svc is not None
        assert svc["image"] == "myorg/myapp:latest"
        assert svc["port"] == 8080


# ── 5. No-deps flag ─────────────────────────────────────────────────────


class TestNoDepsFlag:
    """--no-deps skips automatic dependency resolution."""

    def test_no_deps_skips_postgres(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["service", "add", "n8n", "--no-deps"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        cfg = StackConfig(tmp_path)
        assert cfg.get_service("n8n") is not None
        assert cfg.get_service("postgres") is None


# ── 6. Auto dependency resolution ───────────────────────────────────────


class TestAutoDependencyResolution:
    """Adding n8n without --no-deps also adds postgres."""

    def test_n8n_adds_postgres(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["service", "add", "n8n"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

        cfg = StackConfig(tmp_path)
        assert cfg.get_service("n8n") is not None
        assert cfg.get_service("postgres") is not None


# ── 7. Domain collision ─────────────────────────────────────────────────


class TestDomainCollision:
    """Adding a service whose domain collides with an existing one raises CONFLICT."""

    def test_domain_collision_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-populate with a service that owns status.example.com
        services_yml = (
            "---\nservices:\n  existing-svc:\n    image: foo:latest\n"
            "    domains:\n      - status.example.com\n    access: public\n"
            "accessories: {}\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        # gatus catalog entry defaults to domain_prefix "status",
        # which constructs domain "status.example.com" — collision!
        result = runner.invoke(app, ["service", "add", "gatus"])

        # BayError.conflict has exit_code 2; CliRunner captures the exception
        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "CONFLICT"


# ── 8. Port collision ───────────────────────────────────────────────────


class TestPortCollision:
    """Adding an accessory whose port collides with an existing one raises CONFLICT."""

    def test_port_collision_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-populate with accessory using 5432
        services_yml = (
            "---\nservices: {}\naccessories:\n  existing-db:\n"
            "    image: postgres:16\n    port: '127.0.0.1:5432:5432'\n"
        )
        _setup_consumer(tmp_path, services_yml=services_yml)
        _patch_service_module(monkeypatch, tmp_path)

        # postgres catalog entry also uses port 127.0.0.1:5432:5432
        result = runner.invoke(app, ["service", "add", "postgres"])

        assert result.exception is not None
        from bay_cli.errors import BayError

        assert isinstance(result.exception, BayError)
        assert result.exception.code.value == "CONFLICT"


# ── 9. Non-overlapping regions — no collision ───────────────────────────


class TestNonOverlappingRegions:
    """Same domain in different regions should not collide."""

    def test_different_regions_no_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Existing service in region "eu" with domain status.eu.example.com
        services_yml = (
            "---\nservices:\n  gatus-eu:\n    image: twinproduction/gatus:latest\n"
            "    domains:\n      - status.eu.example.com\n    access: public\n"
            "    regions:\n      - eu\n"
            "accessories: {}\n"
        )
        _setup_consumer(
            tmp_path,
            services_yml=services_yml,
            multi_region=True,
            regions=["eu", "na"],
        )
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(
            app,
            [
                "service", "add", "gatus",
                "--name", "gatus-na",
                "--domain", "status.eu.example.com",
                "--region", "na",
            ],
        )

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert result.exception is None

        cfg = StackConfig(tmp_path)
        assert cfg.get_service("gatus-na") is not None


# ── 10. Secret key printing ─────────────────────────────────────────────


class TestSecretKeyPrinting:
    """Adding n8n should report the secret key DB_POSTGRESDB_PASSWORD."""

    def test_secret_key_in_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_consumer(tmp_path)
        _patch_service_module(monkeypatch, tmp_path)

        result = runner.invoke(app, ["service", "add", "n8n"])

        assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "DB_POSTGRESDB_PASSWORD" in result.stdout
