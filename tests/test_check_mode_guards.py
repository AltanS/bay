"""Guards: tasks that parse a command's stdout must not run when it was skipped.

Companion to tests/test_reconcile_check_mode.py, which pins the same invariant
for the deploy reconciler. The failure mode is identical everywhere:

  In `--check` mode Ansible does not execute `command`/`shell` modules. The
  registered result still carries `rc: 0` and an EMPTY `stdout`. Any follow-up
  task that pipes that stdout through `from_json` (or copies a file the skipped
  task was supposed to produce) then dies with exit 2 — the whole play aborts,
  so `--check --diff` cannot be used as a dry run at all.

  The guard must be `<result> is not skipped`. `rc is defined` is the guard that
  LOOKS right and silently fails: a skipped command task registers `rc: 0`, so
  the condition passes and the crash happens anyway.

Sites pinned here:
  * roles/tailscale_register — `_needs_registration` fact from `tailscale status`
  * roles/headscale         — ACL policy install from a staged render
  * restore.yml             — `_snap` fact from `restic snapshots --json`
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).parent.parent
_TAILSCALE = _REPO_ROOT / "roles" / "tailscale_register" / "tasks" / "main.yml"
_HEADSCALE = _REPO_ROOT / "roles" / "headscale" / "tasks" / "main.yml"
_RESTORE = _REPO_ROOT / "restore.yml"


def _load(path: Path) -> list[dict]:
    with path.open() as f:
        return yaml.safe_load(f)


def _find(tasks: list[dict], name: str, path: Path) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in {path}")


def _when_str(task: dict) -> str:
    when = task.get("when")
    if when is None:
        return ""
    if isinstance(when, str):
        return when
    return " ".join(str(c) for c in when)


def _restore_tasks() -> list[dict]:
    return _load(_RESTORE)[0]["tasks"]


# -- tailscale_register --------------------------------------------------------


def test_tailscale_registration_fact_is_guarded() -> None:
    task = _find(_load(_TAILSCALE), "Set registration needed fact", _TAILSCALE)
    when = _when_str(task)
    assert "tailscale_status_result is not skipped" in when, (
        "'Set registration needed fact' pipes `tailscale status --json` stdout "
        "through `from_json`. In check mode that command is skipped and stdout "
        "is empty, so from_json kills the play. Found when: " + repr(when)
    )


def test_tailscale_registration_fact_guard_is_not_rc_based() -> None:
    task = _find(_load(_TAILSCALE), "Set registration needed fact", _TAILSCALE)
    assert "rc is defined" not in _when_str(task), (
        "a command task skipped by check mode registers rc: 0 — an rc-based "
        "guard passes and from_json still runs on an empty string"
    )


def test_tailscale_register_never_acts_without_the_fact() -> None:
    """With the fact unset, no task may register a node.

    `_needs_registration` is undefined in check mode. Every consumer must fall
    back to false rather than raise (or, worse, be treated as truthy).
    """
    tasks = _load(_TAILSCALE)
    consumers = [t for t in tasks if "_needs_registration" in _when_str(t)]
    assert len(consumers) >= 10, "expected the whole role to gate on the fact"
    for task in consumers:
        when = _when_str(task)
        assert "_needs_registration | default(false)" in when, (
            f"{task['name']!r} uses a bare `_needs_registration`; it is "
            "undefined in check mode. Use `| default(false)`."
        )


# -- headscale ACL policy ------------------------------------------------------


def test_headscale_policy_install_is_guarded_against_check_mode() -> None:
    """The staged source does not exist in check mode, so the copy would abort.

    `Stage headscale ACL policy for validation` is a `template`, which supports
    check mode and therefore writes nothing while still reporting "changed" —
    `is not skipped` would NOT hold it back. `Remove staged headscale ACL
    policy` deletes the staged file at the end of every real run, so it is
    never left behind for a later dry run to find. `ansible_check_mode` is the
    only honest signal, and installing is precisely the host write a dry run
    must not perform.
    """
    task = _find(_load(_HEADSCALE), "Install validated headscale ACL policy", _HEADSCALE)
    when = _when_str(task)
    assert "not ansible_check_mode" in when, (
        "'Install validated headscale ACL policy' copies "
        "/opt/headscale/config/.policy.hujson.staged, which does not exist in "
        "check mode — the copy aborts with 'Source does not exist'. "
        "Found when: " + repr(when)
    )
    assert "check_mode: false" not in yaml.safe_dump(task), (
        "the install must SKIP in check mode, not force itself to run"
    )


def test_headscale_policy_assert_is_guarded_and_not_rc_based() -> None:
    task = _find(_load(_HEADSCALE), "Assert headscale ACL policy is valid", _HEADSCALE)
    when = _when_str(task)
    assert "headscale_policy_check is not skipped" in when
    assert "headscale_policy_check.rc is defined" not in when, (
        "the validation command registers rc: 0 when skipped by check mode"
    )


# -- restore.yml ---------------------------------------------------------------


def test_restore_snapshot_parse_is_guarded() -> None:
    task = _find(_restore_tasks(), "Parse snapshot details", _RESTORE)
    when = _when_str(task)
    assert "_snapshot_info is not skipped" in when, (
        "'Parse snapshot details' pipes `restic snapshots --json` stdout "
        "through from_json; in check mode that command is skipped and stdout "
        "is empty. Found when: " + repr(when)
    )


def test_restore_snapshot_parse_guard_is_not_rc_based() -> None:
    task = _find(_restore_tasks(), "Parse snapshot details", _RESTORE)
    assert "rc is defined" not in _when_str(task)


def test_restore_never_restores_without_snapshot_metadata() -> None:
    """`_snap` is undefined in check mode; nothing may dereference it unguarded."""
    for task in _restore_tasks():
        body = yaml.safe_dump({k: v for k, v in task.items() if k != "when"})
        if "_snap." not in body and "_snap " not in body:
            continue
        when = _when_str(task)
        assert "_snap is defined" in when, (
            f"{task['name']!r} dereferences `_snap`, which is undefined in "
            "check mode; gate it with `_snap is defined`"
        )
