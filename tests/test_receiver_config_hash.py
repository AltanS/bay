"""The receiver's rendered config is part of its container spec.

The receiver reads `config.json` and `image-map.json` once, at start. Before
2.5.2 a change to either (a `watch` edit, a new build app) restarted
`bay-webhook` through an Ansible handler: the plan did not predict it and the
receipt did not list it. The `bay-webhook` spec now carries the label
`com.bay.receiver-config-hash`: a sha256 of the two git_deploy templates,
rendered on the control node with the facts the render tasks use. A changed
render changes the spec, so the reconciler recreates the container, in check
mode too.

These tests run the real `build_specs.yml` under `ansible-playbook --check`
and read the label off the real spec. Nothing in the render is mocked.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GIT_DEPLOY = ROOT / "roles" / "git_deploy" / "tasks"
BUILD_SPECS = ROOT / "roles" / "container_lifecycle" / "tasks" / "build_specs.yml"
LABEL = "com.bay.receiver-config-hash"


def _svc(repo: str, **build: Any) -> dict[str, Any]:
    return {
        "domains": [f"{repo}.example.com"],
        "ports": {"internal": 3000},
        "build": {"repo": f"https://example.com/acme/{repo}.git", **build},
    }


BASE: dict[str, Any] = {
    "shop": _svc("shop", paths={"include": ["src/**"]}),
    "blog": _svc("blog"),
}

# What git_deploy main.yml sets, in its order, on the full-deploy path. They
# are read from the file by name, so the test cannot drift from the role.
_FULL_PATH_TASKS = (
    "Resolve all build services",
    "Resolve all build services globally (for build server webhook)",
    "Resolve global remote build services",
    "Check if this host is the build server",
    "Resolve build strategies",
    "Set git_deploy_services to local builds",
    "Set git_deploy_rebuild_services (deployment server)",
    "Set git_deploy_rebuild_services for build server",
)


def _walk(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks or []:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_walk(task.get(key) or []))
    return out


_FACT_NAMES = (
    "git_deploy_rebuild_services",
    "git_deploy_peer_webhook_urls",
    "git_deploy_build_strategy",
)


def _sets_build_fact(task: dict[str, Any]) -> bool:
    fact = task.get("ansible.builtin.set_fact")
    return isinstance(fact, dict) and any(name in fact for name in _FACT_NAMES)


def test_full_path_fact_list_is_complete() -> None:
    """A new main.yml task that sets a fact the templates read must join the copied list."""
    tasks = _walk(yaml.safe_load((GIT_DEPLOY / "main.yml").read_text()))
    setters = {t["name"] for t in tasks if _sets_build_fact(t)}
    copied = {n for n in _FULL_PATH_TASKS if n in setters}
    assert setters == copied, f"add to _FULL_PATH_TASKS: {sorted(setters - copied)}"
    # Both build-server and deployment-server setters are covered.
    assert {n for n in setters if "rebuild_services" in n} >= {
        "Set git_deploy_rebuild_services (deployment server)",
        "Set git_deploy_rebuild_services for build server",
    }


def _full_path_facts() -> list[dict[str, Any]]:
    by_name = {t.get("name"): t for t in _walk(yaml.safe_load((GIT_DEPLOY / "main.yml").read_text()))}
    picked = []
    for name in _FULL_PATH_TASKS:
        task = copy.deepcopy(by_name[name])
        task.pop("when", None)
        if name.endswith("for build server"):
            # main.yml guards the build-server block with this condition.
            task["when"] = "_is_build_server | bool and _global_remote_build_services | length > 0"
        elif name.endswith("(deployment server)"):
            task["when"] = "not (_is_build_server | bool and _global_remote_build_services | length > 0)"
        task.pop("tags", None)
        if "ansible.builtin.include_tasks" in task:
            del task["ansible.builtin.include_tasks"]
            task["ansible.builtin.include_role"] = {
                "name": "git_deploy", "tasks_from": "resolve_strategy",
            }
        picked.append(task)
    return picked


def _play(path: str, out: Path) -> str:
    facts: list[dict[str, Any]]
    if path == "deploy_stack":
        # `bay up`: git_deploy's render_image_map.yml (main.yml, tagged deploy_stack).
        facts = [{
            "name": "Facts as the deploy_stack path leaves them",
            "ansible.builtin.include_role": {"name": "git_deploy", "tasks_from": "render_image_map"},
        }]
    elif path == "full":
        # `bay deploy`: render_image_map.yml first (main.yml top), then main.yml's own facts.
        facts = [{
            "name": "Facts as the full path leaves them (image map pass)",
            "ansible.builtin.include_role": {"name": "git_deploy", "tasks_from": "render_image_map"},
        }, *_full_path_facts()]
    else:
        raise AssertionError(path)
    tasks = [
        *facts,
        {
            "name": "Build the specs",
            "ansible.builtin.include_role": {"name": "container_lifecycle", "tasks_from": "build_specs"},
        },
        {
            "name": "Keep the receiver spec",
            "ansible.builtin.copy": {
                "content": "{{ _infra_specs | selectattr('name', 'equalto', 'bay-webhook') | first | to_json }}",
                "dest": str(out),
                "mode": "0600",
            },
            "check_mode": False,
        },
    ]
    return yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": tasks,
    }])


def _spec(tmp_path: Path, services: dict[str, Any], *, path: str = "deploy_stack",
          run: str = "run", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    work = tmp_path / run
    work.mkdir(parents=True)
    out = work / "spec.json"
    play = work / "play.yml"
    play.write_text(_play(path, out))
    variables = {
        "stack_dir": str(work / "stack"),
        "stack_name": "testfleet",
        "bay_alert_host": "box-1",
        "webhook": {"domain": "hooks.example.com", "secret": "placeholder"},
        "services": services,
        "active_services": services,
        "inventory_hostname": "localhost",
        **(extra or {}),
    }
    (work / "vars.json").write_text(json.dumps(variables))
    (work / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_FILTER_PLUGINS": str(ROOT / "filter_plugins"),
        "ANSIBLE_LOCAL_TEMP": str(work / "tmp"),
        "ANSIBLE_REMOTE_TEMP": str(work / "tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    }
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{work / 'vars.json'}", str(play), "--check"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    return json.loads(out.read_text())


def _label(*args: Any, **kwargs: Any) -> str:
    return _spec(*args, **kwargs)["labels"][LABEL]


def test_receiver_config_hash_changes_with_watch(tmp_path: Path) -> None:
    first = _label(tmp_path, BASE, run="a")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)

    # The same input gives the same value.
    assert _label(tmp_path, copy.deepcopy(BASE), run="b") == first

    # A different `watch` (compiled to build.paths) changes it.
    watched = copy.deepcopy(BASE)
    watched["shop"]["build"]["paths"] = {"include": ["src/**", "package.json"]}
    changed = _label(tmp_path, watched, run="c")
    assert changed != first

    # So does a new build service, a new branch and a new strategy.
    more = copy.deepcopy(BASE)
    more["docs"] = _svc("docs")
    assert _label(tmp_path, more, run="d") not in (first, changed)
    branch = copy.deepcopy(BASE)
    branch["blog"]["build"]["branch"] = "release"
    assert _label(tmp_path, branch, run="e") != first

    # A change the receiver never reads is no recreate: no spurious plan step.
    moved = copy.deepcopy(BASE)
    moved["shop"]["domains"] = ["other.example.com"]
    assert _label(tmp_path, moved, run="f") == first

    # The label is on the spec next to the code hash, which is a different one.
    spec = _spec(tmp_path, BASE, run="g")
    assert spec["labels"]["com.bay.receiver-hash"] != spec["labels"][LABEL]


def test_receiver_config_hash_stable_across_paths(tmp_path: Path) -> None:
    """`bay up` (deploy_stack) and `bay deploy` (full) build the same label.

    The deploy_stack path gets its build facts from render_image_map.yml alone.
    The full path runs the same file and then main.yml's own fact tasks (read
    from the file here). Both reach one `_webhook_receiver_config_hash` in
    build_specs.yml. Mixed strategies make the image map non-trivial.
    """
    services = copy.deepcopy(BASE)
    services["api"] = {**_svc("api", strategy="remote"), "image": "registry.example.com/api:1"}
    services["static"] = {"image": "registry.example.com/static:1", **{
        k: v for k, v in _svc("static").items() if k != "build"}}
    stack = _label(tmp_path, services, path="deploy_stack", run="stack")
    full = _label(tmp_path, services, path="full", run="full")
    assert stack == full
    # With git_deploy_peer_webhook_urls set the value moves, on both paths alike.
    extra = {"git_deploy_peer_webhook_urls": {"eu": "https://hooks.eu.example.com"}}
    # A build server with a remote-strategy service: the build-server branch of
    # both paths (main.yml sets the list for it, render_image_map.yml mirrors it).
    # The remote service is not active on this host, so only the build server
    # branch puts it in the image map.
    local_only = {"active_services": {k: v for k, v in services.items() if k != "api"}}
    plain = _label(tmp_path, services, path="deploy_stack", run="b0", extra=local_only)
    server = {**local_only, "build_server": "localhost"}
    bs_stack = _label(tmp_path, services, path="deploy_stack", run="b1", extra=server)
    bs_full = _label(tmp_path, services, path="full", run="b2", extra=server)
    assert bs_stack == bs_full
    # The branch really is the build-server one: it adds remote services to the map.
    assert bs_stack != plain
    peers_stack = _label(tmp_path, services, path="deploy_stack", run="p1", extra=extra)
    peers_full = _label(tmp_path, services, path="full", run="p2", extra=extra)
    assert peers_stack == peers_full != stack


def test_receiver_config_hash_has_one_owner() -> None:
    """The hash expression lives once, in build_specs.yml, and only the spec reads it."""
    text = BUILD_SPECS.read_text()
    assert text.count("webhook-config.json.j2") == 1
    assert text.count("image-map.json.j2") == 1
    assert text.count("_webhook_receiver_config_hash") >= 2  # definition + label
    for path in (ROOT / "roles").rglob("*.yml"):
        if path != BUILD_SPECS:
            assert f"{LABEL}:" not in path.read_text(), path
