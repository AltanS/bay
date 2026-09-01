"""`bay setup` must copy catalog config files, not just `bay service add`.

The copy helper lives in commands/service.py and has always worked; the
scaffold path simply never called it, so the wizard's default project
(Gatus) arrived without the config file its own services.yml declared.
Both paths now share the one helper — these tests pin that, and that the
copy is skipped rather than clobbered when a file is already there.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bay_cli.wizard.models import WizardResult
from bay_cli.wizard.scaffold import scaffold


def _result(services: list[str]) -> WizardResult:
    return WizardResult(
        project_name="testapp",
        multi_region=False,
        server_ip="203.0.113.10",
        domain_base="example.com",
        letsencrypt_email="ops@example.com",
        ssh_keys=[],
        access_gateway="none",
        selected_services=services,
    )


def test_gatus_config_lands_in_consumer_files(tmp_path: Path) -> None:
    created = scaffold(_result(["gatus"]), tmp_path)
    config = tmp_path / "files" / "gatus" / "config.yaml"
    assert config.is_file()
    assert config in created
    assert yaml.safe_load(config.read_text())["endpoints"]


def test_every_declared_config_file_is_present(tmp_path: Path) -> None:
    """The scaffolded services.yml must not reference a file that is absent."""
    scaffold(_result(["gatus", "postgres", "redis"]), tmp_path)
    services = yaml.safe_load((tmp_path / "group_vars/all/services.yml").read_text())
    declared = [
        cf
        for section in ("services", "accessories")
        for entry in (services.get(section) or {}).values()
        for cf in (entry.get("config_files") or [])
    ]
    assert declared == ["gatus/config.yaml"]
    for cf in declared:
        assert (tmp_path / "files" / cf).is_file()


def test_existing_config_file_is_not_overwritten(tmp_path: Path) -> None:
    config = tmp_path / "files" / "gatus" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("# mine\n")
    created = scaffold(_result(["gatus"]), tmp_path)
    assert config.read_text() == "# mine\n"
    assert config not in created


def test_service_without_config_files_copies_nothing(tmp_path: Path) -> None:
    scaffold(_result(["redis"]), tmp_path)
    assert not (tmp_path / "files").exists()
