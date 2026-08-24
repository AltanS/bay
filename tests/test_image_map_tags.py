"""Tests for M90 / GH-13: image-map.json render task tags and webhook handler.

These tests are static structural assertions over the git_deploy role files.
They lock the wiring that fixes the stale cross-region image map:

1. image-map.json must be re-rendered under the deploy_stack tag (not just
   build/git_deploy). Background: `bin/bay deploy --tags deploy_stack` is the
   common service-config deploy and previously left image-map.json stale,
   silently dropping new services from the post-build pull-signal fan-out.
   See spec 02 — the producer/pull-signal fanout spec.

2. The image-map.json template task must notify a handler that restarts the
   bay-webhook container. Background: the receiver loads /config/image-map.json
   only at process start (files/webhook/app.py:_load_image_map), so a fresh
   render on disk has no effect until the container restarts. See spec 03 —
   the producer/pull-signal fanout spec.

These are the regression guards required by M90 spec 05.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROLE_DIR = (
    Path(__file__).resolve().parent.parent / "roles" / "git_deploy"
)


def _load_yaml(path: Path):
    with path.open() as f:
        return yaml.safe_load(f)


# ── Test 2 — image-map render task tag invariant ─────────────────────


def test_image_map_render_runs_under_deploy_stack_M90_GH_13():
    """M90 / GH-13: the image-map.json render must execute under
    [deploy_stack, build, git_deploy].

    Locking this prevents the spec 02 fix from being silently reverted.
    If a future refactor drops the deploy_stack tag from the include,
    image-map.json regenerates only under --tags build|git_deploy, and
    routine deploy_stack-only deploys re-introduce the stale-map bug.

    """
    main_yml = _load_yaml(ROLE_DIR / "tasks" / "main.yml")

    # Locate the include_tasks task that pulls in render_image_map.yml.
    include = None
    for task in main_yml:
        include_block = task.get("include_tasks") or task.get(
            "ansible.builtin.include_tasks"
        )
        if not include_block:
            continue
        # include_tasks accepts a string OR a dict with `file:`
        included_file = (
            include_block
            if isinstance(include_block, str)
            else include_block.get("file", "")
        )
        if "render_image_map" in included_file:
            include = task
            break

    assert include is not None, (
        "GH-13: tasks/main.yml must include render_image_map.yml. "
        "If the include was renamed or removed, image-map.json "
        "regeneration under --tags deploy_stack is broken."
    )

    # Tags can be on the task itself OR inside apply: for include_tasks.
    include_block = include.get("include_tasks") or include.get(
        "ansible.builtin.include_tasks"
    )
    apply_tags = []
    if isinstance(include_block, dict):
        apply_tags = include_block.get("apply", {}).get("tags", []) or []
    task_tags = include.get("tags", []) or []
    effective_tags = set(apply_tags) | set(task_tags)

    required = {"deploy_stack", "build", "git_deploy"}
    missing = required - effective_tags
    assert not missing, (
        f"GH-13: render_image_map include must run under tags "
        f"{sorted(required)}, but missing: {sorted(missing)}. "
        f"Currently tagged: {sorted(effective_tags)}. The deploy_stack "
        f"tag in particular is the M90-S2 fix — dropping it re-introduces "
        f"the stale image-map bug from the M87 cutover."
    )


def test_image_map_render_task_file_exists_M90_GH_13():
    """The included render_image_map.yml file must exist alongside main.yml.

    Sanity check: the include in main.yml points to render_image_map.yml,
    and that file must actually be there for the include to do anything.
    """
    render_file = ROLE_DIR / "tasks" / "render_image_map.yml"
    assert render_file.is_file(), (
        f"GH-13: {render_file} is missing. The M90-S2 fix extracts the "
        f"image-map render into this file so it can be invoked under "
        f"--tags deploy_stack without dragging in the rest of git_deploy."
    )


# ── Test 3 — webhook reload handler exists and is wired up ───────────


def test_webhook_restart_handler_exists_M90_GH_13():
    """M90 / GH-13: git_deploy must define a handler that restarts the
    bay-webhook container.

    Background: the receiver caches image-map.json in process memory
    (_load_image_map runs once in main()). Without a restart handler,
    template-rendering a new map on disk is invisible to the running
    container. The receiver-cache half of the regression.

    """
    handlers = _load_yaml(ROLE_DIR / "handlers" / "main.yml")
    assert isinstance(handlers, list), (
        "git_deploy/handlers/main.yml should be a list of handler tasks"
    )

    webhook_handlers = [
        h for h in handlers
        if "webhook" in h.get("name", "").lower()
    ]
    assert webhook_handlers, (
        f"GH-13: no webhook-related handler found in "
        f"git_deploy/handlers/main.yml. Existing handlers: "
        f"{[h.get('name') for h in handlers]}. A handler that restarts "
        f"the bay-webhook container is required so spec-02's fresh "
        f"image-map.json render actually takes effect."
    )

    # The handler must actually restart the bay-webhook container,
    # not just any container.
    matched = False
    for handler in webhook_handlers:
        container_block = handler.get("community.docker.docker_container", {})
        if container_block.get("name") == "bay-webhook":
            assert container_block.get("restart") is True, (
                f"GH-13: handler '{handler['name']}' targets bay-webhook "
                f"but does not set restart: true. A no-op handler defeats "
                f"the purpose."
            )
            matched = True
            break
    assert matched, (
        f"GH-13: no handler restarts the bay-webhook container by name. "
        f"Webhook handlers found: "
        f"{[h.get('name') for h in webhook_handlers]}"
    )


def test_image_map_template_notifies_webhook_restart_M90_GH_13():
    """M90 / GH-13: the image-map.json template task must notify the
    webhook restart handler.

    Without the notify, the handler exists but never fires — image-map.json
    silently changes on disk and the receiver keeps serving stale data.
    """
    render_tasks = _load_yaml(ROLE_DIR / "tasks" / "render_image_map.yml")
    assert isinstance(render_tasks, list), (
        "render_image_map.yml should be a list of tasks"
    )

    # Find the template task that writes image-map.json. It may live at
    # the top level OR nested inside a `block:`.
    def _iter_tasks(tasks):
        for task in tasks:
            yield task
            if "block" in task:
                yield from _iter_tasks(task["block"])

    template_task = None
    for task in _iter_tasks(render_tasks):
        tmpl = task.get("template") or task.get("ansible.builtin.template")
        if tmpl and tmpl.get("src") == "image-map.json.j2":
            template_task = task
            break

    assert template_task is not None, (
        "GH-13: no template task in render_image_map.yml renders "
        "image-map.json.j2. The M90-S2 file is broken."
    )

    notify = template_task.get("notify")
    assert notify, (
        f"GH-13: the image-map.json template task is missing a notify: "
        f"directive. Without it, the spec-03 handler never fires and the "
        f"receiver keeps loading the stale in-memory map. Task: "
        f"{template_task.get('name')!r}"
    )

    # Allow either a single string or a list — both are valid Ansible.
    notified = [notify] if isinstance(notify, str) else list(notify)
    assert any("webhook" in n.lower() for n in notified), (
        f"GH-13: the image-map.json template task notifies "
        f"{notified}, but none target the webhook restart handler. "
        f"Expected something like 'Restart bay-webhook'."
    )
