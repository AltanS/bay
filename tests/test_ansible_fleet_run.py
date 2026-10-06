"""Ansible runs against the fleet, whatever the working directory is.

The framework checkout holds the playbooks and the fleet holds hosts/,
group_vars/ and .vault_pass. `ansible.run_playbook` has to bring the two
together without reading the working directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bay_cli import ansible


def _fleet(tmp_path: Path) -> Path:
    fleet = tmp_path / "fleet"
    (fleet / "hosts").mkdir(parents=True)
    (fleet / "group_vars" / "all").mkdir(parents=True)
    (fleet / "group_vars" / "production").mkdir()
    (fleet / "hosts" / "production").write_text("[production]\nweb ansible_host=203.0.113.10\n")
    (fleet / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: demo\nansible_user: deploy\n")
    (fleet / "group_vars" / "production" / "main.yml").write_text("---\ndomain_base: example.com\n")
    return fleet


def test_fleet_inventory_links_hosts_files_and_group_vars(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    (fleet / "hosts" / ".hidden").write_text("ignored\n")

    with ansible._fleet_inventory(fleet) as inventory:
        assert sorted(p.name for p in inventory.iterdir()) == ["group_vars", "production"]
        assert (inventory / "production").read_text() == (fleet / "hosts" / "production").read_text()
        assert (inventory / "group_vars" / "all" / "main.yml").is_file()
        assert inventory.parent != fleet
    assert not inventory.exists(), "the run directory is removed afterwards"


def test_fleet_inventory_without_a_fleet_is_none() -> None:
    with ansible._fleet_inventory(None) as inventory:
        assert inventory is None


def test_ansible_reads_the_fleets_group_vars_from_the_run_inventory(tmp_path: Path) -> None:
    """The point of the run directory: group_vars are found with no cwd and no .bay/."""
    fleet = _fleet(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    empty_cfg = tmp_path / "ansible.cfg"
    empty_cfg.write_text("[defaults]\n")

    with ansible._fleet_inventory(fleet) as inventory:
        proc = subprocess.run(
            [sys.executable, "-m", "ansible.cli.inventory", "-i", str(inventory), "--list"],
            cwd=elsewhere,
            env={**os.environ, "ANSIBLE_CONFIG": str(empty_cfg)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert proc.returncode == 0, proc.stderr
    hostvars = json.loads(proc.stdout)["_meta"]["hostvars"]["web"]
    assert hostvars["stack_name"] == "demo"
    assert hostvars["ansible_user"] == "deploy"
    assert hostvars["domain_base"] == "example.com"


def test_fleet_env_replaces_the_fleets_ansible_cfg(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    bay_dir = tmp_path / "framework"

    env = ansible.fleet_env(bay_dir, fleet)
    assert env["ANSIBLE_ROLES_PATH"] == f"{bay_dir / 'vendor' / 'roles'}:{bay_dir / 'roles'}"
    assert env["ANSIBLE_INVENTORY"] == str(fleet / "hosts")
    assert env["ANSIBLE_PIPELINING"] == "True"
    assert "ANSIBLE_VAULT_PASSWORD_FILE" not in env

    (fleet / ".vault_pass").write_text("x\n")
    assert ansible.fleet_env(bay_dir, fleet)["ANSIBLE_VAULT_PASSWORD_FILE"] == str(fleet / ".vault_pass")


def test_run_playbook_uses_absolute_playbook_fleet_inventory_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = _fleet(tmp_path)
    bay_dir = tmp_path / "framework"
    bay_dir.mkdir()
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> None:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        inventory = Path(cmd[cmd.index("-i") + 1])
        seen["inventory_files"] = sorted(p.name for p in inventory.iterdir())

    monkeypatch.setattr(ansible.runner, "run", fake_run)
    monkeypatch.setenv("BAY_NO_MITOGEN", "1")

    ansible.run_playbook(
        "deploy", "production", bay_dir=bay_dir, fleet_root=fleet, tags=["deploy_stack"], extra_args=["--check"]
    )

    cmd = seen["cmd"]
    assert str(bay_dir / "deploy.yml") in cmd
    assert "target_host=production" in cmd
    assert json.dumps({"bay_fleet_root": str(fleet)}) in cmd
    assert seen["inventory_files"] == ["group_vars", "production"]
    assert cmd[cmd.index("--tags") + 1] == "deploy_stack"
    assert cmd[-1] == "--check"
    assert seen["kwargs"]["cwd"] == fleet
    assert seen["kwargs"]["env"]["ANSIBLE_INVENTORY"] == str(fleet / "hosts")


def test_the_deploy_roles_read_the_fleet_from_bay_fleet_root() -> None:
    """The two places that used to assume `<framework>/..` is the fleet."""
    root = Path(__file__).resolve().parent.parent
    assert "bay_fleet_root" in (root / "roles/deploy_stack/tasks/config_files.yml").read_text()
    deploy = (root / "deploy.yml").read_text()
    assert "bay_fleet_root" in deploy
    assert "playbook_dir | dirname" not in deploy
