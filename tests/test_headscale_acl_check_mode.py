"""A dry run validates the Headscale ACL policy (2.20.0).

Before 2.20.0, `--check` skipped "Check headscale container is running", then
printed "SKIPPING ACL validation, the headscale container is not running"
(false), and a new policy was first validated in the real run. Now the probe,
the staging write, `headscale policy check` and the cleanup carry
`check_mode: false`. Only the install stays skipped in check mode.

The ACL tasks of roles/headscale/tasks/main.yml run here through a small
interpreter that does what Ansible does with them in check mode: a `command`
without `check_mode: false` is skipped, a file module without it writes
nothing, `when` and `assert` are evaluated as Jinja.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment

REPO = Path(__file__).resolve().parent.parent
TASKS = REPO / "roles" / "headscale" / "tasks" / "main.yml"
STAGED = "/opt/headscale/config/.policy.hujson.staged"
LIVE = "/opt/headscale/config/policy.hujson"
FIRST = "Check headscale container is running for policy validation"
LAST = "Remove staged headscale ACL policy"
WARNING = "SKIPPING ACL validation"


class PlayFailed(Exception):
    pass


@dataclass
class Box:
    running: bool = True
    config_dir: bool = True
    policy_valid: bool = True
    files: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def _acl_tasks() -> list[dict]:
    tasks = yaml.safe_load(TASKS.read_text())
    names = [t.get("name") for t in tasks]
    return tasks[names.index(FIRST): names.index(LAST) + 1]


def _env() -> Environment:
    env = Environment()
    env.tests["skipped"] = lambda r: isinstance(r, dict) and bool(r.get("skipped"))
    return env


def _true(expr: Any, ctx: dict) -> bool:
    return bool(_env().compile_expression(str(expr).strip())(**ctx))


def _run(box: Box, *, check: bool) -> Box:
    ctx: dict[str, Any] = {"ansible_check_mode": check, "headscale_acl_policy": {"acls": []}}
    for task in _acl_tasks():
        when = task.get("when", [])
        if not all(_true(c, ctx) for c in (when if isinstance(when, list) else [when])):
            if "register" in task:
                ctx[task["register"]] = {"skipped": True, "changed": False}
            continue
        forced = task.get("check_mode") is False
        acts = not check or forced
        if "ansible.builtin.command" in task:
            cmd = " ".join(str(task["ansible.builtin.command"]["cmd"]).split())
            if not acts:  # command does not support check mode: skipped
                ctx[task["register"]] = {"skipped": True, "changed": False}
                continue
            box.commands.append(cmd)
            if cmd.startswith("docker ps"):
                result = {"rc": 0, "stdout": "f00dcafe" if box.running else "", "stderr": ""}
            elif "policy check" in cmd:
                assert STAGED in box.files, "validated a policy that was never staged"
                result = {"rc": 0 if box.policy_valid else 1, "stdout": "",
                          "stderr": "" if box.policy_valid else "invalid policy"}
            else:
                raise AssertionError(f"unexpected command {cmd}")
            ctx[task["register"]] = result
        elif "ansible.builtin.template" in task:
            dest = task["ansible.builtin.template"]["dest"]
            if acts:
                if not box.config_dir:
                    raise PlayFailed(f"destination directory of {dest} does not exist")
                box.files.add(dest)
        elif "ansible.builtin.copy" in task:
            mod = task["ansible.builtin.copy"]
            if acts:
                if mod["src"] not in box.files:
                    raise PlayFailed(f"Source {mod['src']} not found")
                box.files.add(mod["dest"])
        elif "ansible.builtin.file" in task:
            if acts:
                box.files.discard(task["ansible.builtin.file"]["path"])
        elif "ansible.builtin.debug" in task:
            box.messages.append(" ".join(str(task["ansible.builtin.debug"]["msg"]).split()))
        elif "ansible.builtin.assert" in task:
            if not all(_true(c, ctx) for c in task["ansible.builtin.assert"]["that"]):
                raise PlayFailed(task["ansible.builtin.assert"]["fail_msg"])
        else:
            raise AssertionError(f"unknown module in {task['name']}")
    return box


def test_dry_run_fails_on_an_invalid_policy() -> None:
    box = Box(policy_valid=False)
    with pytest.raises(PlayFailed, match="INVALID"):
        _run(box, check=True)
    assert any("policy check" in c for c in box.commands)
    assert not [m for m in box.messages if WARNING in m]
    # Nothing installed, and the staged file is gone again.
    assert box.files == set()


def test_dry_run_passes_a_valid_policy_and_leaves_nothing() -> None:
    box = _run(Box(), check=True)
    assert any("policy check" in c for c in box.commands)
    assert not [m for m in box.messages if WARNING in m]
    assert box.files == set()


def test_dry_run_with_no_container_says_so_truthfully() -> None:
    """A fresh host in check mode: no folder, no container. No write, no crash."""
    box = _run(Box(running=False, config_dir=False), check=True)
    assert [m for m in box.messages if WARNING in m]
    assert not any("policy check" in c for c in box.commands)
    assert box.files == set()


def test_real_run_installs_a_valid_policy() -> None:
    box = _run(Box(), check=False)
    assert box.files == {LIVE}


def test_real_run_keeps_a_rejected_policy_for_inspection() -> None:
    box = Box(policy_valid=False)
    with pytest.raises(PlayFailed, match="kept at"):
        _run(box, check=False)
    assert box.files == {STAGED}


def test_real_run_without_container_installs_unvalidated_with_a_warning() -> None:
    box = _run(Box(running=False), check=False)
    assert [m for m in box.messages if WARNING in m]
    assert box.files == {LIVE}


def test_read_only_tasks_run_in_check_mode_and_install_does_not() -> None:
    by_name = {t["name"]: t for t in _acl_tasks()}
    for name in (FIRST, "Stage headscale ACL policy for validation",
                 "Validate staged headscale ACL policy",
                 "Remove the staged headscale ACL policy after a dry-run check", LAST):
        assert by_name[name]["check_mode"] is False, name
    assert "check_mode" not in by_name["Install validated headscale ACL policy"]
    for name in (FIRST, "Validate staged headscale ACL policy"):
        assert by_name[name]["become_user"] == "root", name
        assert by_name[name]["changed_when"] is False, name
