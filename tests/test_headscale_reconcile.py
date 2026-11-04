"""`bay deploy --tags headscale` must move the headscale container onto the pin.

Before 2.8.0 the headscale role rendered config.yaml and restarted the
container, but a restart keeps the image the container was created from. A
`headscale_version` bump reached the host only through a full `deploy_stack`
run, which also reconciles Traefik and every other container on the box.

The role now runs the container_lifecycle reconciler for the headscale
container alone, like git_deploy does for the webhook receiver. These tests pin
that wiring; the behaviour itself needs a real host.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).resolve().parent.parent / "roles" / "headscale"


def _tasks() -> list[dict]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())


def _by_name(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


def test_reconciles_only_headscale_without_orphan_removal():
    task = _by_name(_tasks(), "Bring headscale onto the pinned image")
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "container_lifecycle"
    # include_role does not pass its tags to the role's tasks without `apply`;
    # without it `--tags headscale` would run the include and skip the work.
    assert "headscale" in include["apply"]["tags"]
    assert "headscale" in task["tags"]
    assert task["vars"]["container_lifecycle_only"] == ["headscale"]
    # A one-container bundle must never be allowed to remove the rest.
    assert task["vars"]["bay_reconciler_remove_orphans"] is False


def test_reconcile_runs_after_the_config_is_rendered():
    names = [t.get("name") for t in _tasks()]
    reconcile = names.index("Bring headscale onto the pinned image")
    assert names.index("Template headscale config") < reconcile
    assert names.index("Template split-DNS extra records") < reconcile
    assert names.index("Install validated headscale ACL policy") < reconcile


def test_a_host_without_the_container_is_left_to_deploy_stack():
    tasks = _tasks()
    probe = _by_name(tasks, "Check whether the headscale container exists")
    # Read-only probe: it must also run in --check, or the dry run skips the plan.
    assert probe["check_mode"] is False
    assert probe["changed_when"] is False
    when = str(_by_name(tasks, "Bring headscale onto the pinned image")["when"])
    assert "_headscale_container_exists" in when


def test_no_second_copy_of_the_headscale_spec():
    # One definition, in container_lifecycle/tasks/build_specs.yml.
    text = (_ROLE / "tasks" / "main.yml").read_text()
    for forbidden in ("headscale/headscale:", "traefik.http", "3478:3478"):
        assert forbidden not in text


def test_handler_skips_the_restart_after_a_recreate():
    handlers = yaml.safe_load((_ROLE / "handlers" / "main.yml").read_text())
    restart = _by_name(handlers, "Restart headscale")
    assert "_headscale_reconciled" in str(restart["when"])
    record = _by_name(_tasks(), "Record whether the reconciler changed headscale")
    assert "_headscale_reconciled" in record["ansible.builtin.set_fact"]
