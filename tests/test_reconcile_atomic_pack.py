"""The controller-side reconciler tar must be written atomically.

`creates:` is a check-then-act. With forks, two hosts can both see no tar and
both pack into the same `_pkg_tar`, and a third host can then unpack a
half-written file. Both pack tasks (the real ship and the check-mode plan)
pack into a per-host temp file and `mv -f` it onto the final path. A rename
inside one directory is atomic, so the final path is either absent or whole.
"""

from __future__ import annotations

import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

_RECONCILE = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "container_lifecycle"
    / "tasks"
    / "reconcile.yml"
)

PACK_TASKS = (
    "Pack bay_reconcile without __pycache__",
    "Pack bay_reconcile for the check-mode plan",
)


def _walk(items: list[dict]):
    for task in items:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key, []))


def _pack(name: str) -> dict:
    for task in _walk(yaml.safe_load(_RECONCILE.read_text())):
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found")


@pytest.mark.parametrize("name", PACK_TASKS)
def test_pack_goes_through_a_temp_file_then_a_rename(name: str) -> None:
    task = _pack(name)
    shell = task["ansible.builtin.shell"]
    script = shell["cmd"]
    assert shell["executable"] == "/bin/bash"
    assert "set -euo pipefail" in script
    # Temp path is next to the final tar (same directory, same filesystem) and
    # per host, so two forks never write the same file.
    assert "_pkg_tar ~ '.' ~ inventory_hostname ~ '.tmp'" in script
    assert 'tar \'--exclude=__pycache__\' \'--exclude=*.pyc\' -czf "$tmp"' in script
    assert 'mv -f "$tmp"' in script
    assert script.index("-czf") < script.index("mv -f")
    # An existing tar is still reused, and the failure path cleans up.
    assert shell["creates"] == "{{ _pkg_tar }}"
    assert "trap 'rm -f \"$tmp\"' EXIT" in script
    assert "run_once" not in task


def test_both_pack_tasks_run_the_same_script() -> None:
    first, second = (_pack(n)["ansible.builtin.shell"] for n in PACK_TASKS)
    assert first == second


def _render(script: str, **variables: str) -> str:
    env = Environment(trim_blocks=True, keep_trailing_newline=True)
    env.filters["quote"] = shlex.quote
    return env.from_string(script).render(**variables)


def _run(tmp_path: Path, playbook_dir: Path) -> tuple[subprocess.CompletedProcess, Path]:
    tar_path = tmp_path / "cache dir" / "bay_reconcile-abc.tar.gz"
    tar_path.parent.mkdir(exist_ok=True)
    script = _render(
        _pack(PACK_TASKS[0])["ansible.builtin.shell"]["cmd"],
        _pkg_tar=str(tar_path),
        inventory_hostname="host-a",
        playbook_dir=str(playbook_dir),
    )
    proc = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, check=False
    )
    return proc, tar_path


def test_script_publishes_a_whole_tar_and_leaves_no_temp(tmp_path: Path) -> None:
    pkg = tmp_path / "pb" / "src" / "bay_reconcile"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "mod.py").write_text("x = 1\n")
    (pkg / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"junk")

    proc, tar_path = _run(tmp_path, tmp_path / "pb")

    assert proc.returncode == 0, proc.stderr
    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
    assert "bay_reconcile/mod.py" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert list(tar_path.parent.iterdir()) == [tar_path]


def test_script_failure_publishes_nothing_and_removes_the_temp(tmp_path: Path) -> None:
    # No src/bay_reconcile, so tar fails after it created the temp file.
    (tmp_path / "pb" / "src").mkdir(parents=True)

    proc, tar_path = _run(tmp_path, tmp_path / "pb")

    assert proc.returncode != 0
    assert list(tar_path.parent.iterdir()) == []
