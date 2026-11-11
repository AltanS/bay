"""bay-reboot-alert: the pending reboot probe.

The script is rendered from the real template and run with bash against a temp
dir. Only the shared alert snippet is swapped for a stub that appends each
bay_notify call to a log, so a test sees exactly which alerts would go out.
Time is real: the fake reboot-required file gets an old mtime with os.utime,
and the state file is backdated to move past the repeat interval.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLE = _REPO_ROOT / "roles" / "outbound_monitor"
_TEMPLATE = _ROLE / "templates" / "bay-reboot-alert.sh.j2"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers import make_ansible_env  # noqa: E402

_DAY = 86400

# Stand-in for roles/alert_channel/templates/_notify.sh.j2. Same two names, same
# arguments; it records instead of sending.
_STUB_NOTIFY = r"""BAY_HOST="testhost"
bay_html_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
bay_notify() { printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr '\n' '|')" >> "${NOTIFY_LOG}"; }
"""


class Probe:
    def __init__(self, root: Path):
        self.root = root
        self.state_dir = root / "state"
        self.flag = root / "reboot-required"
        self.pkgs = root / "reboot-required.pkgs"
        self.log = root / "notify.log"
        self.state = self.state_dir / "reboot-alert.state"
        templates = root / "templates"
        templates.mkdir()
        shutil.copy(_TEMPLATE, templates / _TEMPLATE.name)
        (templates / "_notify.sh.j2").write_text(_STUB_NOTIFY)
        self.script = root / "bay-reboot-alert"
        self.render()

    def render(self, threshold: int = 7, repeat: int = 7) -> None:
        env = make_ansible_env(self.root / "templates")
        text = env.get_template(_TEMPLATE.name).render(
            outbound_check_state_dir=str(self.state_dir),
            reboot_alert_flag_file=str(self.flag),
            reboot_alert_threshold_days=threshold,
            reboot_alert_repeat_days=repeat,
        )
        self.script.write_text(text)

    def flag_age(self, days: float, pkgs: str | None = None) -> None:
        self.flag.write_text("*** System restart required ***\n")
        mtime = time.time() - days * _DAY
        os.utime(self.flag, (mtime, mtime))
        if pkgs is not None:
            self.pkgs.write_text(pkgs)

    def run(self) -> subprocess.CompletedProcess:
        env = dict(os.environ, NOTIFY_LOG=str(self.log))
        proc = subprocess.run(
            ["bash", str(self.script)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc

    def alerts(self) -> list[tuple[str, str]]:
        if not self.log.exists():
            return []
        return [
            tuple(line.split("\t", 1)) for line in self.log.read_text().splitlines()
        ]


@pytest.fixture
def probe(tmp_path) -> Probe:
    return Probe(tmp_path)


def test_rendered_script_parses(probe):
    proc = subprocess.run(
        ["bash", "-n", str(probe.script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_no_flag_file_is_silent(probe):
    probe.run()
    assert probe.alerts() == []
    assert not probe.state.exists()


def test_young_flag_file_is_silent(probe):
    probe.flag_age(2)
    probe.run()
    assert probe.alerts() == []
    assert not probe.state.exists()


def test_old_flag_file_sends_one_alert_with_the_details(probe):
    probe.flag_age(30, pkgs="linux-image-6.8.0-99-generic\nlibc6\n")
    probe.run()
    alerts = probe.alerts()
    assert len(alerts) == 1
    alert_id, message = alerts[0]
    assert alert_id == "host.reboot_pending"
    assert "testhost" in message
    assert "<code>30</code> days" in message
    assert "libc6, linux-image-6.8.0-99-generic" in message
    assert "Kernel: running <code>" in message
    assert probe.state.read_text().strip().isdigit()


def test_package_list_is_cut_to_five_with_a_count(probe):
    probe.flag_age(10, pkgs="".join(f"pkg{n}\n" for n in range(8)))
    probe.run()
    (_, message), = probe.alerts()
    assert "pkg0, pkg1, pkg2, pkg3, pkg4" in message
    assert "pkg5" not in message
    assert "(+3 more)" in message


def test_missing_package_list_still_alerts(probe):
    probe.flag_age(10)
    probe.run()
    (_, message), = probe.alerts()
    assert "(not listed)" in message


def test_package_names_are_escaped(probe):
    probe.flag_age(10, pkgs="<b>evil</b>&co\n")
    probe.run()
    (_, message), = probe.alerts()
    assert "&lt;b&gt;evil&lt;/b&gt;&amp;co" in message
    assert "<b>evil</b>" not in message


def test_second_run_inside_the_repeat_interval_is_silent(probe):
    probe.flag_age(30)
    probe.run()
    probe.run()
    assert len(probe.alerts()) == 1


def test_run_after_the_repeat_interval_alerts_again(probe):
    probe.flag_age(30)
    probe.run()
    probe.state.write_text(f"{int(time.time()) - 8 * _DAY}\n")
    probe.run()
    assert len(probe.alerts()) == 2
    assert int(probe.state.read_text()) >= int(time.time()) - 5


def test_run_just_inside_the_repeat_interval_stays_silent(probe):
    probe.flag_age(30)
    probe.run()
    probe.state.write_text(f"{int(time.time()) - 6 * _DAY}\n")
    probe.run()
    assert len(probe.alerts()) == 1


def test_flag_file_gone_clears_the_state(probe):
    probe.flag_age(30)
    probe.run()
    assert probe.state.exists()
    probe.flag.unlink()
    probe.run()
    assert not probe.state.exists()
    assert len(probe.alerts()) == 1
    # A new pending reboot starts a fresh cycle.
    probe.flag_age(30)
    probe.run()
    assert len(probe.alerts()) == 2


def test_garbage_state_file_does_not_block_the_alert(probe):
    probe.flag_age(30)
    probe.state_dir.mkdir()
    probe.state.write_text("not a number\n")
    probe.run()
    assert len(probe.alerts()) == 1


def test_threshold_and_repeat_come_from_the_variables(probe):
    probe.render(threshold=40, repeat=1)
    probe.flag_age(30)
    probe.run()
    assert probe.alerts() == []
    probe.render(threshold=3, repeat=1)
    probe.run()
    assert len(probe.alerts()) == 1
    probe.state.write_text(f"{int(time.time()) - 2 * _DAY}\n")
    probe.run()
    assert len(probe.alerts()) == 2


# ── Wiring ───────────────────────────────────────────────────────────────


def test_call_site_passes_a_literal_registered_id():
    registry = yaml.safe_load((_REPO_ROOT / "alerts" / "registry.yml").read_text())
    assert registry["host.reboot_pending"]["level"] == "warn"
    assert registry["host.reboot_pending"]["enabled_by_default"] is True
    assert re.search(
        r"^bay_notify host\.reboot_pending ", _TEMPLATE.read_text(), re.MULTILINE
    )


def test_defaults_are_declared():
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["reboot_alert_enabled"] is True
    assert defaults["reboot_alert_threshold_days"] == 7
    assert defaults["reboot_alert_repeat_days"] == 7


def test_role_is_provisioned_not_deployed():
    """Tells the changelog what to say: the role runs from provision.yml only."""
    provision = (_REPO_ROOT / "provision.yml").read_text()
    deploy = (_REPO_ROOT / "deploy.yml").read_text()
    assert "role: outbound_monitor" in provision
    assert "role: outbound_monitor" not in deploy


def test_timer_is_daily():
    timer = (_ROLE / "templates" / "bay-reboot-alert.timer.j2").read_text()
    assert "OnCalendar=*-*-* " in timer
    assert "Persistent=true" in timer
