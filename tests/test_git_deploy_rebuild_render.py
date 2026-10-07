"""`bay up` runs `--tags deploy_stack` only, yet rebuild.sh must follow the deploy.

rebuild.sh holds a frozen copy of each container's labels, ports and mounts.
The git_deploy role is tagged [build, git_deploy], so the render of rebuild.sh
is tagged `deploy_stack` on its own, as image-map.json already is (issue #13).
Nothing that clones, builds or pulls app code may carry that tag, or a `bay up`
would do work that `bay plan` (same tag) never predicted. The one build under
the tag is the webhook receiver image (render_webhook.yml, 2.2.0, see
tests/test_git_deploy_up_receiver.py): a box's first build app needs it before
the container pass creates `bay-webhook`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_TASKS = Path(__file__).resolve().parent.parent / "roles" / "git_deploy" / "tasks"

#: Task files that fetch, build or pull code, or set up the builders.
_HEAVY = (
    "clone_repos.yml",
    "build.yml",
    "remote_build.yml",
    "setup_builder.yml",
    "deploy_keys.yml",
    "remote_builder.yml",
)


def _load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((_TASKS / name).read_text())


def _walk(tasks: list[dict[str, Any]]):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key) or [])


def _tags(task: dict[str, Any]) -> list[str]:
    tags = task.get("tags") or []
    return [tags] if isinstance(tags, str) else list(tags)


def _include_file(task: dict[str, Any]) -> str | None:
    inc = task.get("ansible.builtin.include_tasks")
    if isinstance(inc, dict):
        return inc.get("file")
    return inc if isinstance(inc, str) else None


def test_only_the_render_includes_carry_deploy_stack() -> None:
    tagged = [t for t in _walk(_load("main.yml")) if "deploy_stack" in _tags(t)]
    assert [_include_file(t) for t in tagged] == [
        "render_image_map.yml",
        "render_rebuild_script.yml",
        "render_webhook.yml",
        "render_trigger_units.yml",
    ]


def test_the_rebuild_include_follows_the_image_map_include_and_applies_the_tags() -> None:
    top = _load("main.yml")
    files = [_include_file(t) for t in top]
    i = files.index("render_image_map.yml")
    assert files[i + 1] == "render_rebuild_script.yml", "it reads the facts the image-map file sets"
    task = top[i + 1]
    want = ["deploy_stack", "build", "git_deploy"]
    assert sorted(_tags(task)) == sorted(want)
    assert sorted(task["ansible.builtin.include_tasks"]["apply"]["tags"]) == sorted(want)


def test_nothing_that_builds_or_pulls_is_tagged_deploy_stack() -> None:
    for name in ("main.yml", "render_rebuild_script.yml", *_HEAVY):
        for task in _walk(_load(name)):
            if _include_file(task) in _HEAVY or name in _HEAVY:
                assert "deploy_stack" not in _tags(task), (name, task.get("name"))
    inner = _load("render_rebuild_script.yml")
    assert not any(_include_file(t) in _HEAVY for t in _walk(inner))


def test_the_rebuild_render_matches_the_one_in_systemd_yml() -> None:
    def template(name: str, task_name: str) -> dict[str, Any]:
        (task,) = [t for t in _walk(_load(name)) if t.get("name") == task_name]
        return task["ansible.builtin.template"]

    a = template("render_rebuild_script.yml", "Deploy rebuild script")
    b = template("systemd.yml", "Deploy rebuild script")
    assert a == b
    assert a["src"] == "rebuild.sh.j2"


def test_the_rebuild_render_needs_no_fact_the_image_map_file_does_not_set() -> None:
    """rebuild.sh.j2 reads these set_fact values; with deploy_stack only, main.yml's set them not."""
    set_by_image_map = {
        k
        for t in _walk(_load("render_image_map.yml"))
        for k in (t.get("ansible.builtin.set_fact") or {})
    }
    assert {"git_deploy_services", "git_deploy_rebuild_services", "_is_build_server"} <= set_by_image_map
    template = (_TASKS.parent / "templates" / "rebuild.sh.j2").read_text()
    for fact in ("git_deploy_services", "git_deploy_rebuild_services"):
        assert fact in template
