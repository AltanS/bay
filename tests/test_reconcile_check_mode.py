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


# Since M115/S06 the pass (bundle write .. CLI hand-off) sits in one block
# whose `always:` removes the bundle. Its children are read as if they were
# top-level: the layout rules below are about the pass, not the wrapper.
_PASS_BLOCK = "Reconcile the containers and record the result"


def _tasks() -> list[dict]:
    with _RECONCILE_TASKS.open() as f:
        top = yaml.safe_load(f)
    out: list[dict] = []
    for task in top:
        if task.get("name") == _PASS_BLOCK:
            out.extend(task["block"])
            out.extend(task.get("always", []))
        else:
            out.append(task)
    return out


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


# ── The container plan under --check (GH #2) ─────────────────────────────
#
# Check mode skips the real reconciler run, so a dry run printed no plan. The
# fix runs the reconciler with --plan-only in check mode. The trap: check mode
# also skips the bundle write and the package ship, so planning from
# stack_dir would read what the LAST real deploy left. The plan must come from
# a per-run temporary directory that holds the NEW bundle and package.
#
# Structural assertions over the task file. The repo has no ansible-runner or
# molecule harness that runs this role against a live docker daemon.

_CHECK_BLOCK = "Plan the container changes in check mode"
_REAL_RUN = "Run server-side reconciler"
_TMP = "_reconcile_check_dir.path"


def _check_block() -> dict:
    return _task(_CHECK_BLOCK)


def _block_tasks() -> list[dict]:
    block = _check_block()
    return list(block["block"]) + list(block.get("always", []))


def _module(task: dict) -> tuple[str, dict]:
    for key, value in task.items():
        if key.startswith("ansible.builtin."):
            return key, value
    raise AssertionError(f"no module in task {task.get('name')!r}")


def _in_block(name: str) -> dict:
    for task in _block_tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not in the check-mode block")


def test_check_mode_plan_block_is_gated_on_check_mode() -> None:
    assert _check_block()["when"] == "ansible_check_mode"


def test_check_mode_plan_block_is_the_only_new_top_level_task() -> None:
    """Outside check mode the task list is the one the real deploy always ran.

    Every top-level task except the check block is free of check-mode logic,
    and the real run keeps no `when` and no `check_mode` key, so --check still
    skips it and a real deploy still runs it.
    """
    for task in _tasks():
        if task.get("name") == _CHECK_BLOCK:
            continue
        text = yaml.safe_dump(task)
        assert "ansible_check_mode" not in text, task.get("name")
        assert "_reconcile_check_" not in text, task.get("name")
    real = _task(_REAL_RUN)
    assert "when" not in real
    assert "check_mode" not in real
    names = [t.get("name") for t in _tasks()]
    assert names.index(_CHECK_BLOCK) == names.index(_REAL_RUN) + 1


def test_every_check_mode_step_really_runs_under_check() -> None:
    """A step skipped by check mode would leave the plan reading nothing."""
    for task in _block_tasks():
        if "ansible.builtin.debug" in task:
            continue
        assert task.get("check_mode") is False, task.get("name")


def test_check_mode_plan_uses_a_temp_dir_never_stack_dir() -> None:
    tmp = _in_block("Create a temporary directory for the check-mode plan")
    assert tmp["ansible.builtin.tempfile"]["state"] == "directory"
    assert tmp["register"] == "_reconcile_check_dir"

    for task in _block_tasks():
        if task.get("delegate_to") == "localhost":
            continue  # controller-side package cache, never the host
        assert "stack_dir" not in yaml.safe_dump(task), task.get("name")
        _, args = _module(task)
        for key in ("dest", "path"):
            if key in args:
                assert _TMP in args[key], f"{task['name']}: {key}={args[key]}"


def test_check_mode_plan_writes_the_same_bundle_a_real_run_writes() -> None:
    """The temp bundle is the real bundle expression, not a hand-kept copy."""
    real = _task("Write reconcile bundle (resolved env — mode 0600, removed after run)")
    check = _in_block("Write the check-mode plan bundle into the temporary directory")
    assert (
        check["ansible.builtin.copy"]["content"]
        == real["ansible.builtin.copy"]["content"]
    )
    assert check["ansible.builtin.copy"]["mode"] == "0600"
    assert check["no_log"] is True
    assert check["diff"] is False


def test_check_mode_plan_ships_the_new_package_into_the_temp_dir() -> None:
    ship = _task("Ship the bay_reconcile package")
    assert _check_block()["vars"] == ship["vars"], "same tar as the real ship"
    unpack = _in_block("Unpack bay_reconcile into the temporary directory")
    assert unpack["ansible.builtin.unarchive"]["src"] == "{{ _pkg_tar }}"
    pack = _in_block("Pack bay_reconcile for the check-mode plan")
    real_pack = _task_nested("Pack bay_reconcile without __pycache__")
    assert pack["ansible.builtin.shell"] == real_pack["ansible.builtin.shell"]


def _task_nested(name: str) -> dict:
    def walk(items: list[dict]):
        for task in items:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key, []))

    for task in walk(_tasks()):
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found")


def test_check_mode_run_is_plan_only_from_the_temp_dir() -> None:
    run = _in_block("Run the reconciler in plan-only mode")
    argv = run["ansible.builtin.command"]["argv"]
    assert argv[:3] == ["python3", "-m", "bay_reconcile"]
    assert "--plan-only" in argv, "--plan-only must be forced, not optional"
    assert _TMP in argv[3]
    assert _TMP in run["environment"]["PYTHONPATH"]
    assert run["register"] == "_reconcile_check_result", (
        "must not reuse _reconcile_result — the CLI hand-off would then run "
        "under --check and the post-deploy summary would read a plan as a deploy"
    )
    assert run["failed_when"] == "_reconcile_check_result.rc != 0"


def test_check_mode_changed_means_the_plan_has_work() -> None:
    changed = " ".join(_in_block("Run the reconciler in plan-only mode")["changed_when"].split())
    assert "reject('equalto', 'NoOp')" in changed
    assert "length > 0" in changed
    assert changed.startswith("_reconcile_check_result.rc == 0 and")


def test_check_mode_report_says_the_plan_uses_images_on_the_host() -> None:
    report = _in_block("Reconciler check-mode plan")
    when = report.get("when", "")
    assert "_reconcile_check_result is not skipped" in when
    assert "rc is defined" not in when
    msg = report["ansible.builtin.debug"]["msg"]
    note = " ".join(msg["note"].split())
    assert "images" in note and "on the host now" in note
    assert "rebuild" in note and "NoOp" in note
    assert "_reconcile_check_result.stdout" in msg["plan"]


def test_check_mode_temp_dir_is_always_removed() -> None:
    always = _check_block().get("always", [])
    assert always, "cleanup must sit in `always:` so a failed plan still cleans up"
    rm = always[0]
    assert rm["ansible.builtin.file"]["state"] == "absent"
    assert rm["ansible.builtin.file"]["path"] == "{{ " + _TMP + " }}"
    assert rm["check_mode"] is False
    assert "path is defined" in rm["when"]
