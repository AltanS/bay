"""The deploy summary reads every reconcile pass of the run, not the last one.

A run can reconcile one host several times: the narrow headscale,
tailnet-identity and webhook passes, then deploy_stack. The hand-off wrote
``<host>.json`` for each pass, so the last pass overwrote the others. A
``--tags tailnet_identity,deploy_stack`` run recreated two containers, then the
deploy_stack pass found nothing to do, and the summary said "This deploy changed
no containers". Each pass now writes ``<host>.<pass>.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from bay_cli.healthcheck import read_touched_services

ROOT = Path(__file__).resolve().parent.parent
RECONCILE_YML = ROOT / "roles" / "container_lifecycle" / "tasks" / "reconcile.yml"

_HAND_OFF = (
    "Reset the reconcile bundle entries",
    "Ensure the control-node report directory exists",
    "Hand the reconciler report to the CLI",
)


def _report(*entries: tuple[str, str]) -> str:
    return json.dumps(
        {
            "ok": True,
            "changed": any(kind != "NoOp" for kind, _ in entries),
            "results": [
                {"kind": kind, "name": name, "status": "done", "detail": ""}
                for kind, name in entries
            ],
        }
    )


def _tasks(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in items:
        out.append(task)
        for key in ("block", "rescue", "always"):
            out.extend(_tasks(task.get(key) or []))
    return out


def _hand_off_tasks() -> list[dict[str, Any]]:
    all_tasks = _tasks(yaml.safe_load(RECONCILE_YML.read_text()))
    by_name = {t["name"]: t for t in all_tasks if "name" in t}
    return [by_name[name] for name in _HAND_OFF]


def test_every_pass_names_its_own_report_file() -> None:
    tasks = {t["name"]: t for t in _hand_off_tasks()}
    reset = tasks["Reset the reconcile bundle entries"]["ansible.builtin.set_fact"]
    assert "_reconcile_report_seq" in reset
    dest = tasks["Hand the reconciler report to the CLI"]["ansible.builtin.copy"]["dest"]
    assert "{{ _reconcile_report_seq }}" in dest
    assert "{{ inventory_hostname }}" in dest


def test_two_passes_on_one_host_leave_two_reports_and_the_summary_sees_both(tmp_path: Path) -> None:
    """Run the real hand-off tasks twice on one host, as a narrow pass then deploy_stack."""
    report_dir = tmp_path / "reports"
    play = [
        {
            "hosts": "box1",
            "gather_facts": False,
            "vars": {"bay_reconciler_report_dir": str(report_dir)},
            "tasks": [],
        }
    ]
    stdouts = [
        _report(("Recreate", "tailnet-identity"), ("Recreate", "headscale")),
        _report(("NoOp", "api"), ("NoOp", "web")),
    ]
    for stdout in stdouts:
        for task in _hand_off_tasks():
            task = dict(task)
            if task["name"] == "Reset the reconcile bundle entries":
                play[0]["tasks"].append(task)
                play[0]["tasks"].append(
                    {
                        "name": "Fake the reconciler result",
                        "ansible.builtin.set_fact": {"_reconcile_result": {"stdout": stdout}},
                    }
                )
                continue
            play[0]["tasks"].append(task)
    playbook = tmp_path / "play.yml"
    playbook.write_text(yaml.safe_dump(play))
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("localhost ansible_connection=local\nbox1 ansible_connection=local\n")
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", str(inventory), str(playbook)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    files = sorted(p.name for p in report_dir.glob("*.json"))
    assert files == ["box1.1.json", "box1.2.json"]
    touched = read_touched_services(tmp_path, report_dir=report_dir)
    assert touched == {"tailnet-identity", "headscale"}


def test_summary_unions_the_passes_of_one_host(tmp_path: Path) -> None:
    (tmp_path / "box1.1.json").write_text(_report(("Recreate", "tailnet-identity")))
    (tmp_path / "box1.2.json").write_text(_report(("NoOp", "api")))
    (tmp_path / "box1.3.json").write_text(_report(("Create", "bay-webhook"), ("NoOp", "web")))
    touched = read_touched_services(tmp_path, report_dir=tmp_path)
    assert touched == {"tailnet-identity", "bay-webhook"}


def test_one_unreadable_report_means_the_result_is_unknown(tmp_path: Path) -> None:
    """The lost file may hold a recreate. A smaller set would say a container was not touched."""
    (tmp_path / "box1.1.json").write_text(_report(("NoOp", "api")))
    (tmp_path / "box1.2.json").write_text("{not json")
    assert read_touched_services(tmp_path, report_dir=tmp_path) is None


def test_a_report_that_is_not_an_object_means_the_result_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "box1.1.json").write_text("[]")
    assert read_touched_services(tmp_path, report_dir=tmp_path) is None
