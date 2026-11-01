"""`common_mptcp_enabled` sets net.mptcp.enabled through its own sysctl file.

Context: CVE-2026-98432. The public exploit needs an MPTCP listener. The task
keeps its own tag so a run can change this one setting and nothing else.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROLE = Path(__file__).parent.parent / "roles" / "common"


def _task() -> dict:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text())
    return next(t for t in tasks if t["name"] == "Set Multipath TCP on or off")


def test_default_is_off() -> None:
    defaults = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["common_mptcp_enabled"] is False


def test_task_survives_a_reboot_and_applies_now() -> None:
    sysctl = _task()["ansible.posix.sysctl"]
    assert sysctl["name"] == "net.mptcp.enabled"
    assert sysctl["sysctl_file"] == "/etc/sysctl.d/99-bay-mptcp.conf"
    assert sysctl["sysctl_set"] is True
    assert sysctl["reload"] is True


def test_task_has_its_own_tag() -> None:
    assert "mptcp" in _task()["tags"]
