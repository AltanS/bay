"""The live restore lock (2.24.0 review fix).

`bay backup restore --into live` stops the clients of the database and writes
`<backup_lock_dir>/restore-<target>.lock` until they run again. While a fresh
lock exists (younger than 12 hours: an older one is from a restore that died):

- the nightly `docker system prune -af` does not run: it removes stopped
  containers, so it would delete the stopped clients;
- a deploy stops before it changes a container, with a message that names
  the lock;
- rebuild.sh refuses the recreate (tests/test_rebuild_preflight.py).
"""
from __future__ import annotations

import json
import shlex
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _task(path: Path, name: str) -> dict[str, Any]:
    def walk(items):
        for t in items or []:
            yield t
            for key in ("block", "rescue", "always"):
                yield from walk(t.get(key))

    return next(t for t in walk(yaml.safe_load(path.read_text())) if t.get("name") == name)


def _lock(dir_: Path, age_hours: float) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    lock = dir_ / "restore-shop-postgres.lock"
    lock.write_text("restore of shop-postgres\n")
    then = time.time() - age_hours * 3600
    os.utime(lock, (then, then))
    return lock


# ── the nightly prune ───────────────────────────────────────────────────────


def _prune_job(stack_dir: Path) -> str:
    task = _task(ROOT / "roles" / "cronjobs" / "tasks" / "main.yml", "Schedule Docker system prune")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: bool(v)
    env.filters["quote"] = shlex.quote
    return env.from_string(task["ansible.builtin.cron"]["job"]).render(
        docker_prune_volumes=False, stack_dir=str(stack_dir))


@pytest.mark.parametrize(("age_hours", "prunes"), [(None, True), (0, False), (11, False), (13, True)])
def test_the_nightly_prune_skips_while_a_live_restore_runs(tmp_path: Path, age_hours, prunes) -> None:
    stack = tmp_path / "stack"
    (stack / "backup" / "locks").mkdir(parents=True)
    if age_hours is not None:
        _lock(stack / "backup" / "locks", age_hours)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\n')
    (bin_dir / "docker").chmod(0o755)
    job = _prune_job(stack).replace("/var/log/docker-prune.log", str(tmp_path / "prune.log"))
    proc = subprocess.run(["sh", "-c", job], env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    pruned = (tmp_path / "docker.log").exists()
    assert pruned is prunes
    if not prunes:
        assert "restore" in (tmp_path / "prune.log").read_text()


# ── a deploy ────────────────────────────────────────────────────────────────

REFUSE = "Refuse to deploy while a live restore holds clients stopped"


def _deploy_check(tmp_path: Path, **variables: Any) -> subprocess.CompletedProcess[str]:
    task = _task(ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml", REFUSE)
    tasks = [{k: v for k, v in t.items() if k not in ("become", "become_user")}
             for t in [_task(ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml",
                             "Find a running live restore"), task]]
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_LOCALHOST_WARNING": "0", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
           "ANSIBLE_PYTHON_INTERPRETER": sys.executable}
    return subprocess.run([sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
                           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize(("age_hours", "refused"), [(None, False), (0, True), (13, False)])
def test_a_deploy_refuses_while_a_live_restore_runs(tmp_path: Path, age_hours, refused) -> None:
    stack = tmp_path / "stack"
    if age_hours is not None:
        _lock(stack / "backup" / "locks", age_hours)
    proc = _deploy_check(tmp_path, stack_dir=str(stack))
    assert (proc.returncode != 0) is refused, proc.stdout[-3000:]
    if refused:
        assert "restore-shop-postgres.lock" in proc.stdout
        assert "live restore" in proc.stdout


def test_the_deploy_check_runs_before_any_container_changes() -> None:
    top = yaml.safe_load((ROOT / "roles" / "deploy_stack" / "tasks" / "main.yml").read_text())
    block = next(t for t in top if t.get("name") == "Deploy stack")["block"]
    names = [t.get("name") for t in block]
    assert REFUSE in names
    wait = names.index("Wait for active backups to complete before deploy")
    assert names.index(REFUSE) == wait + 2
