"""Guards the pruning of custom CrowdSec scenarios that left the list.

`crowdsec_custom_scenarios` renders one file per entry to
/etc/crowdsec/scenarios/<name>.yaml. Removing or renaming an entry used to
leave the old file on the host, where it kept banning: One renamed scenario
survived three times on a production consumer and an operator deleted the stale
file by hand each time.

The prune has two hard constraints, and both are asserted here:

1. It must only ever touch REGULAR files carrying this role's own template
   header. Hub content sits at the same path as a symlink into
   /etc/crowdsec/hub, and deleting one of those takes a real scenario down.
2. It must run when the list is EMPTY. Gating it on
   `crowdsec_custom_scenarios | length > 0` would mean emptying the list prunes
   nothing, which is exactly the case an operator hits after a cleanup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CROWDSEC_TASKS = (
    Path(__file__).resolve().parent.parent / "roles" / "crowdsec" / "tasks" / "main.yml"
)

FIND_TASK = "Find rendered custom scenario files"
REMOVE_TASK = "Remove custom scenarios that left the list"
FLUSH_TASK = "Flush decisions of removed custom scenarios"


@pytest.fixture(scope="module")
def tasks() -> list[dict]:
    return yaml.safe_load(_CROWDSEC_TASKS.read_text())


def _task(tasks: list[dict], name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_CROWDSEC_TASKS}")


def _when(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


def _index(tasks: list[dict], name: str) -> int:
    for i, task in enumerate(tasks):
        if task.get("name") == name:
            return i
    raise AssertionError(f"task {name!r} not found in {_CROWDSEC_TASKS}")


def test_find_only_matches_regular_files_with_the_template_marker(
    tasks: list[dict],
) -> None:
    find = _task(tasks, FIND_TASK)["ansible.builtin.find"]
    # A symlink is the hub's scenario, never ours.
    assert find["file_type"] == "file"
    # Only files this role's custom-scenario template rendered are candidates.
    assert find["contains"] == "^# Custom CrowdSec Scenario"
    assert find["paths"] == "/etc/crowdsec/scenarios"


def test_find_marker_matches_only_the_custom_scenario_template(tasks: list[dict]) -> None:
    """The marker must not match the built-in scenario templates, which live in
    the same directory and are also rendered by this role."""
    import re

    marker = _task(tasks, FIND_TASK)["ansible.builtin.find"]["contains"]
    pattern = re.compile(marker)
    template_dir = (
        _CROWDSEC_TASKS.parent.parent / "templates" / "scenarios"
    )
    matched = {
        path.name
        for path in sorted(template_dir.glob("*.yaml.j2"))
        if any(pattern.search(line) for line in path.read_text().splitlines())
    }
    assert matched == {"custom-scenario.yaml.j2"}


def test_remove_task_deletes_and_reloads(tasks: list[dict]) -> None:
    remove = _task(tasks, REMOVE_TASK)
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert remove["notify"] == "Reload crowdsec"
    assert remove["loop_control"]["label"] == "{{ item }}.yaml"


def test_remove_task_runs_when_the_list_is_empty(tasks: list[dict]) -> None:
    """Emptying `crowdsec_custom_scenarios` must prune everything the template
    rendered, so the only guard allowed here is `crowdsec_enabled`."""
    conditions = _when(_task(tasks, REMOVE_TASK))
    assert conditions == ["crowdsec_enabled | bool"]


def test_flush_task_targets_the_pruned_scenario_names(tasks: list[dict]) -> None:
    flush = _task(tasks, FLUSH_TASK)
    cmd = flush["ansible.builtin.command"]["cmd"]
    assert "cscli decisions delete" in cmd
    assert "--scenario" in cmd
    assert "custom/{{ item }}" in cmd
    # cscli exits non-zero with nothing to delete on some versions.
    assert flush["failed_when"] is False
    assert "changed_when" in flush


def test_prune_runs_before_validation_and_hub_update(tasks: list[dict]) -> None:
    """An invalid or stale file must be gone before `cscli hub update` parses
    the directory."""
    assert _index(tasks, REMOVE_TASK) < _index(tasks, "List rendered scenario files")
    assert _index(tasks, REMOVE_TASK) < _index(tasks, "Update CrowdSec hub")
    assert _index(tasks, "Deploy custom CrowdSec scenarios") < _index(tasks, FIND_TASK)
