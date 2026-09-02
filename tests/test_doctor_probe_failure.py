"""A doctor probe that crashes must not be reported as a green run.

The webhook block swallowed every exception into a warning and left the issue
count untouched, so an import error, a YAML error or a network failure made
`doctor` print "All checks passed!" and exit 0 -- the exact opposite of the
truth.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root

ROOT = _package_framework_root()


def _project(tmp_path: Path) -> Path:
    """A minimal consumer tree that gets doctor as far as the webhook probe."""
    project = tmp_path / "proj"
    (project / "hosts").mkdir(parents=True)
    (project / "group_vars" / "all").mkdir(parents=True)
    (project / ".bay").symlink_to(ROOT)
    (project / ".vault_pass").write_text("hunter2\n")
    (project / "hosts" / "production").write_text("[production]\n203.0.113.10\n")
    (project / "group_vars" / "all" / "main.yml").write_text("---\nadmin_user: bay-admin\n")
    (project / "group_vars" / "all" / "services.yml").write_text(
        "---\nservices:\n  gatus:\n    domains:\n      - status.example.com\n"
    )
    return project


def _invoke(project: Path, monkeypatch):
    monkeypatch.chdir(project)
    from bay_cli.cli import app

    return CliRunner().invoke(app, ["doctor"])


def _no_network(monkeypatch) -> None:
    monkeypatch.setattr("bay_cli.commands.doctor._probe_ssh", lambda h, u: (True, "root", ""))
    monkeypatch.setattr("bay_cli.commands.doctor._resolve_domain", lambda d: "203.0.113.10")
    monkeypatch.setattr(
        "bay_cli.commands.validate._probe_webhook_health",
        lambda root, env, services, parsed, result: None,
    )


def test_a_crashing_webhook_probe_is_an_error(tmp_path: Path, monkeypatch) -> None:
    _no_network(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("bay_cli.commands.validate._probe_webhook_health", boom)
    result = _invoke(_project(tmp_path), monkeypatch)

    assert result.exit_code == 1, result.output
    assert "All checks passed" not in result.output
    assert "kaboom" in result.output


def test_a_crashing_dns_probe_is_an_error(tmp_path: Path, monkeypatch) -> None:
    _no_network(monkeypatch)

    def boom(_domain):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr("bay_cli.commands.doctor._resolve_domain", boom)
    result = _invoke(_project(tmp_path), monkeypatch)

    assert result.exit_code == 1, result.output
    assert "All checks passed" not in result.output
    assert "resolver exploded" in result.output


def test_the_summary_is_still_green_when_nothing_crashes(tmp_path: Path, monkeypatch) -> None:
    _no_network(monkeypatch)
    result = _invoke(_project(tmp_path), monkeypatch)

    assert result.exit_code == 0, result.output
    assert "All checks passed" in result.output
