"""A directory mount compiles to one config file per file, through the path `bay up` runs.

``plan._materialize`` builds the scratch copy that ``bay compile``, ``bay plan``
and ``bay up`` compile. It once left out the fleet's ``files/`` tree, so a
``from =`` directory collapsed into one entry named after the directory.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from bay_cli import plan as planmod
from bay_cli.context import Context

FLEET_TOML = """\
name = "demo"
default_box = "box-1"
default_domain = "example.com"
primary_env = "production"

[boxes.box-1]
env = "production"

[tailnet]
allowlist = ["100.64.0.0/10"]
"""

APP_TOML = """\
name = "site"
fleet = "demo"
image = "ghcr.io/acme/site:1"
port = 3000

[access]
mode = "public"

[[mounts]]
path = "/content/legal"
from = "legal/app"
mode = "0644"

[deploy.production]
domain = "site.example.com"
"""

LEGAL = ("de/imprint.md", "de/privacy.md", "en/imprint.md")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def _fleet(tmp_path: Path, *, legal: tuple[str, ...]) -> Path:
    fleet = tmp_path / "fleet"
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: demo\n")
    (fleet / "projects" / "site").mkdir(parents=True)
    (fleet / "bay.fleet.toml").write_text(FLEET_TOML)
    (fleet / "projects" / "site" / "bay.toml").write_text(APP_TOML)
    lock = {
        "lock_version": 1,
        "name": "site",
        "repo": None,
        "commit": None,
        "local_path": None,
        "envs": {
            "production": {
                "box": "box-1",
                "adopted": {"files": {"legal/app": "legal/app"}},
            }
        },
    }
    (fleet / "projects" / "site.lock").write_text(json.dumps(lock, indent=2) + "\n")
    for rel in legal:
        path = fleet / "files" / "legal" / "app" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n")
    _git(fleet, "init", "-q")
    _git(fleet, "add", "-A")
    _git(fleet, "commit", "-q", "-m", "fleet")
    return fleet


def test_directory_mount_compiles_to_one_entry_per_file(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, legal=LEGAL)
    # An uncommitted file is not in the fleet at HEAD, so it is not compiled.
    (fleet / "files" / "legal" / "app" / "de" / "draft.md").write_text("draft\n")
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.errors == []
        assert comp.result is not None
        data = yaml.safe_load(comp.result.body())
    files = data["services"]["site"]["config_files"]
    assert files == [f"legal/app/{rel}" for rel in sorted(LEGAL)]


def test_missing_directory_fails_naming_project_mount_and_path(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, legal=())
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.result is None
        assert len(comp.errors) == 1
        (line,) = comp.errors
    assert "site" in line
    assert "mounts[0]" in line
    assert "from = 'legal/app' cannot be listed" in line
    assert "files/legal/app" in line


def test_files_tree_is_read_at_head_not_the_working_tree(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path, legal=LEGAL)
    # Deleted in the working tree only: HEAD still holds it.
    (fleet / "files" / "legal" / "app" / "de" / "imprint.md").unlink()
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.result is not None, comp.errors
        data = yaml.safe_load(comp.result.body())
    assert "legal/app/de/imprint.md" in data["services"]["site"]["config_files"]


@pytest.mark.parametrize("git_repo", [True, False])
def test_fleet_without_git_copies_files_as_they_are(tmp_path: Path, git_repo: bool) -> None:
    fleet = _fleet(tmp_path, legal=LEGAL)
    if not git_repo:
        subprocess.run(["rm", "-rf", str(fleet / ".git")], check=True)
    with planmod.compiled_fleet(Context.for_fleet_root(fleet)) as comp:
        assert comp.result is not None, comp.errors
        data = yaml.safe_load(comp.result.body())
    assert len(data["services"]["site"]["config_files"]) == 3
