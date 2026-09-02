"""`bay setup --defaults` must refuse to scaffold a project that cannot deploy.

The built-in defaults were `0.0.0.0` and `example.com`: an inventory pointing
at nothing and a status page on a domain the operator does not own. Nothing
rejected them, so `--defaults` produced a tree whose first deploy could only
fail. The guard runs before anything is written to disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root

ROOT = _package_framework_root()
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"


def _project(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "myproj"
    project.mkdir()
    (project / ".bay").symlink_to(ROOT)
    monkeypatch.chdir(project)
    return project


def _run(args: list[str]):
    from bay_cli.cli import app

    return CliRunner().invoke(app, ["setup", "--defaults", *args])


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--server-ip", "203.0.113.10"],
        ["--domain", "example.test"],
    ],
)
def test_both_flags_are_named_when_either_is_missing(
    tmp_path: Path, monkeypatch, args: list[str]
) -> None:
    project = _project(tmp_path, monkeypatch)
    result = _run([*args, "--ssh-key", KEY])

    assert result.exit_code != 0
    output = result.output + str(result.exception or "")
    assert "--server-ip" in output
    assert "--domain" in output

    # Nothing was written -- not even the bin/bay wrapper.
    assert not (project / "group_vars").exists()
    assert not (project / "hosts").exists()
    assert not (project / "bin").exists()


def test_both_flags_present_still_scaffolds(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path, monkeypatch)
    result = _run(
        ["--server-ip", "203.0.113.10", "--domain", "example.test", "--ssh-key", KEY]
    )

    assert result.exit_code == 0, result.output
    assert "203.0.113.10" in (project / "hosts" / "production").read_text()
