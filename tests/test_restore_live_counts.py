"""Structure guards for the exact live counts of a restore test (2.24.1).

The live read must stay read-only, bounded and serial; the limits must be
fleet variables with documented defaults. The end-to-end cases against a real
Postgres are in tests/test_restore_scratch.py (they skip without Docker).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "roles" / "backup" / "tasks" / "restore_scratch.yml"
DEFAULTS = ROOT / "roles" / "backup" / "defaults" / "main.yml"


def _tasks() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(items):
        for t in items:
            out.append(t)
            for key in ("block", "rescue", "always"):
                walk(t.get(key) or [])

    walk(yaml.safe_load(SCRATCH.read_text()))
    return out


def _task(name: str) -> dict[str, Any]:
    return next(t for t in _tasks() if t.get("name") == name)


def _live_script() -> str:
    return _task("Count the rows of every leaf table of the live database")["ansible.builtin.command"]["stdin"]


def test_the_live_counts_run_in_one_read_only_repeatable_read_session():
    script = _live_script()
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    assert lines[0] == "BEGIN READ ONLY ISOLATION LEVEL REPEATABLE READ;"
    assert lines[1].startswith("SET LOCAL statement_timeout = '{{ _scratch_count_timeout | int }}s'")
    assert lines[-1] == "ROLLBACK;"
    assert "COMMIT" not in script.upper()


def test_the_live_read_has_no_write_statement():
    script = _live_script().upper()
    for word in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "ALTER ", "CREATE ", "VACUUM",
                 "ANALYZE", "LOCK TABLE"):
        assert word not in script, word


def test_a_count_that_fails_is_rolled_back_to_a_savepoint_and_the_next_table_goes_on():
    script = _live_script()
    assert "SAVEPOINT bay_count" in script
    assert "ROLLBACK TO SAVEPOINT bay_count" in script
    assert "\\gexec" in script, "one query per table, run one after the other"
    # Leaf tables only, children of an inheritance parent not counted twice.
    assert "FROM ONLY %I.%I" in script
    assert "c.relkind = 'r'" in script
    assert "relkind = 'v'" not in script


def test_the_live_count_is_one_serial_session():
    task = _task("Count the rows of every leaf table of the live database")
    assert "loop" not in task and "async" not in task and "with_items" not in task
    # one docker exec with a script for the whole live side, none other reads the live tables
    with_script = [t["name"] for t in _tasks() if "stdin" in (t.get("ansible.builtin.command") or {})]
    assert with_script == [task["name"]]
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["docker", "exec", "-i", "{{ accessory }}"]


def test_the_live_read_does_not_fail_the_play_by_itself():
    task = _task("Count the rows of every leaf table of the live database")
    assert task["failed_when"] is False


def test_the_limits_are_documented_fleet_variables_with_defaults():
    defaults = yaml.safe_load(DEFAULTS.read_text())
    assert defaults["restore_test_max_loss_pct"] == 1.0
    assert defaults["restore_test_max_loss_rows"] == 1000
    assert defaults["restore_test_count_timeout"] == 120
    docs = (ROOT / "docs" / "backups.md").read_text()
    for var in ("restore_test_max_loss_pct", "restore_test_max_loss_rows", "restore_test_count_timeout"):
        assert f"`{var}`" in docs, var


def test_a_nonsense_limit_stops_before_anything_starts():
    names = [t.get("name") for t in yaml.safe_load(SCRATCH.read_text())]
    guard = names.index("Never start the scratch container with a limit that makes no sense")
    assert guard < names.index("Restore into the scratch container and count the rows")
    assert guard < names.index("Remove expired scratch containers")


def test_only_a_restore_test_fails_on_lost_rows():
    task = _task("Fail a restore test with a table that lost rows")
    assert task["when"] == "restore_test | default(false) | bool"
    assert "failures" in task["ansible.builtin.assert"]["that"]


def test_an_unverified_restore_test_sends_the_restore_test_alert_with_the_word_unverified():
    """2.24.1: nothing compared must not pass silently. The alert is sent from
    restore.yml (the registry source of backup.restore_test_failed), literal ID."""
    pb = yaml.safe_load((ROOT / "restore.yml").read_text())

    def walk(items):
        for t in items:
            yield t
            for key in ("block", "rescue", "always"):
                yield from walk(t.get(key) or [])

    tasks = [t for play in pb for t in walk(play.get("tasks") or [])]
    compose = next(t for t in tasks if t.get("name") == "Compose restore test unverified alert")
    assert compose["ansible.builtin.set_fact"]["_bay_alert_id"] == "backup.restore_test_failed"
    assert "unverified" in compose["ansible.builtin.set_fact"]["_bay_alert_message"].lower()
    cond = " ".join(compose["when"]) if isinstance(compose["when"], list) else compose["when"]
    assert "restore_test" in cond and "'unverified'" in cond
    deliver = tasks[tasks.index(compose) + 1]
    assert deliver["ansible.builtin.include_role"]["tasks_from"] == "send_alert"
    assert deliver["ignore_errors"] is True, "an alert problem must not fail the test"
    registry = yaml.safe_load((ROOT / "alerts" / "registry.yml").read_text())
    assert "unverified" in registry["backup.restore_test_failed"]["summary"]
