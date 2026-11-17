"""Traefik restarts for a new static config only after the container pass (2.23.1).

Before 2.23.1 the traefik role notified a handler, ``Restart traefik``. The
log_archive role runs before deploy_stack in the "Deploy services" play of
deploy.yml and runs ``meta: flush_handlers`` (it needs ``daemon-reload`` before
it starts its timers). A flush runs every pending handler of the play, so
Traefik restarted before the reconciler gave the containers their new labels.
On a switch to ``traefik_split_entrypoints`` tailnet clients got 404 for about
20 s: Traefik listened on the tailnet address and no router had
``websecure_tailnet`` yet.

Now the traefik role marks the restart as pending (``traefik_restart_pending``),
and ``roles/traefik/tasks/restart.yml`` runs right after the container pass in
deploy_stack, or in the post_tasks for a run without deploy_stack.

1. A static play-order check over deploy.yml: no handler that a role before
   deploy_stack can notify restarts Traefik, when a role before deploy_stack
   flushes handlers.
2. The real ``ansible-playbook --list-tasks`` for a full run, ``--tags
   deploy_stack`` and ``--tags traefik``: the restart task comes after the
   container pass and after the task that marks it pending.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS = ROOT / "vendor" / "collections"

PLAY = "Deploy services"
RESTART_TASK = "traefik : Restart traefik for its new static config"
MARK_TASK = "traefik : Mark the traefik restart as pending"
CONTAINER_PASS = "Deploy containers"
DOCKER_MODULES = (
    "community.docker.docker_container",
    "docker_container",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "command",
    "shell",
)


def _load(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text()) or []


def _flat(tasks: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _flat(task[key])


def _play_roles() -> list[str]:
    plays = _load(ROOT / "deploy.yml")
    play = next(p for p in plays if p.get("name") == PLAY)
    return [r["role"] if isinstance(r, dict) else r for r in play["roles"]]


def _role_tasks(role: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in sorted((ROOT / "roles" / role / "tasks").rglob("*.yml")):
        tasks.extend(_flat(_load(path)))
    return tasks


def _notified(task: dict[str, Any]) -> list[str]:
    value = task.get("notify") or []
    return [value] if isinstance(value, str) else list(value)


def _flushes(task: dict[str, Any]) -> bool:
    meta = task.get("ansible.builtin.meta", task.get("meta"))
    return meta == "flush_handlers"


def _handlers(roles: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Every handler topic of the play's roles: its name and its ``listen``."""
    topics: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        path = ROOT / "roles" / role / "handlers" / "main.yml"
        if not path.is_file():
            continue
        for handler in _flat(_load(path)):
            listen = handler.get("listen") or []
            names = [handler.get("name")] + ([listen] if isinstance(listen, str) else list(listen))
            for name in filter(None, names):
                topics.setdefault(name, []).append(handler)
    return topics


def _restarts_traefik(handler: dict[str, Any]) -> bool:
    for module in DOCKER_MODULES:
        if module in handler and "traefik" in yaml.safe_dump(handler[module]).lower():
            return True
    return False


def test_no_early_flush_can_restart_traefik() -> None:
    roles = _play_roles()
    before = roles[: roles.index("deploy_stack")]
    flushing = [i for i, role in enumerate(before) if any(_flushes(t) for t in _role_tasks(role))]
    assert flushing, "test premise: a role before deploy_stack flushes handlers (log_archive)"

    topics = _handlers(roles)
    # Every role up to the last early flush may have notified a handler by then.
    pending = [n for role in before[: flushing[-1] + 1] for t in _role_tasks(role) for n in _notified(t)]
    seen: set[str] = set()
    hits: list[str] = []
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for handler in topics.get(name, []):
            if _restarts_traefik(handler):
                hits.append(f"{name!r} -> {handler.get('name')!r}")
            pending.extend(_notified(handler))
    assert not hits, (
        "a handler that restarts Traefik can be notified before the handler flush in "
        f"{before[flushing[-1]]!r}, so it fires before deploy_stack applies the new "
        f"labels: {hits}. Mark the restart pending instead (roles/traefik/tasks/restart.yml)."
    )


def test_deploy_stack_restarts_traefik_right_after_the_container_pass() -> None:
    block = next(t for t in _load(ROOT / "roles/deploy_stack/tasks/main.yml") if t.get("name") == "Deploy stack")
    names = [t.get("name") for t in block["block"]]
    at = names.index(CONTAINER_PASS)
    restart = block["block"][at + 1]
    assert restart.get("ansible.builtin.import_role") == {"name": "traefik", "tasks_from": "restart"}
    assert not any(_flushes(t) for t in _flat(block["block"][:at]))


# ── real ansible-playbook --list-tasks ────────────────────────────────────


def _list_tasks(tmp_path: Path, *extra: str) -> list[str]:
    inventory = tmp_path / "hosts"
    inventory.write_text("[production]\n192.0.2.11 ansible_connection=local\n")
    env = {
        **os.environ,
        "ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS),
        "ANSIBLE_ROLES_PATH": f"{ROOT / 'vendor' / 'roles'}:{ROOT / 'roles'}",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", str(ROOT / "deploy.yml"),
         "-i", str(inventory), "-e", "target_host=production", "--list-tasks", *extra],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return [line.strip().split("\t")[0] for line in proc.stdout.splitlines()]


@pytest.mark.skipif(
    not (COLLECTIONS / "ansible_collections").is_dir(),
    reason="needs the vendored collections (make install)",
)
@pytest.mark.parametrize(
    ("tags", "after_container_pass"),
    [((), True), (("--tags", "deploy_stack"), True), (("--tags", "traefik"), False)],
    ids=["full", "deploy_stack", "traefik"],
)
def test_the_restart_runs_after_the_labels(tmp_path: Path, tags: tuple[str, ...], after_container_pass: bool) -> None:
    tasks = _list_tasks(tmp_path, *tags)
    assert MARK_TASK in tasks
    restarts = [i for i, name in enumerate(tasks) if name == RESTART_TASK]
    assert restarts, f"no Traefik restart task in a run with {tags or 'no tags'}"
    assert min(restarts) > tasks.index(MARK_TASK)
    if after_container_pass:
        assert min(restarts) > tasks.index(CONTAINER_PASS)
    else:
        assert CONTAINER_PASS not in tasks
