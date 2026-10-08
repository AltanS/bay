"""Every task that creates `{{ stack_dir }}/env` sets the same owner, group and mode.

Gap 33 of M118: deploy_stack and traefik set 0750 and zot set 0755 on the same
directory, so on a box that runs all three the mode flipped on every deploy and
two tasks reported changed. One value for all, found by parsing the roles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENV_DIR = "{{ stack_dir }}/env"


def _walk(tasks: Any) -> Iterator[dict[str, Any]]:
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _env_dir_tasks() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "roles").glob("*/tasks/**/*.yml")):
        tasks = yaml.safe_load(path.read_text())
        if not isinstance(tasks, list):
            continue
        for task in _walk(tasks):
            args = task.get("ansible.builtin.file")
            if isinstance(args, dict) and args.get("path") == ENV_DIR:
                found[f"{path.relative_to(ROOT)}::{task['name']}"] = args
    return found


def test_env_dir_mode_agrees() -> None:
    found = _env_dir_tasks()
    roles = {name.split("/")[1] for name in found}
    assert {"deploy_stack", "traefik", "zot"} <= roles, sorted(found)
    shapes = {
        name: (args.get("state"), args.get("owner"), args.get("group"), args.get("mode"))
        for name, args in found.items()
    }
    assert len(set(shapes.values())) == 1, shapes
    state, owner, group, mode = next(iter(shapes.values()))
    assert (state, owner, group, mode) == ("directory", "{{ app_user }}", "docker", "0750")
