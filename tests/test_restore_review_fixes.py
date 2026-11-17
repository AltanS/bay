"""2.24.0 review fixes for the restore paths that need no container runtime.

- Every restic call of restore.yml and restore_scratch.yml runs with
  --no-cache. They run as root; a cache under the scripts dir became
  root-owned, and the backup unit (the app user) then ran without a cache.
- The space a scratch restore needs comes from the live pg_database_size
  plus a margin; the restic stats fallback reads the JSON line among other
  output (restic 0.19 prints more than one line).
- A failed restore test sends backup.restore_test_failed (warning), not
  restore.failed (critical).
- The freshness limit of a target follows its schedule: 1.25 times the
  longest gap between two runs, at least backup_max_age_hours.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "filter_plugins"))

RESTORE = ROOT / "restore.yml"
SCRATCH = ROOT / "roles" / "backup" / "tasks" / "restore_scratch.yml"


def _walk(items):
    for t in items or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from _walk(t.get(key))


def _restore_tasks() -> list[dict[str, Any]]:
    play = yaml.safe_load(RESTORE.read_text())[0]
    return list(_walk(play.get("pre_tasks"))) + list(_walk(play.get("tasks")))


def _scratch_tasks() -> list[dict[str, Any]]:
    return list(_walk(yaml.safe_load(SCRATCH.read_text())))


def _command_text(task: dict[str, Any]) -> str:
    for key in ("ansible.builtin.shell", "ansible.builtin.command"):
        body = task.get(key)
        if isinstance(body, str):
            return body
        if isinstance(body, dict):
            if "argv" in body:
                return " ".join(str(a) for a in body["argv"])
            return str(body.get("cmd", ""))
    return ""


# ── restic cache ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("source", ["restore", "scratch"])
def test_every_restic_call_of_a_restore_runs_without_a_cache(source: str) -> None:
    tasks = _restore_tasks() if source == "restore" else _scratch_tasks()
    calls = [(t["name"], _command_text(t)) for t in tasks if "_backup_restic_bin" in _command_text(t)]
    assert calls, "found no restic call"
    missing = [name for name, text in calls if "--no-cache" not in text]
    assert not missing, f"restic without --no-cache: {missing}"


def test_the_restore_restic_env_names_no_cache_dir() -> None:
    env = yaml.safe_load(RESTORE.read_text())[0]["vars"]["_restic_env"]
    assert "RESTIC_CACHE_DIR" not in env


# ── the space a scratch restore needs ───────────────────────────────────────

NEED = "Work out the space the scratch restore needs"


def _need(tmp_path: Path, **variables: Any) -> dict[str, Any]:
    task = next(t for t in _scratch_tasks() if t.get("name") == NEED)
    show = {"name": "Show", "ansible.builtin.debug": {"msg": "NEED {{ _scratch_need | to_json }}"}}
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "tasks": [task, show]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_LOCALHOST_WARNING": "0", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
           "ANSIBLE_PYTHON_INTERPRETER": sys.executable}
    proc = subprocess.run([sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
                           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:]
    for line in proc.stdout.splitlines():
        if '"msg": "NEED ' in line:
            return json.loads(json.loads("{" + line.strip().rstrip(",") + "}")["msg"][5:])
    raise AssertionError(proc.stdout[-3000:])


GIB = 1024 ** 3


def test_the_need_follows_the_live_database_size(tmp_path: Path) -> None:
    got = _need(tmp_path, _scratch_live_size={"rc": 0, "stdout": str(8 * GIB)},
                _scratch_size={"skipped": True})
    assert got["source"] == "live database"
    # The live size plus a quarter, at least 1 GiB more.
    assert got["bytes"] == 10 * GIB


def test_a_small_database_still_gets_a_margin(tmp_path: Path) -> None:
    got = _need(tmp_path, _scratch_live_size={"rc": 0, "stdout": str(1000)},
                _scratch_size={"skipped": True})
    assert got["bytes"] == 1000 + GIB


def test_without_the_live_size_the_restic_stats_json_line_counts(tmp_path: Path) -> None:
    """restic 0.19 can print a status line before the JSON document."""
    stdout = "repository 1234abcd opened (version 2)\n" + json.dumps(
        {"total_size": 3 * GIB, "total_file_count": 1, "snapshots_count": 1})
    got = _need(tmp_path, _scratch_live_size={"rc": 2, "stdout": ""},
                _scratch_size={"rc": 0, "stdout": stdout})
    assert got["source"] == "snapshot"
    assert got["bytes"] == 6 * GIB


# ── alert IDs ───────────────────────────────────────────────────────────────


def test_a_failed_restore_test_has_its_own_alert() -> None:
    registry = yaml.safe_load((ROOT / "alerts" / "registry.yml").read_text())
    alerts = registry
    entry = alerts["backup.restore_test_failed"]
    assert entry["level"] == "warn"
    play = yaml.safe_load(RESTORE.read_text())[0]
    rescue = next(t for t in play["tasks"] if "block" in t)["rescue"]
    ids = {t["ansible.builtin.set_fact"]["_bay_alert_id"]: t.get("when")
           for t in rescue if "_bay_alert_id" in (t.get("ansible.builtin.set_fact") or {})}
    assert set(ids) == {"restore.failed", "backup.restore_test_failed"}
    assert "restore_test" in str(ids["backup.restore_test_failed"])
    assert "not" in str(ids["restore.failed"])


# ── freshness limit by schedule ─────────────────────────────────────────────


@pytest.mark.parametrize(("schedule", "hours"), [
    ("0 3 * * *", 24),
    ("17 4 * * *", 24),
    ("0 */6 * * *", 6),
    ("0 3,15 * * *", 12),
    ("0 3 * * 0", 168),
    ("0 3 * * 1-5", 72),
    ("0 3 1 * *", 744),
])
def test_the_longest_gap_of_a_cron_schedule(schedule: str, hours: int) -> None:
    from bay_filters import bay_cron_interval_hours
    assert bay_cron_interval_hours(schedule) == hours


@pytest.mark.parametrize(("schedule", "floor", "want"), [
    ("0 3 * * *", 30, 30),
    ("0 3 * * 0", 30, 210),
    ("0 */6 * * *", 30, 30),
    ("0 3 1 * *", 30, 930),
    ("not a schedule", 30, 30),
])
def test_the_default_freshness_limit_follows_the_schedule(schedule: str, floor: int, want: int) -> None:
    from bay_filters import bay_backup_max_age
    assert bay_backup_max_age(schedule, floor) == want


def test_resolve_uses_the_schedule_for_the_default_limit() -> None:
    text = (ROOT / "roles" / "backup" / "tasks" / "resolve.yml").read_text()
    assert text.count("bay_backup_max_age") >= 3


def _alert_id(tmp_path: Path, **variables: Any) -> str:
    play = yaml.safe_load(RESTORE.read_text())[0]
    rescue = next(t for t in play["tasks"] if "block" in t)["rescue"]
    tasks = [t for t in rescue if "_bay_alert_id" in (t.get("ansible.builtin.set_fact") or {})]
    tasks.append({"name": "Show", "ansible.builtin.debug": {"msg": "ID {{ _bay_alert_id }}"}})
    pl = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
           "vars": {"_into": "{{ restore_into | default('live') }}", "accessory": "pg",
                    "_restore_method": "pg_dump"},
           "tasks": tasks}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(pl))
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_LOCALHOST_WARNING": "0", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
           "ANSIBLE_PYTHON_INTERPRETER": sys.executable}
    proc = subprocess.run([sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
                           "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:]
    return next(line.split('"ID ')[1].rstrip('",') for line in proc.stdout.splitlines()
                if '"msg": "ID ' in line)


@pytest.mark.parametrize(("variables", "want"), [
    ({}, "restore.failed"),
    ({"restore_test": True}, "backup.restore_test_failed"),
    ({"restore_into": "scratch"}, "backup.restore_test_failed"),
])
def test_a_failed_scratch_restore_is_a_restore_test_failure(tmp_path: Path, variables, want) -> None:
    """Second review: `bay backup restore --into scratch` never touches the
    live database either, so its failure is a warning about the backup."""
    assert _alert_id(tmp_path, **variables) == want

