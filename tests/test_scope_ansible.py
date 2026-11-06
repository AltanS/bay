"""A scoped ``bay up`` with real Ansible: the limit and the build server bootstrap (2.13.0).

``bay up <env> --project <p>`` runs ``ansible-playbook deploy.yml -l <hosts>``
with ``-e bay_scope_limited=true`` (:func:`bay_cli.scope.limit_args`). These
tests run the real ``ansible-playbook`` on a four-host fixture inventory
(192.0.2.0/24, local connection). No host is contacted, except one
unreachable TEST-NET address in the test that proves the old behaviour.

1. ``--list-hosts`` on the real deploy.yml: the limit selects exactly the
   scope hosts in every play.
2. The build server bootstrap play of deploy.yml (``run_once`` +
   ``delegate_to: build_server``, tag ``always``), taken from the real file
   with its ``hosts``, ``run_once``, ``when`` and ``delegate_to`` as they are.
   Only the module arguments change (a marker file instead of the stack
   directory). A limited run with the build server outside the limit skips
   it; the same run without the flag var (``bay deploy --region``) still
   reaches the build server; a limited run with the build server inside the
   limit runs it.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from bay_cli import scope as scopemod

ROOT = Path(__file__).resolve().parent.parent
COLLECTIONS = ROOT / "vendor" / "collections"
HOSTS = ["192.0.2.11", "192.0.2.12", "192.0.2.13", "192.0.2.14"]
BUILD_SERVER = HOSTS[3]

pytestmark = pytest.mark.skipif(
    not (COLLECTIONS / "ansible_collections").is_dir(),
    reason="needs the vendored collections (make install)",
)


def _inventory(tmp_path: Path, *, server_reachable: bool) -> Path:
    """Four groups, one host each. The build server is the fourth host."""
    lines = ["[production:children]", "one", "two", "three", "four", ""]
    for group, host in zip(("one", "two", "three", "four"), HOSTS, strict=True):
        lines += [f"[{group}]", host, ""]
    lines += ["[production:vars]", "ansible_connection=local", f"build_server={BUILD_SERVER}", ""]
    path = tmp_path / "inventory"
    path.mkdir()
    (path / "production").write_text("\n".join(lines))
    if not server_reachable:
        # A TEST-NET address over ssh: a task delegated there fails UNREACHABLE.
        (path / "host_vars").mkdir()
        (path / "host_vars" / f"{BUILD_SERVER}.yml").write_text(
            "---\nansible_connection: ssh\nansible_host: 192.0.2.1\n"
            "ansible_ssh_common_args: -o ConnectTimeout=2 -o BatchMode=yes\n"
        )
    return path


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS),
        "ANSIBLE_ROLES_PATH": f"{ROOT / 'vendor' / 'roles'}:{ROOT / 'roles'}",
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
    }


def _playbook(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_env(),
        timeout=180,
    )


def _listed(stdout: str) -> list[list[str]]:
    """The hosts of each play in ``--list-hosts`` output."""
    plays: list[list[str]] = []
    in_hosts = False
    for line in stdout.splitlines():
        text = line.strip()
        if text.startswith("play #"):
            plays.append([])
            in_hosts = False
        elif text.startswith("hosts ("):
            in_hosts = True
        elif in_hosts and text:
            plays[-1].append(text)
        else:
            in_hosts = False
    return plays


def test_the_limit_selects_exactly_the_scope_hosts(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path, server_reachable=True)
    scope_hosts = [HOSTS[0], HOSTS[2]]
    proc = _playbook(
        str(ROOT / "deploy.yml"),
        "-i", str(inventory),
        "-e", "target_host=production",
        "--tags", "deploy_stack",
        *scopemod.limit_args(scope_hosts),
        "--list-hosts",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    plays = _listed(proc.stdout)
    assert len(plays) == 4
    assert all(sorted(p) == scope_hosts for p in plays), plays

    # No limit (a run with no scope): every host of the box env.
    proc = _playbook(
        str(ROOT / "deploy.yml"),
        "-i", str(inventory),
        "-e", "target_host=production",
        "--tags", "deploy_stack",
        "--list-hosts",
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(sorted(p) == HOSTS for p in _listed(proc.stdout))


def _bootstrap_play(marker: Path) -> dict[str, Any]:
    """The real build server bootstrap play of deploy.yml, with a marker file as its target."""
    plays = yaml.safe_load((ROOT / "deploy.yml").read_text())
    name = "Bootstrap stack directory on build server"
    play = copy.deepcopy(next(p for p in plays if p["name"] == name))
    assert play["run_once"] is True and "always" in play["tags"]
    assert play["hosts"] == "{{ target_host }}"
    (task,) = play["tasks"]
    assert task["delegate_to"] == "{{ build_server | default(inventory_hostname) }}"
    assert task["when"] == [
        "build_server is defined",
        "not (bay_scope_limited | default(false) | bool) or build_server in ansible_play_hosts_all",
    ]
    # Only the module arguments change: no root, no app user, no docker group here.
    task["ansible.builtin.file"] = {"path": str(marker), "state": "touch"}
    play.pop("vars_files", None)
    play["become"] = False
    return play


def _run_bootstrap(
    tmp_path: Path, inventory: Path, limit: list[str], *, flag: bool
) -> tuple[subprocess.CompletedProcess[str], Path]:
    marker = tmp_path / f"bootstrap-{len(list(tmp_path.glob('play-*.yml')))}"
    playbook = tmp_path / f"play-{len(list(tmp_path.glob('play-*.yml')))}.yml"
    playbook.write_text(yaml.safe_dump([_bootstrap_play(marker)], sort_keys=False))
    args = scopemod.limit_args(limit) if flag else ["-l", ",".join(limit)]
    proc = _playbook(
        str(playbook), "-i", str(inventory), "-e", "target_host=production", *args, cwd=tmp_path
    )
    return proc, marker


def test_a_limited_run_skips_a_build_server_outside_the_limit(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path, server_reachable=False)
    proc, marker = _run_bootstrap(tmp_path, inventory, [HOSTS[1]], flag=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipping" in proc.stdout
    assert "UNREACHABLE" not in proc.stdout
    assert not marker.exists()


def test_without_the_flag_the_play_still_reaches_the_build_server(tmp_path: Path) -> None:
    """``bay deploy --region`` passes ``-l`` and no flag: the old behaviour, which the
    guard exists for. The run delegates to the build server outside the limit."""
    inventory = _inventory(tmp_path, server_reachable=False)
    proc, marker = _run_bootstrap(tmp_path, inventory, [HOSTS[1]], flag=False)
    assert proc.returncode != 0
    assert "UNREACHABLE" in proc.stdout
    assert f"-> {BUILD_SERVER}" in proc.stdout


def test_a_limited_run_with_the_build_server_inside_the_limit_runs_it(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path, server_reachable=True)
    proc, marker = _run_bootstrap(tmp_path, inventory, [HOSTS[1], BUILD_SERVER], flag=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert marker.exists()
    assert f"-> {BUILD_SERVER}" in proc.stdout
