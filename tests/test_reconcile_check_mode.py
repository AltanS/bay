"""Guard: the reconciler report task must not run when its command was skipped.

Regression context:
  `roles/container_lifecycle/tasks/reconcile.yml` runs the server-side
  reconciler with `ansible.builtin.command` and then debug-prints
  `_reconcile_result.stdout | from_json`.

  In `--check` mode the command module does not run, so `stdout` is empty and
  `from_json` raises on an empty string. That is not a warning — the play dies
  with exit 2, which made `bin/bay deploy ... -- --check --diff` unusable as a
  pre-deploy dry run.

  The guard has to be `_reconcile_result is not skipped`. An `rc is defined`
  guard does NOT work: a command task skipped by check mode still registers
  `rc: 0` and an empty `stdout`, so the condition passes and the crash happens
  anyway.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_RECONCILE_TASKS = (
    _REPO_ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"
)


def _tasks() -> list[dict]:
    with _RECONCILE_TASKS.open() as f:
        return yaml.safe_load(f)


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {_RECONCILE_TASKS}")


def test_reconciler_report_is_guarded_against_check_mode_skip() -> None:
    report = _task("Reconciler report")
    when = report.get("when")
    assert when is not None, (
        "'Reconciler report' has no `when` — in --check mode the command task "
        "above it is skipped, stdout is empty, and `from_json` kills the play."
    )
    when_str = when if isinstance(when, str) else " ".join(str(c) for c in when)
    assert "_reconcile_result is not skipped" in when_str, (
        "'Reconciler report' must be guarded by "
        "`_reconcile_result is not skipped`; found: " + repr(when)
    )


def test_reconciler_report_guard_is_not_rc_based() -> None:
    """`rc is defined` is the guard that looks right and silently fails.

    A command task skipped by check mode registers `rc: 0`, so an rc-based
    condition passes and the `from_json` still runs on an empty string.
    """
    report = _task("Reconciler report")
    when = report.get("when")
    when_str = when if isinstance(when, str) else " ".join(str(c) for c in (when or []))
    assert "rc is defined" not in when_str, (
        "rc-based guard does not hold in check mode — a skipped command task "
        "registers rc: 0. Use `is not skipped`."
    )


def test_reconciler_report_still_parses_stdout_as_json() -> None:
    """The guard must not have been 'fixed' by dropping the report itself."""
    report = _task("Reconciler report")
    msg = report["ansible.builtin.debug"]["msg"]
    assert "_reconcile_result.stdout" in msg and "from_json" in msg
