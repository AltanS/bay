"""The build-control directories must agree across every task that touches them.

roles/git_deploy/tasks/webhook.yml creates `{{ stack_dir }}/state` and
`{{ stack_dir }}/triggers` as 2770, owner app_user, group
`git_deploy_build_group` — the shared group that lets both the webhook
container (UID 10001) and rebuild.sh (run as app_user via a systemd path
unit) write into them.

roles/git_deploy/tasks/systemd.yml used to redeclare `{{ stack_dir }}/state`
itself, as 0755 with no group and no `become`. It always runs immediately
after webhook.yml (see main.yml), so on every single deploy it silently
flipped the directory it had just been given back to 0755 — locking the
webhook container out of its own state directory (it could no longer write
telegram-failures.log) and making three tasks report "changed" on every run
for no functional reason.

This test loads every task file under roles/git_deploy/tasks/, collects every
`ansible.builtin.file` task whose `path` is the state or triggers directory,
and asserts they all declare the identical owner/group/mode. It must go red
if any one of them drifts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TASKS_DIR = _REPO_ROOT / "roles" / "git_deploy" / "tasks"

_TARGET_PATHS = {
    "{{ stack_dir }}/state",
    "{{ stack_dir }}/triggers",
}

_EXPECTED_OWNER = "{{ app_user }}"
_EXPECTED_GROUP = "{{ git_deploy_build_group }}"
_EXPECTED_MODE = "2770"


def _flatten(tasks):
    """Tasks in this repo live inside `block:` as often as at the top level."""
    for task in tasks:
        if task is None:
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _flatten(task[key])


def _collect_state_dir_file_tasks():
    """Every `ansible.builtin.file` task that creates/owns state/ or triggers/.

    Excludes the one-off "Re-group pre-existing state files" migration task
    (recurse: true, mode "g+w") — it propagates the group onto files written
    under the old 0777 scheme, and deliberately does not restate owner/mode
    for the directory itself. It is a different concern from "who owns this
    directory going forward", which is what this test guards.

    Returns a list of (task_file_name, task_name, task) tuples.
    """
    found = []
    for task_file in sorted(_TASKS_DIR.glob("*.yml")):
        loaded = yaml.safe_load(task_file.read_text())
        if not loaded:
            continue
        for task in _flatten(loaded):
            if not isinstance(task, dict):
                continue
            args = task.get("ansible.builtin.file")
            if not isinstance(args, dict):
                continue
            if args.get("path") not in _TARGET_PATHS:
                continue
            if args.get("recurse"):
                continue
            found.append((task_file.name, task.get("name", "<unnamed>"), task))
    return found


def test_at_least_two_tasks_manage_the_state_or_triggers_dir():
    """Sanity check: the test isn't silently matching nothing."""
    found = _collect_state_dir_file_tasks()
    assert len(found) >= 2, (
        "expected multiple ansible.builtin.file tasks targeting "
        "{{ stack_dir }}/state or {{ stack_dir }}/triggers across "
        "roles/git_deploy/tasks/*.yml; found none or too few — did the "
        "task/path spelling change?"
    )


def test_every_state_or_triggers_dir_task_agrees_on_owner_group_mode():
    found = _collect_state_dir_file_tasks()
    for task_file, task_name, task in found:
        args = task["ansible.builtin.file"]
        assert args.get("owner") == _EXPECTED_OWNER, (
            f"{task_file}::{task_name!r} sets owner={args.get('owner')!r}, "
            f"expected {_EXPECTED_OWNER!r}"
        )
        assert args.get("group") == _EXPECTED_GROUP, (
            f"{task_file}::{task_name!r} sets group={args.get('group')!r}, "
            f"expected {_EXPECTED_GROUP!r} — a task with no group (or the "
            f"wrong one) silently locks the webhook container out of the "
            f"directory the next time it runs"
        )
        assert args.get("mode") == _EXPECTED_MODE, (
            f"{task_file}::{task_name!r} sets mode={args.get('mode')!r}, "
            f"expected {_EXPECTED_MODE!r}"
        )
        assert task.get("become") is True or task.get("become_user") == "root", (
            f"{task_file}::{task_name!r} must run privileged (become: true, "
            f"become_user: root) to chown/chgrp the directory"
        )
