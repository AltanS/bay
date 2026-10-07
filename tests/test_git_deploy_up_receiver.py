"""`bay up` (`--tags deploy_stack`) registers a build app with the receiver and its trigger.

Before 2.2.0 the webhook receiver's config.json, its image and the
`bay-build@<container>.path` units came only from webhook.yml and systemd.yml.
main.yml includes those with no tags, from blocks that need untagged facts, so
a `bay up` gave a new build app a container but no receiver entry and no
trigger, and `bay remove` left the trigger of the removed app running.

2.2.0 moves that work into render_webhook.yml and render_trigger_units.yml,
included like render_image_map.yml with `deploy_stack` in `tags` and
`apply.tags`. These tests read the real role files. The stale-unit cleanup is
run with ansible-playbook on localhost against a stub `systemctl`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_TASKS = _ROOT / "roles" / "git_deploy" / "tasks"
_FILES = ("render_webhook.yml", "render_trigger_units.yml")


def _load(name: str) -> list[dict[str, Any]]:
    return yaml.safe_load((_TASKS / name).read_text())


def _walk(tasks: list[dict[str, Any]]):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key) or [])


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _include_file(task: dict[str, Any]) -> str | None:
    inc = task.get("ansible.builtin.include_tasks")
    if isinstance(inc, dict):
        return inc.get("file")
    return inc if isinstance(inc, str) else None


def _template_tasks(tasks, src: str) -> list[dict[str, Any]]:
    return [t for t in _walk(tasks) if (t.get("ansible.builtin.template") or {}).get("src") == src]


def _when(task: dict[str, Any]) -> list[str]:
    when = task.get("when") or []
    return [str(w).strip() for w in ([when] if isinstance(when, str) else when)]


def test_up_renders_receiver_config_and_trigger_unit() -> None:
    main = _load("main.yml")
    files = [_include_file(t) for t in main]

    # Both files are included at the top level, right after rebuild.sh, with
    # deploy_stack in the include tags AND in apply.tags.
    i = files.index("render_rebuild_script.yml")
    assert files[i + 1 : i + 3] == list(_FILES)
    for name in _FILES:
        task = main[files.index(name)]
        assert "deploy_stack" in _tags(task.get("tags")), name
        apply = task["ansible.builtin.include_tasks"]["apply"]
        assert "deploy_stack" in _tags(apply.get("tags")), name
        assert sorted(_tags(task.get("tags"))) == sorted(_tags(apply.get("tags")))

    webhook = _load("render_webhook.yml")
    units = _load("render_trigger_units.yml")

    # config.json: rendered here, and a change restarts the receiver (it reads
    # the file once, at start).
    (config,) = _template_tasks(webhook, "webhook-config.json.j2")
    assert config["ansible.builtin.template"]["dest"].endswith("/webhook/config.json")
    assert config.get("notify") == "Restart bay-webhook" or "Restart bay-webhook" in _tags(
        config.get("notify")
    )

    # The receiver image is built here.
    builds = [
        t for t in _walk(webhook)
        if "docker build -t bay-webhook" in str((t.get("ansible.builtin.command") or {}).get("cmd", ""))
    ]
    assert len(builds) == 1
    assert any("is not skipped" in w for w in _when(builds[0])), "check-mode guard"

    # The path unit enable for every rebuild service is here.
    enables = [
        t for t in _walk(units)
        if (t.get("ansible.builtin.systemd") or {}).get("name") == "bay-build@{{ item }}.path"
    ]
    assert len(enables) == 1
    assert enables[0]["ansible.builtin.systemd"]["enabled"] is True
    assert enables[0]["ansible.builtin.systemd"]["state"] == "started"
    assert enables[0]["loop"] == "{{ git_deploy_rebuild_services }}"
    for src in ("bay-build@.path.j2", "bay-build@.service.j2", "bay-build-alert@.service.j2",
                "select-builder.sh.j2", "build-alert.sh.j2"):
        assert len(_template_tasks(units, src)) == 1, src

    # Moved, not copied: the full-deploy files no longer render any of it.
    old_webhook = _load("webhook.yml")
    old_systemd = _load("systemd.yml")
    assert not _template_tasks(old_webhook, "webhook-config.json.j2")
    assert "docker build" not in (_TASKS / "webhook.yml").read_text()
    assert "hmac.key" not in (_TASKS / "webhook.yml").read_text()
    for src in ("bay-build@.path.j2", "bay-build@.service.j2", "bay-build-alert@.service.j2",
                "select-builder.sh.j2", "build-alert.sh.j2"):
        assert not _template_tasks(old_systemd, src), src
    assert "bay-build@{{ item }}.path" not in (_TASKS / "systemd.yml").read_text()
    # The receiver-only reconcile stays on the full-deploy path.
    assert any(
        (t.get("ansible.builtin.include_role") or {}).get("name") == "container_lifecycle"
        for t in _walk(old_webhook)
    )

    # Both render under the same `when` as the image-map render.
    (image_map_block,) = [t for t in _load("render_image_map.yml") if t.get("name") == "Render image-map.json"]
    for tasks in (webhook, units):
        blocks = [t for t in tasks if "block" in t and str(t.get("name", "")).startswith("Render the")]
        assert len(blocks) == 1
        assert _when(blocks[0]) == _when(image_map_block)

    # git_deploy runs before deploy_stack in the play, so the config, the
    # image and the units exist before the container pass.
    play = yaml.safe_load((_ROOT / "deploy.yml").read_text())
    roles = [r["role"] for p in play for r in p.get("roles", []) if isinstance(r, dict)]
    assert roles.index("git_deploy") < roles.index("deploy_stack")


# ── Stale trigger units ─────────────────────────────────────────────────

_STUB = """#!/bin/bash
echo "$*" >> "$BAY_TEST_SYSTEMCTL_LOG"
case "$1" in
  list-units) cat "$BAY_TEST_SYSTEMCTL_UNITS" ;;
  show) printf 'Id=%s\\nLoadState=loaded\\nActiveState=active\\nSubState=waiting\\nUnitFileState=enabled\\n' "$2" ;;
  is-enabled) echo enabled ;;
esac
exit 0
"""

# --plain --no-legend output: UNIT LOAD ACTIVE SUB DESCRIPTION. `ghost` is
# not loaded (its line must not be taken: systemd cannot stop it).
_UNITS = """bay-build@keep.path loaded active waiting Watch keep
bay-build@gone.path loaded active waiting Watch gone
bay-build@also-gone.path loaded inactive dead Watch also-gone
bay-build@ghost.path not-found inactive dead bay-build@ghost.path
"""


_TEMPLATE = """# managed
[Unit]
Description=Watch for build trigger - %i

[Path]
PathExists={stack}/triggers/%i.trigger

[Install]
WantedBy=multi-user.target
"""


def _run_stale(
    tmp_path: Path, *, check: bool, installed_stack: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    unit_dir = tmp_path / "units-dir"
    unit_dir.mkdir()
    if installed_stack is not None:
        (unit_dir / "bay-build@.path").write_text(_TEMPLATE.format(stack=installed_stack))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemctl"
    stub.write_text(_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    units = tmp_path / "units.txt"
    units.write_text(_UNITS)
    log = tmp_path / "systemctl.log"
    log.write_text("")
    # The render block is off (no build service on this box), the stale
    # cleanup is on (webhook defined). The facts are set, so the guarded
    # render_image_map.yml include is skipped too.
    extra = {
        "webhook": {"secret": "unused"},
        "git_deploy_rebuild_services": ["keep"],
        "_all_build_services": [],
        "_is_build_server": False,
        "_global_remote_build_services": [],
        "stack_dir": str(tmp_path / "stack"),
        "git_deploy_systemd_unit_dir": str(unit_dir),
    }
    (tmp_path / "vars.json").write_text(json.dumps(extra))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    (tmp_path / "play.yml").write_text(
        "- hosts: localhost\n"
        "  connection: local\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        f"    - ansible.builtin.include_tasks: {_TASKS / 'render_trigger_units.yml'}\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "BAY_TEST_SYSTEMCTL_LOG": str(log),
        "BAY_TEST_SYSTEMCTL_UNITS": str(units),
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    cmd = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")]
    if check:
        cmd.append("--check")
    proc = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    return proc, log.read_text().splitlines()


def test_up_stops_trigger_unit_of_removed_service(tmp_path: Path) -> None:
    proc, calls = _run_stale(tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]

    listing = [c for c in calls if c.startswith("list-units")]
    assert listing and "--all" in listing[0] and "bay-build@*.path" in listing[0]

    acted = [c for c in calls if c.split()[0] in ("stop", "disable")]
    assert sorted(acted) == sorted([
        "stop bay-build@gone.path",
        "disable bay-build@gone.path",
        "stop bay-build@also-gone.path",
        "disable bay-build@also-gone.path",
    ]), acted
    # The unit of a container still on the box is never touched, and a unit
    # systemd has not loaded is not taken.
    assert not any("keep" in c for c in acted)
    assert not any("ghost" in c for c in calls if not c.startswith("list-units"))


def test_shared_build_box_guard_stops_when_another_stack_owns_the_triggers(tmp_path: Path) -> None:
    other = "/srv/other-fleet/stack"
    proc, calls = _run_stale(tmp_path, check=False, installed_stack=other)
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "The build triggers on this box belong to the stack at" in out
    assert other in out
    assert "Another fleet owns this box's builds" in out
    # Nothing was stopped or disabled, and the template was not rewritten.
    assert not [c for c in calls if c.split()[0] in ("stop", "disable")]
    assert not any(c.startswith("list-units") for c in calls)
    installed = (tmp_path / "units-dir" / "bay-build@.path").read_text()
    assert installed == _TEMPLATE.format(stack=other)


def test_shared_build_box_guard_stops_in_check_mode_too(tmp_path: Path) -> None:
    proc, calls = _run_stale(tmp_path, check=True, installed_stack="/srv/other-fleet/stack")
    assert proc.returncode != 0
    assert "Another fleet owns this box's builds" in proc.stdout + proc.stderr
    assert not [c for c in calls if c.split()[0] in ("stop", "disable")]


def test_shared_build_box_guard_passes_for_the_same_stack(tmp_path: Path) -> None:
    proc, calls = _run_stale(tmp_path, check=False, installed_stack=str(tmp_path / "stack"))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    acted = [c for c in calls if c.split()[0] in ("stop", "disable")]
    assert "stop bay-build@gone.path" in acted


def test_shared_build_box_guard_precedes_every_write() -> None:
    tasks = _load("render_trigger_units.yml")
    names = [str(t.get("name", "")) for t in tasks]
    guard = names.index("Guard against a build box that another fleet owns")
    render = names.index("Render the build trigger units (runs under deploy_stack too)")
    cleanup = names.index("Stop the trigger units of containers that left the box")
    assert guard < render < cleanup
    assert "webhook is defined" in _when(tasks[guard])
    # Only this file writes the system-wide template, so the guard covers every writer.
    writers = [
        p.name for p in _TASKS.glob("*.yml")
        if _template_tasks(_load(p.name), "bay-build@.path.j2")
    ]
    assert writers == ["render_trigger_units.yml"]


def test_stale_trigger_cleanup_changes_nothing_in_check_mode(tmp_path: Path) -> None:
    """`bay plan --remote` runs this in check mode: it may list, never stop."""
    proc, calls = _run_stale(tmp_path, check=True)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert any(c.startswith("list-units") for c in calls), "the listing must run in check mode"
    assert not [c for c in calls if c.split()[0] in ("stop", "disable")]
    # ...but the plan still predicts the stop.
    assert "bay-build@gone.path" in proc.stdout


def test_stale_trigger_tasks_guard_registered_results() -> None:
    tasks = list(_walk(_load("render_trigger_units.yml")))
    (listing,) = [t for t in tasks if "list-units" in str((t.get("ansible.builtin.command") or {}).get("cmd", ""))]
    assert listing["check_mode"] is False
    assert listing["changed_when"] is False
    register = listing["register"]
    readers = [t for t in tasks if "block" not in t and register in json.dumps(t) and t is not listing]
    assert readers
    for task in readers:
        assert any(f"{register} is not skipped" in w for w in _when(task)), task.get("name")
        assert not any("rc is defined" in w for w in _when(task)), task.get("name")
