"""Scaffolded ``secrets.yml`` must be mode 0600 the moment it is written.

A scaffolded ``group_vars/production/secrets.yml`` holds real, generated
passwords in plaintext until the operator runs ``bin/bay vault encrypt``.
Writing it under the process umask (typically 0644) leaves those passwords
readable by any local user until encryption happens. Both scaffold paths —
the templated ``--defaults`` render and the ``--no-interactive`` example
copy — must chmod it to 0600 as soon as the file lands, not only when
``_fill_empty_secrets`` happens to rewrite it.

The equivalent by hand:

    T=$(mktemp -d) && ln -s "$PWD" "$T/.bay" && cd "$T"
    bay setup --defaults --server-ip 203.0.113.10 --domain example.test
    stat -c '%a' group_vars/production/secrets.yml   # must be 600
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root

ROOT = _package_framework_root()
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "myproj"
    root.mkdir()
    (root / ".bay").symlink_to(ROOT)
    monkeypatch.chdir(root)
    return root


def _run(args: list[str]):
    from bay_cli.cli import app

    return CliRunner().invoke(app, args)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_defaults_render_hardens_secrets_yml(project: Path) -> None:
    result = _run(
        [
            "setup", "--defaults",
            "--server-ip", "203.0.113.10",
            "--domain", "example.test",
            "--ssh-key", KEY,
        ]
    )
    assert result.exit_code == 0, result.output

    secrets_file = project / "group_vars/production/secrets.yml"
    assert secrets_file.is_file()
    assert _mode(secrets_file) == 0o600


def test_defaults_render_does_not_harden_non_secrets_files(project: Path) -> None:
    result = _run(
        [
            "setup", "--defaults",
            "--server-ip", "203.0.113.10",
            "--domain", "example.test",
            "--ssh-key", KEY,
        ]
    )
    assert result.exit_code == 0, result.output

    domains_file = project / "group_vars/production/domains.yml"
    assert domains_file.is_file()
    assert _mode(domains_file) != 0o600


def test_no_interactive_example_copy_hardens_secrets_yml(project: Path, tmp_path: Path) -> None:
    key_file = tmp_path / "id_test.pub"
    key_file.write_text(KEY + "\n")
    result = _run(
        [
            "setup", "--no-interactive",
            "--server-ip", "203.0.113.10",
            "--domain", "example.test",
            "--ssh-key-file", str(key_file),
        ]
    )
    assert result.exit_code == 0, result.output

    secrets_file = project / "group_vars/production/secrets.yml"
    assert secrets_file.is_file()
    assert _mode(secrets_file) == 0o600


def test_no_interactive_example_copy_does_not_harden_non_secrets_files(project: Path, tmp_path: Path) -> None:
    key_file = tmp_path / "id_test.pub"
    key_file.write_text(KEY + "\n")
    result = _run(
        [
            "setup", "--no-interactive",
            "--server-ip", "203.0.113.10",
            "--domain", "example.test",
            "--ssh-key-file", str(key_file),
        ]
    )
    assert result.exit_code == 0, result.output

    domains_file = project / "group_vars/production/domains.yml"
    assert domains_file.is_file()
    assert _mode(domains_file) != 0o600
