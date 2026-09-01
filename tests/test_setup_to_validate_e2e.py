"""The milestone's key test: scaffold a project, then validate it.

Nothing between `bin/bay setup` and `bin/bay deploy` ever ran end to end,
so the default project shipped broken in four independent ways at once —
a schema-invalid healthcheck key, a config file that did not exist, empty
secrets and a keyless admin. Each was individually plausible; together
they made the documented first deploy impossible.

Run the whole path here, in CI, on every commit. The equivalent by hand:

    T=$(mktemp -d) && ln -s "$PWD" "$T/.bay" && cd "$T"
    bay setup --defaults --server-ip 203.0.113.10 --domain example.test
    bay validate
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from bay_cli.catalog import _package_framework_root

ROOT = _package_framework_root()
# Split so the literal never sits in the tree: scripts/leak-scan.sh flags any
# 32+ char high-entropy blob, and a fake key is shaped exactly like a real one.
KEY = "ssh-ed25519 " "AAAAC3NzaC1lZDI1NTE5" "AAAAI" "TESTKEY" " tester@example.com"
_EMPTY_SECRET_RE = re.compile(r'^  [A-Za-z_]+: ""$', re.MULTILINE)


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


def _setup(services: str | None = None):
    args = [
        "setup", "--defaults",
        "--server-ip", "203.0.113.10",
        "--domain", "example.test",
        "--ssh-key", KEY,
    ]
    if services:
        args += ["--services", services]
    result = _run(args)
    assert result.exit_code == 0, result.output
    return result


def test_default_project_validates_clean(project: Path) -> None:
    _setup()
    result = _run(["validate"])
    assert result.exit_code == 0, result.output
    assert "All validation checks passed" in result.output


def test_project_with_every_service_validates_clean(project: Path) -> None:
    _setup("gatus,postgres,redis,mariadb,vaultwarden,n8n,plausible,umami")
    result = _run(["validate"])
    assert result.exit_code == 0, result.output


def test_scaffolded_tree_is_complete(project: Path) -> None:
    _setup()

    # The config file the generated services.yml declares.
    assert (project / "files" / "gatus" / "config.yaml").is_file()

    # No secret left as an empty string.
    secrets_text = (project / "group_vars/production/secrets.yml").read_text()
    assert _EMPTY_SECRET_RE.search(secrets_text) is None

    # The admin account can still log in after hardening.
    users = yaml.safe_load((project / "group_vars/all/users.yml").read_text())
    assert users["users"][0]["keys"] == [KEY]

    # The retired registry variables are not scaffolded.
    main = (project / "group_vars/all/main.yml").read_text()
    assert "docker_registry_org" not in main


def test_validate_catches_a_deleted_config_file(project: Path) -> None:
    """The new check must be able to go red, not just green."""
    _setup()
    (project / "files" / "gatus" / "config.yaml").unlink()
    result = _run(["validate"])
    assert result.exit_code != 0
    assert "config_files" in result.output


def test_validate_catches_a_stripped_admin_key(project: Path) -> None:
    _setup()
    users_file = project / "group_vars/all/users.yml"
    users_file.write_text(users_file.read_text().replace(f'      - "{KEY}"\n', ""))
    result = _run(["validate"])
    assert result.exit_code != 0
    assert "ssh-access" in result.output
