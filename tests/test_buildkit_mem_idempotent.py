"""The buildkit memory-limit task reports `changed` only when the limit moved.

`docker update` exits 0 whether or not it changed anything, and the task used
`changed_when: rc == 0`. So every deploy printed `changed`, for every fleet
that shares one build server. The task now compares the container's memory
and swap before and after.

The tests run the real tasks with ansible-playbook on localhost. A fake
`docker` keeps the container state in a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import run_role_tasks

ROLE_TASKS = Path(__file__).resolve().parent.parent / "roles" / "git_deploy" / "tasks"
CASES = [
    ("main.yml", "Apply memory limit to the bay buildx container"),
    ("setup_builder.yml", "Apply memory limit to builder container"),
]

FAKE_DOCKER = r"""#!/bin/sh
state="$FAKE_DOCKER_STATE"
case "$1" in
  inspect)
    [ -f "$state" ] || { echo "Error: No such object: $3" >&2; exit 1; }
    cat "$state" ;;
  update)
    [ -f "$state" ] || { echo "Error response from daemon: No such container: $6" >&2; exit 1; }
    [ -n "$FAKE_DOCKER_FAIL" ] && { echo "boom" >&2; exit 1; }
    echo "$3 $5" > "$state" ;;
esac
"""


def _run(tmp_path: Path, tasks: str, name: str, limit: str, state: str | None, fail=False):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    state_file = tmp_path / "state"
    if state is not None:
        state_file.write_text(state + "\n")
    import os
    os.environ["FAKE_DOCKER_STATE"] = str(state_file)
    if fail:
        os.environ["FAKE_DOCKER_FAIL"] = "1"
    else:
        os.environ.pop("FAKE_DOCKER_FAIL", None)
    try:
        proc = run_role_tasks(
            tmp_path, ROLE_TASKS / tasks, [name],
            {"bay_buildx_builder": "b", "git_deploy_buildkit_memory_max": limit},
            path_dir=bindir,
        )
    finally:
        os.environ.pop("FAKE_DOCKER_STATE", None)
        os.environ.pop("FAKE_DOCKER_FAIL", None)
    return proc, (state_file.read_text().strip() if state_file.exists() else None)


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_a_new_limit_is_applied_and_reported_changed(tmp_path, tasks, name):
    proc, state = _run(tmp_path, tasks, name, "2G", "0 0")
    assert proc.returncode == 0, proc.stdout
    assert state == "2G 2G"
    assert "changed=1" in proc.stdout


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_the_same_limit_again_is_not_a_change(tmp_path, tasks, name):
    first, _ = _run(tmp_path, tasks, name, "2G", "0 0")
    assert "changed=1" in first.stdout
    again, state = _run(tmp_path / "again", tasks, name, "2G", "2G 2G")
    assert again.returncode == 0, again.stdout
    assert state == "2G 2G"
    assert "changed=0" in again.stdout, again.stdout


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_another_fleets_different_limit_is_reported_changed(tmp_path, tasks, name):
    """The limit is one setting of a shared container: a different value is a change."""
    proc, state = _run(tmp_path, tasks, name, "3G", "2G 4G")
    assert "changed=1" in proc.stdout
    assert state == "3G 3G"


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_a_missing_container_is_not_a_failure(tmp_path, tasks, name):
    proc, _ = _run(tmp_path, tasks, name, "2G", None)
    assert proc.returncode == 0, proc.stdout
    assert "changed=0" in proc.stdout


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_a_real_docker_error_still_fails(tmp_path, tasks, name):
    proc, _ = _run(tmp_path, tasks, name, "2G", "0 0", fail=True)
    assert proc.returncode != 0
    assert "failed=1" in proc.stdout


@pytest.mark.parametrize(("tasks", "name"), CASES)
def test_an_empty_limit_skips_the_task(tmp_path, tasks, name):
    proc, state = _run(tmp_path, tasks, name, "", "2G 4G")
    assert proc.returncode == 0
    assert state == "2G 4G"
    assert "skipped=1" in proc.stdout
