"""`bay provision` locks the root password (no console password login)."""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parent.parent / "roles" / "users"


def _root_lock_tasks() -> list[dict]:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())
    return [
        t for t in tasks
        if "ansible.builtin.user" in t
        and t["ansible.builtin.user"].get("name") == "root"
        and t["ansible.builtin.user"].get("password_lock") is True
    ]


def test_the_default_locks_root():
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["users_lock_root_password"] is True


def test_one_task_locks_root_and_honours_the_flag():
    tasks = _root_lock_tasks()
    assert len(tasks) == 1
    assert "users_lock_root_password" in tasks[0]["when"]
