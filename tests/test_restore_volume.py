"""restore.yml, volume targets: stop every container on the volume, fail on no host.

Two behaviours, both run in a real ansible-playbook with a fake `docker` on PATH.
The tasks are lifted from restore.yml, so the test fails when they change:

* A volume restore stops every running container that mounts the volume, not
  only the owner, and starts that same set afterwards. A container that was
  already stopped stays stopped.
* When no targeted host runs the container of the volume, the play fails with a
  message that names it. It used to end every host and finish green.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

FAKE_DOCKER = """#!/bin/bash
echo "$(hostname -s)-${FAKE_NAME} $*" >> "$FAKE_LOG"
case "$1" in
  container) [[ "${FAKE_HAS}" == 1 ]] || exit 1; echo "/shop" ;;
  ps) for id in ${FAKE_PS}; do echo "$id"; done ;;
  volume) echo "/var/lib/docker/volumes/bay_shop_data/_data" ;;
esac
exit 0
"""


def _all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_all_tasks(task.get(key) or []))
    return out


def _lifted_play() -> dict[str, Any]:
    """The pre_tasks and the volume block of restore.yml, with docker faked."""
    playbook = yaml.safe_load((ROOT / "restore.yml").read_text())[0]
    wanted = ("_volume_key", "_volume_backup", "_is_volume")
    tasks = {t["name"]: t for t in _all_tasks(playbook["pre_tasks"] + playbook["tasks"])}
    block = json.loads(json.dumps(tasks["Restore a volume backup"]))
    for task in _all_tasks(block["block"] + block["always"]):
        if "ansible.builtin.command" in task:
            task["environment"] = "{{ fake_env }}"
        if task["name"] == "Restore the volume into its mountpoint":
            task.clear()
            task.update({"name": "Restore the volume into its mountpoint",
                         "ansible.builtin.command": {"cmd": "true"}, "changed_when": True})
    return {
        "hosts": "all", "gather_facts": False,
        "vars": {k: playbook["vars"][k] for k in wanted} | {
            "stack_name": "bay", "accessory": "bay_shop_data", "_snap": {"short_id": "abc"},
            "volume_backups": {"shop_data": {"container": "shop", "path": "/app/data"}},
        },
        "pre_tasks": [
            {**t, "environment": "{{ fake_env }}"} if "ansible.builtin.command" in t else t
            for t in (tasks["Find the container of the volume on this host"],
                      tasks["Fail when no targeted host runs the container of the volume"],
                      tasks["Skip a host that does not run the container of the volume"])
        ],
        "tasks": [block],
    }


def _run(tmp_path: Path, hosts: dict[str, dict[str, str]]) -> tuple[int, str, list[str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    log.write_text("")
    inventory = {"all": {"hosts": {
        name: {"ansible_connection": "local", "fake_env": {**env, "FAKE_NAME": name, "FAKE_LOG": str(log)}}
        for name, env in hosts.items()
    }}}
    (tmp_path / "inventory.yml").write_text(yaml.safe_dump(inventory))
    (tmp_path / "play.yml").write_text(yaml.safe_dump([_lifted_play()]))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", str(tmp_path / "inventory.yml"),
         str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    calls = [line.split(" ", 1)[1] for line in log.read_text().splitlines()]
    return proc.returncode, proc.stdout + proc.stderr, calls


def test_restore_stops_and_starts_every_container_on_the_volume(tmp_path: Path) -> None:
    code, out, calls = _run(tmp_path, {"h1": {"FAKE_HAS": "1", "FAKE_PS": "c1 c2 c3"}})
    assert code == 0, out
    assert "ps -q --filter volume=bay_shop_data" in calls
    assert "stop c1 c2 c3" in calls and "start c1 c2 c3" in calls
    assert calls.index("stop c1 c2 c3") < calls.index("start c1 c2 c3")


def test_restore_starts_only_what_ran_before(tmp_path: Path) -> None:
    # `docker ps -q` lists running containers only: nothing running, nothing to stop or start.
    code, out, calls = _run(tmp_path, {"h1": {"FAKE_HAS": "1", "FAKE_PS": ""}})
    assert code == 0, out
    assert not [c for c in calls if c.startswith(("stop", "start"))]


def test_restore_fails_when_no_host_runs_the_container(tmp_path: Path) -> None:
    code, out, calls = _run(tmp_path, {"h1": {"FAKE_HAS": "0"}, "h2": {"FAKE_HAS": "0"}})
    assert code != 0
    assert "'shop'" in out and "bay_shop_data" in out and "Nothing was restored" in out
    assert not [c for c in calls if c.startswith(("stop", "start"))]


def test_restore_skips_hosts_without_the_container_when_one_has_it(tmp_path: Path) -> None:
    code, out, calls = _run(
        tmp_path, {"h1": {"FAKE_HAS": "0"}, "h2": {"FAKE_HAS": "1", "FAKE_PS": "c9"}}
    )
    assert code == 0, out
    assert "stop c9" in calls and "start c9" in calls
