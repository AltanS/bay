"""`kernel_module_blocklist` blocks kernel modules with a modprobe.d file.

Context: nginx2root (2026-11-18) chains nginx RCEs with an unpatched SCTP
use-after-free to get host root, also from inside a container. SCTP is a
module on every box and autoloads on socket(). The role makes modprobe run
/bin/false for it. It never unloads a module: a loaded one only warns.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

from helpers import run_role_tasks

_REPO = Path(__file__).parent.parent
_ROLE = _REPO / "roles" / "kernel_module_blocklist"
_TASKS = _ROLE / "tasks" / "main.yml"
_DEST = "/etc/modprobe.d/bay-blocklist.conf"


def _tasks() -> list[dict]:
    return yaml.safe_load(_TASKS.read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t["name"] == name)


def _render(modules: list[str]) -> str:
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    template = env.from_string((_ROLE / "templates" / "modprobe-blocklist.conf.j2").read_text())
    return template.render(kernel_module_blocklist=modules)


def test_default_blocks_sctp() -> None:
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["kernel_module_blocklist"] == ["sctp"]


def test_template_has_install_and_blacklist_per_module() -> None:
    lines = _render(["sctp", "dccp"]).splitlines()
    assert lines[0].startswith("# Managed by Bay")
    body = [line for line in lines if not line.startswith("#")]
    assert body == [
        "install sctp /bin/false",
        "blacklist sctp",
        "install dccp /bin/false",
        "blacklist dccp",
    ]


def test_file_is_root_owned_0644_and_runs_as_root() -> None:
    task = _task("Write the kernel module blocklist")
    template = task["ansible.builtin.template"]
    assert template["dest"] == _DEST
    assert template["owner"] == "root"
    assert template["group"] == "root"
    assert template["mode"] == "0644"
    assert task["become"] is True
    assert task["become_user"] == "root"


def test_empty_list_removes_the_file() -> None:
    task = _task("Remove the kernel module blocklist when the list is empty")
    assert task["ansible.builtin.file"] == {"path": _DEST, "state": "absent"}
    assert task["become_user"] == "root"
    assert task["when"] == "kernel_module_blocklist | length == 0"


def test_role_never_unloads_a_module() -> None:
    text = _TASKS.read_text()
    assert "modprobe -r" not in text
    assert "community.general.modprobe" not in text
    assert "update-initramfs" not in text
    commands = [t for t in _tasks() if any(k.endswith((".command", ".shell")) for k in t)]
    assert commands == []


def test_provision_runs_the_role_with_its_own_tag() -> None:
    plays = yaml.safe_load((_REPO / "provision.yml").read_text())
    roles = [r for play in plays for r in play.get("roles", [])]
    entry = next(r for r in roles if r["role"] == "kernel_module_blocklist")
    assert "kernel_module_blocklist" in entry["tags"]
    assert "security" in entry["tags"]


def _point_at(tmp_path: Path):
    def transform(task: dict) -> dict:
        task.pop("become", None)
        task.pop("become_user", None)
        if "ansible.builtin.stat" in task:
            task["ansible.builtin.stat"] = {"path": f"{tmp_path}/sys/module/{{{{ item }}}}"}
        return task

    return transform


def test_loaded_module_warns_and_does_not_fail(tmp_path: Path) -> None:
    (tmp_path / "sys" / "module" / "sctp").mkdir(parents=True)
    result = run_role_tasks(
        tmp_path,
        _TASKS,
        ["Check whether a blocked module is loaded", "Warn about a blocked module that is loaded"],
        {"kernel_module_blocklist": ["sctp", "dccp"]},
        transform=_point_at(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: kernel module sctp is loaded" in result.stdout
    assert "kernel module dccp is loaded" not in result.stdout
    assert "failed=0" in result.stdout


def test_no_loaded_module_prints_no_warning(tmp_path: Path) -> None:
    result = run_role_tasks(
        tmp_path,
        _TASKS,
        ["Check whether a blocked module is loaded", "Warn about a blocked module that is loaded"],
        {"kernel_module_blocklist": ["sctp"]},
        transform=_point_at(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout
