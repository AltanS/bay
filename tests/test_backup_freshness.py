"""bay-backup-freshness: the 6-hourly check of every backup target (2.24.0).

The script is rendered from the real template and run with bash in a temp dir.
The shared alert snippet is swapped for a stub that logs each bay_notify call,
and a fake systemctl answers is-enabled / is-active. Time is real: status files
and unit files get old timestamps, and alert state files are backdated.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _REPO_ROOT / "roles" / "backup" / "templates" / "bay-backup-freshness.sh.j2"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_H = 3600

_STUB_NOTIFY = r"""BAY_HOST="testhost"
bay_html_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
bay_notify() { printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr '\n' '|')" >> "${NOTIFY_LOG}"; }
"""

_FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
verb="$1"; unit="$2"
line=$(grep -F "${unit} " "${FAKE_TIMERS}" 2>/dev/null | head -n1)
enabled=$(echo "$line" | cut -d' ' -f2); active=$(echo "$line" | cut -d' ' -f3)
case "$verb" in
  is-enabled) echo "${enabled:-not-found}"; [ "$enabled" = enabled ] ;;
  is-active) echo "${active:-inactive}"; [ "$active" = active ] ;;
esac
"""


# `docker ps` lists the scratch containers from FAKE_SCRATCH (name<TAB>expires);
# `docker rm` is logged. The real docker is never called.
_FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$1" in
  ps) cat "${FAKE_SCRATCH}" 2>/dev/null; exit 0 ;;
  rm) shift; echo "rm $*" >> "${DOCKER_LOG}"; exit 0 ;;
esac
exit 97
"""


class Box:
    def __init__(self, root: Path):
        if not shutil.which("jq"):
            pytest.skip("jq is not installed")
        self.root = root
        self.scripts = root / "backup"
        self.status = self.scripts / "status"
        self.units = root / "units"
        for d in (self.status, self.units, root / "bin", root / "templates"):
            d.mkdir(parents=True, exist_ok=True)
        self.timers = root / "timers.txt"
        self.timers.write_text("")
        self.log = root / "notify.log"
        (root / "bin" / "systemctl").write_text(_FAKE_SYSTEMCTL)
        (root / "bin" / "systemctl").chmod(0o755)
        (root / "bin" / "docker").write_text(_FAKE_DOCKER)
        (root / "bin" / "docker").chmod(0o755)
        self.scratch = root / "scratch.txt"
        self.docker_log = root / "docker.log"
        shutil.copy(_TEMPLATE, root / "templates" / _TEMPLATE.name)
        (root / "templates" / "_notify.sh.j2").write_text(_STUB_NOTIFY)
        text = make_ansible_env(root / "templates").get_template(_TEMPLATE.name).render(
            backup_scripts_dir=str(self.scripts),
            backup_status_dir=str(self.status),
            backup_freshness_repeat_hours=24,
            backup_max_age_hours=30,
            backup_freshness_unit_dir=str(self.units),
        )
        self.script = root / "bay-backup-freshness"
        self.script.write_text(text)

    def target(self, name: str, *, script=True, timer=("enabled", "active"),
               installed_h: float = 100, max_age: int | None = None) -> None:
        manifest = self.scripts / "targets.json"
        items = json.loads(manifest.read_text()) if manifest.exists() else []
        entry = {"name": name}
        if max_age is not None:
            entry["max_age_hours"] = max_age
        items.append(entry)
        manifest.write_text(json.dumps(items))
        self.age(manifest, 200)
        if script:
            (self.scripts / f"backup-{name}.sh").write_text("#!/bin/sh\n")
        if timer:
            with self.timers.open("a") as f:
                f.write(f"bay-backup@{name}.timer {timer[0]} {timer[1]}\n")
        unit = self.units / f"bay-backup@{name}.timer"
        unit.write_text("[Timer]\n")
        self.age(unit, installed_h)

    @staticmethod
    def age(path: Path, hours: float) -> None:
        t = time.time() - hours * _H
        os.utime(path, (t, t))

    def ran(self, name: str, hours_ago: float) -> None:
        (self.status / f"{name}.json").write_text(
            json.dumps({"at": int(time.time() - hours_ago * _H), "bytes": 1, "snapshots": []})
        )

    def run(self) -> list[tuple[str, str]]:
        env = dict(os.environ, NOTIFY_LOG=str(self.log), FAKE_TIMERS=str(self.timers),
                   FAKE_SCRATCH=str(self.scratch), DOCKER_LOG=str(self.docker_log),
                   PATH=f"{self.root / 'bin'}:{os.environ['PATH']}")
        proc = subprocess.run(["bash", str(self.script)], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return self.alerts()

    def alerts(self) -> list[tuple[str, str]]:
        if not self.log.exists():
            return []
        return [tuple(l.split("\t", 1)) for l in self.log.read_text().splitlines()]


@pytest.fixture
def box(tmp_path) -> Box:
    return Box(tmp_path)


def test_the_rendered_script_parses(box):
    assert subprocess.run(["bash", "-n", str(box.script)]).returncode == 0


def test_no_manifest_is_silent(box):
    assert box.run() == []


def test_a_fresh_target_is_silent(box):
    box.target("postgres")
    box.ran("postgres", 5)
    assert box.run() == []


def test_a_target_with_no_timer_alerts_missing_once_per_day(box):
    box.target("postgres", timer=None)
    alerts = box.run()
    assert [a[0] for a in alerts] == ["backup.missing"]
    assert "postgres" in alerts[0][1] and "timer not enabled" in alerts[0][1]
    # The next check, 6 hours later, stays silent.
    assert len(box.run()) == 1
    # After the repeat interval it reminds once more.
    box.age(box.status / ".alerted-postgres", 25)
    (box.status / ".alerted-postgres").write_text(f"missing {int(time.time() - 25 * _H)}\n")
    assert [a[0] for a in box.run()] == ["backup.missing", "backup.missing"]


def test_a_missing_script_alerts(box):
    box.target("postgres", script=False)
    assert "no script" in box.run()[0][1]


def test_an_inactive_timer_alerts(box):
    box.target("postgres", timer=("enabled", "inactive"))
    assert "timer not active" in box.run()[0][1]


def test_an_old_last_run_alerts_stale_then_clears(box):
    box.target("postgres")
    box.ran("postgres", 31)
    alerts = box.run()
    assert [a[0] for a in alerts] == ["backup.stale"]
    assert len(box.run()) == 1
    box.ran("postgres", 1)
    assert len(box.run()) == 1
    assert not (box.status / ".alerted-postgres").exists()


def test_per_target_max_age(box):
    box.target("big", max_age=50)
    box.ran("big", 40)
    assert box.run() == []


def test_a_new_target_is_not_stale_before_max_age(box):
    box.target("postgres", installed_h=2)
    assert box.run() == []


def test_a_never_run_target_goes_stale_after_max_age(box):
    box.target("postgres", installed_h=40)
    alerts = box.run()
    assert [a[0] for a in alerts] == ["backup.stale"]
    assert "never" in alerts[0][1]


def test_an_upgraded_box_gets_a_grace_period_from_the_manifest(box):
    """Old timer units, no status file, a manifest just written by 2.24.0."""
    box.target("postgres", installed_h=2000)
    box.age(box.scripts / "targets.json", 1)
    assert box.run() == []


def test_a_bad_target_name_is_ignored(box):
    (box.scripts / "targets.json").write_text(json.dumps([{"name": "../etc/passwd"}]))
    assert box.run() == []


def test_a_mangled_alert_state_does_not_block_alerts(box):
    box.target("postgres", timer=None)
    (box.status / ".alerted-postgres").write_text("$(rm -rf /)\n")
    assert [a[0] for a in box.run()] == ["backup.missing"]


# ── second review: expired scratch containers ──────────────────────────────


def test_expired_scratch_containers_are_removed_with_their_volume(box: Box) -> None:
    """An interrupted restore test, or a kept container whose transient timer
    was lost in a reboot, carries bay.restore_test.expires; the 6-hourly run
    removes it with its own volume (`rm -f -v`), by name and label only."""
    now = int(time.time())
    box.scratch.write_text(
        f"bay-restore-test-shop-postgres-1\t{now - 60}\n"
        f"bay-restore-test-shop-postgres-2\t{now + 3600}\n"
        f"shop-postgres\t{now - 60}\n"
        f"bay-restore-test-blog-3\tsoon\n"
    )
    assert box.run() == []
    removed = box.docker_log.read_text().splitlines() if box.docker_log.exists() else []
    assert removed == ["rm -f -v bay-restore-test-shop-postgres-1"]

