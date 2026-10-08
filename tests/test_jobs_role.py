"""Scheduled jobs on the box: the deploy_stack job script, units and pruning.

`bay compile` writes bay.toml [[jobs]] to the top-level `jobs:` map. The
deploy_stack role (tasks/jobs.yml) renders, per job whose main container runs
on the host, a script that runs the main container's image once, and a UTC
timer that starts the shared one-shot service `bay-job@.service`. A job that
is gone loses its timer and script.

The templates are rendered the way Ansible does (trim_blocks). The selection
and pruning expressions run in a real ansible-playbook, as pure set_fact tasks
lifted from the task file, so no unit is touched on the machine running the
tests.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles" / "deploy_stack"
sys.path.insert(0, str(ROOT / "filter_plugins"))
sys.path.insert(0, str(Path(__file__).parent))

from bay_filters import bay_prefix_volumes  # noqa: E402
from helpers import make_ansible_env  # noqa: E402

JOBS = {
    "shop-job-nightly": {
        "of": "shop",
        "schedule": "30 2 * * 1-5",
        "on_calendar": "Mon,Tue,Wed,Thu,Fri *-*-* 02:30:00 UTC",
        "command": "bin/report --all",
    },
    "shop-job-small": {
        "of": "shop",
        "schedule": "0 * * * *",
        "on_calendar": "*-*-* *:00:00 UTC",
        "command": "bin/tick",
        "memory": "128m",
    },
    "blog-job-sync": {
        "of": "blog",
        "schedule": "0 3 * * *",
        "on_calendar": "*-*-* 03:00:00 UTC",
        "command": "sync",
    },
}
SHOP = {
    "image": "ghcr.io/acme/shop:1",
    "env": {"clear": {"A": "1"}},
    "volumes": ["shop-data:/app/data", "/opt/bay/config/shop/x.yaml:/etc/x.yaml:ro"],
    "mem_limit": "512m",
}


def _env() -> Any:
    env = make_ansible_env(ROLE / "templates")
    env.filters["bay_prefix_volumes"] = bay_prefix_volumes
    return env


def _tasks() -> list[dict[str, Any]]:
    return yaml.safe_load((ROLE / "tasks" / "jobs.yml").read_text())


def _task(name: str) -> dict[str, Any]:
    return next(t for t in _tasks() if t["name"] == name)


def _render_script(job: str, main: dict[str, Any]) -> str:
    entry = JOBS[job]
    memory = entry.get("memory", main.get("mem_limit", ""))
    return _env().get_template("bay-job.sh.j2").render(
        ansible_managed="managed", job_name=job, job=entry, job_main=main, job_memory=memory,
        stack_dir="/opt/bay", stack_name="bay", traefik_docker_network="services",
    )


def _run_set_facts(tmp_path: Path, names: list[str], variables: dict[str, Any]) -> dict[str, Any]:
    """Run the named set_fact tasks of jobs.yml in a real playbook; return the facts."""
    tasks = [_task(n) for n in names]
    out = tmp_path / "facts.json"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "tasks": [*tasks, {
            "name": "Write the facts",
            "ansible.builtin.copy": {
                "content": "{{ {'jobs': _bay_jobs, 'gone': _bay_jobs_gone | default(None)} | to_json }}",
                "dest": str(out), "mode": "0600",
            },
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "vars.json").write_text(json.dumps(variables))
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
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
         "-e", f"@{tmp_path / 'vars.json'}", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return json.loads(out.read_text())


def test_job_units_render_and_prune(tmp_path: Path) -> None:
    # ── the script: the main image, env file, network, mounts, memory, command
    script = _render_script("shop-job-small", SHOP)
    path = tmp_path / "job.sh"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    assert "docker inspect --format '{{.Config.Image}}' shop" in script
    run = script[script.index("exec docker run"):]
    words = shlex.split(run.replace("\\\n", " "))
    assert words[:4] == ["exec", "docker", "run", "--rm"]
    assert words[words.index("--name") + 1] == "shop-job-small"
    assert words[words.index("--network") + 1] == "services"
    assert words[words.index("--env-file") + 1] == "/opt/bay/env/shop.env"
    assert ["-v", "bay_shop-data:/app/data"] == words[words.index("-v"):words.index("-v") + 2]
    assert words[words.index("--memory") + 1] == "128m"  # the job's own cap wins
    assert words[-5:] == ["--entrypoint", "/bin/sh", "${IMAGE}", "-c", "bin/tick"]
    assert "-p" not in words and not any(w.startswith("traefik.") for w in words)
    # Without a job cap, the main container's cap applies.
    nightly = _render_script("shop-job-nightly", SHOP)
    assert "--memory 512m" in nightly and "-c 'bin/report --all'" in nightly

    # ── the units: one-shot service, UTC timer per job
    env = _env()
    service = env.get_template("bay-job@.service.j2").render(
        ansible_managed="managed", app_user="bay", stack_dir="/opt/bay")
    assert "Type=oneshot" in service
    assert "ExecStart=/opt/bay/jobs/%i.sh" in service
    assert "User=bay" in service
    timer = env.get_template("bay-job@.timer.j2").render(
        ansible_managed="managed", job_name="shop-job-nightly", job=JOBS["shop-job-nightly"])
    assert "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 02:30:00 UTC" in timer
    assert "Unit=bay-job@shop-job-nightly.service" in timer

    # ── selection and pruning, in a real playbook
    facts = _run_set_facts(
        tmp_path,
        ["Select the jobs whose container runs on this host", "Name the jobs that are gone"],
        {
            "jobs": JOBS,
            "active_services": {"shop": SHOP},
            "active_accessories": {"redis": {"image": "redis:7"}},
            "_bay_job_timer_files": {"files": [
                {"path": "/etc/systemd/system/bay-job@shop-job-nightly.timer"},
                {"path": "/etc/systemd/system/bay-job@shop-job-old.timer"},
            ]},
            "_bay_job_script_files": {"files": [
                {"path": "/opt/bay/jobs/shop-job-small.sh"},
                {"path": "/opt/bay/jobs/blog-job-sync.sh"},
            ]},
        },
    )
    # blog does not run here: its job is not installed, and its old script goes.
    assert sorted(facts["jobs"]) == ["shop-job-nightly", "shop-job-small"]
    assert facts["gone"] == ["blog-job-sync", "shop-job-old"]

    # Without a `jobs:` map nothing is selected and nothing is installed.
    empty = _run_set_facts(
        tmp_path, ["Select the jobs whose container runs on this host"],
        {"active_services": {"shop": SHOP}},
    )
    assert empty["jobs"] == {}

    # ── the prune tasks act on the gone list; everything in /etc runs as root
    tasks = {t["name"]: t for t in _tasks()}
    for name in ("Stop and disable the timers of jobs that are gone",
                 "Remove the timers of jobs that are gone",
                 "Remove the scripts of jobs that are gone"):
        assert tasks[name]["loop"] == "{{ _bay_jobs_gone }}"
    assert tasks["Stop and disable the timers of jobs that are gone"]["ansible.builtin.systemd"] == {
        "name": "bay-job@{{ item }}.timer", "state": "stopped", "enabled": False}
    for task in _tasks():
        module = next(k for k in task if k.startswith("ansible.builtin."))
        args = task[module] if isinstance(task[module], dict) else {}
        dest = str(args.get("dest") or args.get("path") or "")
        if dest.startswith("/etc") or module == "ansible.builtin.systemd":
            assert task.get("become") is True and task.get("become_user") == "root", task["name"]
    start = tasks["Enable and start the job timers"]
    assert start["when"] == "not ansible_check_mode"


def test_jobs_tasks_are_included_by_deploy_stack() -> None:
    main = (ROLE / "tasks" / "main.yml").read_text()
    assert "ansible.builtin.include_tasks: jobs.yml" in main


def _run_script_scan(tmp_path: Path, stack_dir: Path) -> tuple[str, list[str]]:
    """Run the real stat, find and name-the-gone tasks; return the output and the gone jobs."""
    names = ("Check for the job script directory on this host", "Find the job scripts on this host",
             "Name the jobs that are gone")
    out = tmp_path / "gone.json"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"stack_dir": str(stack_dir), "_bay_jobs": {"shop-job-nightly": {}},
                 "_bay_job_timer_files": {"files": []}},
        "tasks": [*(_task(n) for n in names), {
            "name": "Write the result",
            "ansible.builtin.copy": {"content": "{{ _bay_jobs_gone | to_json }}", "dest": str(out)},
        }],
    }]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
           "ANSIBLE_PYTHON_INTERPRETER": sys.executable, "ANSIBLE_LOCALHOST_WARNING": "0",
           "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0"}
    proc = subprocess.run(
        [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,", str(tmp_path / "play.yml")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout + proc.stderr, json.loads(out.read_text())


def test_a_box_without_a_jobs_directory_scans_quietly(tmp_path: Path) -> None:
    """find warned on every deploy of a box that never ran a job."""
    output, gone = _run_script_scan(tmp_path, tmp_path / "stack")
    assert "WARNING" not in output, output
    assert gone == []


def test_the_scan_still_names_the_scripts_of_jobs_that_are_gone(tmp_path: Path) -> None:
    jobs = tmp_path / "stack" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "shop-job-nightly.sh").write_text("#!/bin/sh\n")
    (jobs / "shop-job-old.sh").write_text("#!/bin/sh\n")
    output, gone = _run_script_scan(tmp_path, tmp_path / "stack")
    assert "WARNING" not in output
    assert gone == ["shop-job-old"]
