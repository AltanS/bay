"""The wizard must scaffold the alerts file the CLI reads, and nothing legacy.

`group_vars/all/alerts.yml` did not exist in a scaffolded tree, while
`group_vars/production/main.yml` carried `docker_monitor_telegram_*`. Those
keys desugar into implicit recipients (duplicate delivery once a real one is
added) and, being env-level, outrank the `group_vars/all/alerts.yml` that
`bin/bay alerts` writes. A fresh project shipped with the trap armed.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root
from bay_cli.wizard.scaffold import _TEMPLATES

ROOT = _package_framework_root()
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"

TEMPLATE_DIR = ROOT / "src" / "bay_cli" / "wizard" / "templates"
EXAMPLE_DIR = ROOT / "example"


def _scaffolded(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".bay").symlink_to(ROOT)
    monkeypatch.chdir(project)
    from bay_cli.cli import app

    result = CliRunner().invoke(
        app,
        ["setup", "--defaults", "--server-ip", "203.0.113.10",
         "--domain", "example.test", "--ssh-key", KEY],
    )
    assert result.exit_code == 0, result.output
    return project


# ── The scaffold ─────────────────────────────────────────────────────────


def test_alerts_yml_is_registered_in_the_template_map() -> None:
    assert _TEMPLATES["alerts.yml.j2"] == "group_vars/all/alerts.yml"


def test_scaffold_writes_an_empty_recipient_list(tmp_path: Path, monkeypatch) -> None:
    project = _scaffolded(tmp_path, monkeypatch)
    alerts = project / "group_vars" / "all" / "alerts.yml"
    assert alerts.is_file()

    data = yaml.safe_load(alerts.read_text())
    assert data["alert_recipients"] == []


def test_the_file_points_at_the_cli(tmp_path: Path, monkeypatch) -> None:
    project = _scaffolded(tmp_path, monkeypatch)
    text = (project / "group_vars" / "all" / "alerts.yml").read_text()
    assert "bin/bay alerts" in text


# ── The legacy keys are gone ─────────────────────────────────────────────


def _sources() -> list[Path]:
    return [
        p
        for base in (TEMPLATE_DIR, EXAMPLE_DIR)
        for p in base.rglob("*")
        if p.is_file()
    ]


def test_no_legacy_telegram_keys_anywhere_in_the_scaffold_sources() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in _sources()
        if "docker_monitor_telegram" in p.read_text(errors="replace")
    ]
    assert not offenders, f"legacy alert keys still scaffolded in: {offenders}"


def test_scaffolded_project_has_no_legacy_keys(tmp_path: Path, monkeypatch) -> None:
    project = _scaffolded(tmp_path, monkeypatch)
    # group_vars only -- `project` also holds the .bay symlink to the framework.
    for path in (project / "group_vars").rglob("*.yml"):
        assert "docker_monitor_telegram" not in path.read_text(errors="replace"), path
