"""`bay deploy <env> --tags tailnet_identity` rotates the sidecar's API key alone.

Before 2.19.0 the tag only rendered the env file. Docker reads an env file only
when it creates a container, and `docker restart` keeps the old environment,
so a new key needed `--tags tailnet_identity,deploy_stack`, which reconciles
every container on the box.

The role now reconciles the sidecar alone (container_lifecycle_only), like the
headscale role does for its container. Two kinds of test:

* the wiring of the role (parsed YAML), as tests/test_headscale_reconcile.py;
* a real ansible-playbook on localhost that renders the env file with two
  keys, builds the real specs (build_specs.yml) and the real bundle entries
  (reconcile.yml), and compares the config hashes: only the sidecar's hash
  moves, and the narrow bundle holds only the sidecar.

The recreate itself, the health wait and the key check need a real host.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
_ROLE = ROOT / "roles" / "tailnet_identity"
_RECONCILE = ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
_ENTRY_TASKS = (
    "Reset the reconcile bundle entries",
    "Read the rendered env files in one pass",
    "Fail with the list of env files that could not be read",
    "Build reconcile bundle entries",
)


def _tasks() -> list[dict[str, Any]]:
    return yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())


def _by_name(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task named {name!r}")


_RECONCILE_TASK = "Bring the tailnet-identity sidecar onto its current env file and image"


# ── the wiring ──────────────────────────────────────────────────────────────


def test_reconciles_only_the_sidecar_without_orphan_removal() -> None:
    task = _by_name(_tasks(), _RECONCILE_TASK)
    include = task["ansible.builtin.include_role"]
    assert include["name"] == "container_lifecycle"
    # Without `apply`, `--tags tailnet_identity` runs the include and skips its tasks.
    assert "tailnet_identity" in include["apply"]["tags"]
    assert "tailnet_identity" in task["tags"]
    assert task["vars"]["container_lifecycle_only"] == ["tailnet-identity"]
    assert task["vars"]["bay_reconciler_remove_orphans"] is False


def test_the_reconcile_runs_after_the_env_file_and_the_image() -> None:
    names = [t.get("name") for t in _tasks()]
    at = names.index(_RECONCILE_TASK)
    assert names.index("Render tailnet-identity env file") < at
    assert names.index("Build tailnet-identity image") < at


def test_only_a_run_without_deploy_stack_reconciles_here() -> None:
    tasks = _tasks()
    decide = _by_name(tasks, "Decide whether this run reconciles the sidecar itself")
    expr = decide["ansible.builtin.set_fact"]["_tailnet_identity_narrow"]
    assert "'all' not in ansible_run_tags" in expr
    assert "'deploy_stack' not in ansible_run_tags" in expr
    for name in (
        "Check whether the tailnet-identity container exists",
        _RECONCILE_TASK,
        "Wait for the tailnet-identity sidecar to answer",
        "Check the sidecar's API key against Headscale",
    ):
        assert "_tailnet_identity_narrow | bool" in _by_name(tasks, name)["when"], name


def test_a_host_without_the_sidecar_is_left_to_deploy_stack() -> None:
    tasks = _tasks()
    probe = _by_name(tasks, "Check whether the tailnet-identity container exists")
    # Read-only: it must run in --check too, or the dry run plans nothing.
    assert probe["check_mode"] is False and probe["changed_when"] is False
    assert probe["become_user"] == "root"
    assert "_tailnet_identity_container_exists" in str(_by_name(tasks, _RECONCILE_TASK)["when"])


def test_the_health_wait_and_the_key_check_never_run_in_check_mode() -> None:
    tasks = _tasks()
    wait = _by_name(tasks, "Wait for the tailnet-identity sidecar to answer")
    assert "not ansible_check_mode" in wait["when"]
    assert "_tailnet_identity_reconciled | default(false) | bool" in wait["when"]
    assert wait["ansible.builtin.uri"]["url"] == "http://127.0.0.1:{{ tailnet_identity_port }}/healthz"
    key = _by_name(tasks, "Check the sidecar's API key against Headscale")
    assert "not ansible_check_mode" in key["when"]
    assert "tailnet_identity_source == 'api'" in key["when"]


def test_the_key_check_never_puts_the_key_on_a_command_line() -> None:
    key = _by_name(_tasks(), "Check the sidecar's API key against Headscale")
    argv = key["ansible.builtin.command"]["argv"]
    text = json.dumps(argv)
    assert "tailnet_identity_api_key" not in text and "{{" not in text
    assert 'os.environ["HEADSCALE_API_KEY"]' in argv[-1]
    # Only the status is printed.
    assert "print(resp.status)" in argv[-1] and "print(exc.code)" in argv[-1]
    assert key["failed_when"] == "(_tailnet_identity_key_check.stdout | default('') | trim) != '200'"


def test_no_second_copy_of_the_sidecar_spec() -> None:
    # One definition, in container_lifecycle/tasks/build_specs.yml.
    text = (_ROLE / "tasks" / "main.yml").read_text()
    for forbidden in ("restart_policy", "127.0.0.1:{{ tailnet_identity_port }}:", "traefik.enable"):
        assert forbidden not in text


# ── the hashes, in a real ansible-playbook ──────────────────────────────────


def _entries(tmp: Path, key: str, *, only: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Render the env file with ``key``, build the specs and the bundle entries.

    Returns ``{container: entry}`` of the bundle. ``only`` is
    ``container_lifecycle_only`` (the narrow pass).
    """
    run = tmp / f"run-{len(list(tmp.glob('run-*')))}"
    run.mkdir()
    stack = tmp / "stack"
    out = run / "entries.json"
    entries = [t for t in yaml.safe_load(_RECONCILE.read_text()) if t.get("name") in _ENTRY_TASKS]
    assert [t["name"] for t in entries] == list(_ENTRY_TASKS)
    (run / "entries.yml").write_text(yaml.safe_dump(entries, sort_keys=False))
    variables: dict[str, Any] = {
        "stack_dir": str(stack),
        "stack_name": "bay",
        "tailnet_identity_enabled": True,
        "headscale_server": True,
        "tailnet_identity_api_key": key,
        "active_services": {
            "api": {"image": "ghcr.io/acme/api:1", "port": 3000, "domains": ["api.example.com"],
                    "env": {"clear": {"A": "1"}}},
        },
        "active_accessories": {},
        "out_file": str(out),
    }
    if only is not None:
        variables["container_lifecycle_only"] = only
    (run / "vars.json").write_text(json.dumps(variables))
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [
            {"name": "Load the tailnet-identity defaults", "ansible.builtin.include_vars": {
                "file": str(_ROLE / "defaults" / "main.yml")}},
            {"name": "Ensure the stack folders exist", "ansible.builtin.file": {
                "path": "{{ stack_dir }}/{{ item }}", "state": "directory", "mode": "0755"},
             "loop": ["tailnet-identity", "env"]},
            # The role's own template, as `Render tailnet-identity env file` renders it.
            {"name": "Render the sidecar env file", "ansible.builtin.template": {
                "src": str(_ROLE / "templates" / "tailnet-identity.env.j2"),
                "dest": "{{ stack_dir }}/tailnet-identity/tailnet-identity.env", "mode": "0600"}},
            {"name": "Build the specs", "ansible.builtin.include_role": {
                "name": "container_lifecycle", "tasks_from": "build_specs"}},
            {"name": "Pick the specs of this pass", "ansible.builtin.set_fact": {
                "_reconcile_specs": "{{ _container_lifecycle_partial_specs if container_lifecycle_only"
                                    " is defined else container_specs }}"}},
            # The other env files exist on a box; their content does not change here.
            {"name": "Ensure the other env files exist", "ansible.builtin.copy": {
                "dest": "{{ item }}", "content": "X=1\n", "force": False, "mode": "0640"},
             "loop": "{{ _reconcile_specs | selectattr('env_file', 'defined')"
                     " | map(attribute='env_file') | reject('search', 'tailnet-identity') | list }}"},
            {"name": "Build the entries", "ansible.builtin.include_tasks": str(run / "entries.yml")},
            {"name": "Save the entries", "ansible.builtin.copy": {
                "dest": "{{ out_file }}", "mode": "0600",
                "content": "{{ _reconcile_entries | map(attribute='name') | zip("
                           "_reconcile_entries | map(attribute='config_hash')) | list | to_json }}"}},
        ],
    }]
    (run / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    (run / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_FILTER_PLUGINS": str(ROOT / "filter_plugins"),
        "ANSIBLE_CONFIG": str(run / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{run / 'vars.json'}", str(run / "play.yml")],
        cwd=run, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return {name: {"config_hash": h} for name, h in json.loads(out.read_text())}


def test_a_new_key_moves_only_the_sidecar_hash(tmp_path: Path) -> None:
    old = _entries(tmp_path, "hskey-api-old-placeholder")
    new = _entries(tmp_path, "hskey-api-new-placeholder")
    assert set(old) == set(new) >= {"tailnet-identity", "traefik", "watchtower", "api"}
    moved = sorted(name for name in old if old[name] != new[name])
    assert moved == ["tailnet-identity"]
    # The same key again is the same hash: a deploy with an unchanged key is a NoOp.
    assert _entries(tmp_path, "hskey-api-old-placeholder") == old


def test_the_narrow_pass_holds_only_the_sidecar(tmp_path: Path) -> None:
    full = _entries(tmp_path, "hskey-api-a-placeholder")
    narrow = _entries(tmp_path, "hskey-api-a-placeholder", only=["tailnet-identity"])
    assert list(narrow) == ["tailnet-identity"]
    # The same spec and env file in both passes: the hash a full deploy_stack
    # run would compute, so the next full run sees no drift.
    assert narrow["tailnet-identity"] == full["tailnet-identity"]
