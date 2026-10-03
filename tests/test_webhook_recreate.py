"""The git_deploy role must leave the webhook receiver on its current image.

Bug (found in production, 2026-10-03): `webhook.yml` rebuilt
`bay-webhook:latest` when the receiver files changed, but nothing recreated
the running container. The `Restart bay-webhook` handler only restarts it, and
a restart keeps the image the container was created from. After a release that
changed app.py, `--tags git_deploy` left every host on a receiver weeks old.
Only `--tags deploy_stack` recreated it.

The fix runs the container_lifecycle reconciler for the receiver alone, from
git_deploy. The reconciler already compares the running container's image ID
with the local one on every run (planner: `image_drifted`), so a stale
receiver heals and a current one is a NoOp. These tests pin the wiring, since
the behaviour itself needs a real host.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_GIT_DEPLOY = _ROOT / "roles" / "git_deploy"
_LIFECYCLE = _ROOT / "roles" / "container_lifecycle" / "tasks"


def _load(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())


def _by_name(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


def _webhook_tasks() -> list[dict]:
    return _load(_GIT_DEPLOY / "tasks" / "webhook.yml")


def test_webhook_pass_reconciles_only_the_receiver_without_orphan_removal():
    task = _by_name(_webhook_tasks(), "Bring the webhook receiver onto the current image")
    assert task["ansible.builtin.include_role"]["name"] == "container_lifecycle"
    assert task["vars"]["container_lifecycle_only"] == ["bay-webhook"]
    # A one-container bundle must never be allowed to remove the rest.
    assert task["vars"]["bay_reconciler_remove_orphans"] is False


def test_reconcile_runs_after_the_image_build_and_is_not_gated_on_changed_files():
    tasks = _webhook_tasks()
    names = [t.get("name") for t in tasks]
    assert names.index("Build webhook receiver image") < names.index(
        "Bring the webhook receiver onto the current image"
    )
    # Self-heal: the reconcile compares image IDs on every run.
    when = str(
        _by_name(tasks, "Bring the webhook receiver onto the current image")["when"]
    )
    assert "_webhook_files" not in when


def test_missing_image_triggers_a_build_even_when_files_are_unchanged():
    tasks = _webhook_tasks()
    build = _by_name(tasks, "Build webhook receiver image")
    assert "_webhook_files.changed" in build["when"]
    assert "_webhook_image_inspect.rc != 0" in build["when"]
    inspect = _by_name(tasks, "Check whether the webhook receiver image exists")
    assert inspect["check_mode"] is False
    assert inspect["changed_when"] is False


def test_fresh_host_without_env_file_skips_instead_of_failing():
    tasks = _webhook_tasks()
    stat = _by_name(
        tasks, "Check that deploy_stack has rendered the webhook receiver env file"
    )
    assert stat["ansible.builtin.stat"]["path"].endswith("/env/bay-webhook.env")
    task = _by_name(tasks, "Bring the webhook receiver onto the current image")
    assert "_webhook_env_stat.stat.exists" in task["when"]


def test_no_second_copy_of_the_receiver_spec():
    # One definition, in build_specs.yml. Nothing in git_deploy may spell out
    # the container's env, volumes, labels or user.
    text = (_GIT_DEPLOY / "tasks" / "webhook.yml").read_text()
    for forbidden in ("community.docker.docker_container", "traefik.http", "volumes:", "network_mode"):
        assert forbidden not in text


def test_partial_mode_does_not_set_container_specs():
    # Setting it would make deploy_stack's later run reuse a one-container list.
    tasks = _load(_LIFECYCLE / "build_specs.yml")
    full = _by_name(tasks, "Assemble container_specs")
    assert "container_specs" in full["ansible.builtin.set_fact"]
    assert "container_lifecycle_only | default([]) | length == 0" in full["when"]
    partial = _by_name(tasks, "Assemble the partial container spec list")
    assert "container_specs" not in partial["ansible.builtin.set_fact"]
    assert "selectattr('name', 'in', container_lifecycle_only)" in str(
        partial["ansible.builtin.set_fact"]
    )


def test_main_builds_specs_in_partial_mode_and_reconciles_the_selection():
    tasks = _load(_LIFECYCLE / "main.yml")
    build = _by_name(tasks, "Build container specs from services.yml")
    assert "container_lifecycle_only" in build["when"]
    reconcile = _by_name(tasks, "Reconcile via server-side reconciler")
    assert "_container_lifecycle_partial_specs" in reconcile["vars"]["_reconcile_specs"]


def test_reconcile_resets_entries_so_a_second_pass_does_not_accumulate():
    tasks = _load(_LIFECYCLE / "reconcile.yml")
    assert tasks[0]["name"] == "Reset the reconcile bundle entries"
    assert tasks[0]["ansible.builtin.set_fact"] == {"_reconcile_entries": []}


def test_handler_restarts_tolerates_a_missing_receiver_and_skips_after_reconcile():
    handlers = _load(_GIT_DEPLOY / "handlers" / "main.yml")
    handler = _by_name(handlers, "Restart bay-webhook")
    assert "ansible.builtin.command" in handler
    assert "docker restart bay-webhook" in handler["ansible.builtin.command"]["cmd"]
    assert any("No such container" in str(c) for c in handler["failed_when"])
    assert "_bay_webhook_reconciled" in handler["when"]
