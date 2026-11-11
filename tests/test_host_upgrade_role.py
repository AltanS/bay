"""roles/host_upgrade, run in a real ansible-playbook against localhost with fakes.

Every command the role calls on the box (apt-get, systemd-run, systemctl,
docker, dpkg, nft, iptables, tailscale, date, uname) is a fake on PATH that
logs its call and reads its answers from one JSON state file. `systemd-run`
runs its `/bin/sh -c` script at once, so the detached unit, the rc file and
the poll run as on a box. What a fake cannot show: a real apt, a real reboot,
a dropped SSH session, real Docker and nftables. docs/host-upgrade.md lists
those for the live trial.

Also here: the guard that deploy.yml and provision.yml include.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

FAKE = r'''#!PYTHON
import json, os, subprocess, sys
state_path = os.environ["FAKE_STATE"]
with open(state_path) as fh:
    st = json.load(fh)
cmd = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(cmd + " " + " ".join(args) + "\n")

def save():
    with open(state_path, "w") as fh:
        json.dump(st, fh)

def out(text, rc=0):
    if text:
        print(text)
    sys.exit(rc)

if cmd == "date":
    out(str(st["now"]))
if cmd == "uname":
    out(st["kernel"])
if cmd == "apt-get":
    if "-s" in args:
        out("\n".join(f"Inst {p} [1.0] (2.0 Debian:12/stable [amd64])" for p in st["packages"]))
    if "update" in args:
        out("")
    if "-f" in args and "install" in args:
        out("fixed", st.get("fix_rc", 0))
    if "dist-upgrade" in args:
        if st.get("apt_flushes"):
            st["chains"] = False
            st["nft_started"] = ["999"]
        st["upgraded"] = True
        rcs = st.get("apt_rc", 0)
        if isinstance(rcs, list):
            rc = rcs[0] if len(rcs) == 1 else rcs.pop(0)
        else:
            rc = rcs
        save()
        print("upgraded " + " ".join(st["packages"]))
        sys.exit(rc)
    out("")
if cmd == "systemd-run":
    script = args[args.index("/bin/sh") + 2]
    rc = subprocess.run(["/bin/sh", "-c", script]).returncode
    out("", rc)
if cmd == "systemctl":
    verb = args[0]
    if verb == "is-active":
        name = args[1].removesuffix(".service")
        if name in st["active"]:
            out("active")
        out("inactive", 3)
    if verb == "list-unit-files":
        name = args[1].removesuffix(".service")
        if name in st["installed"]:
            out(f"{name}.service enabled enabled")
        out("", 1)
    if verb == "show":
        out(st["nft_started"][0])
    if verb == "restart":
        if args[1] == "docker.service":
            st["chains"] = True
            save()
        out("")
    out("")
if cmd == "docker":
    if args[0] == "inspect":
        lines = []
        for name in args[3:]:
            counts = st.setdefault("restarts", {}).get(name, [0])
            count = counts[0] if len(counts) == 1 else counts.pop(0)
            lines.append(f"/{name}\t{count}\t{st.get('states', {}).get(name, 'running')}")
        save()
        out("\n".join(lines))
    if args[0] == "logs":
        out(f"{args[-1]}: listen tcp 100.64.0.1:443: bind: cannot assign requested address")
    if args[0] == "info":
        out("ok", 0 if "docker" in st["active"] else 1)
    if args[0] == "ps":
        fmt = args[args.index("--format") + 1]
        names = st["after"] if st.get("upgraded") else st["before"]
        if "Status" in fmt:
            out("\n".join(f"{n}\t{st.get('status', {}).get(n, 'Up 2 hours')}" for n in names))
        if "Ports" in fmt:
            out("\n".join(st.get("ports", [])))
        out("\n".join(names))
    out("")
if cmd == "dpkg":
    if "--audit" in args:
        out(st.get("audit", ""))
    out("")
if cmd == "cscli":
    out("You can successfully interact with Local API (LAPI)", st.get("lapi_rc", 0))
if cmd == "nft":
    if args[:2] == ["list", "tables"]:
        out("\n".join(st["tables"]))
    if args[0] == "-f":
        st["reloaded"] = True
        save()
    out("")
if cmd == "iptables":
    out("Chain DOCKER", 0 if st["chains"] else 1)
if cmd == "tailscale":
    if args[0] == "ip":
        out(st.get("tailscale_ip", "100.64.0.1"))
    out(json.dumps({"BackendState": "Running"}))
out("")
'''

FAKES = ("date", "uname", "apt-get", "systemd-run", "systemctl", "docker", "dpkg", "nft",
         "iptables", "tailscale", "cscli")


def _state(**over: Any) -> dict[str, Any]:
    base = {
        "now": 1_800_000_000,
        "kernel": "6.1.0-25-amd64",
        "packages": ["docker-ce", "tailscale"],
        "before": ["api", "postgres"],
        "after": ["api", "postgres"],
        "active": ["docker", "nftables", "crowdsec", "crowdsec-firewall-bouncer", "tailscaled"],
        "installed": ["nftables", "crowdsec", "crowdsec-firewall-bouncer", "tailscaled"],
        "nft_started": ["100"],
        "tables": ["table inet filter", "table ip crowdsec"],
        "chains": True,
    }
    base.update(over)
    return base


class Box:
    """A fake box in ``tmp``: PATH fakes, a stack dir, a modules dir, a state file."""

    def __init__(self, tmp: Path, state: dict[str, Any]) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        script = FAKE.replace("#!PYTHON", f"#!{sys.executable}")
        for name in FAKES:
            path = self.bin / name
            path.write_text(script)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.state_path = tmp / "state.json"
        self.state_path.write_text(json.dumps(state))
        self.log = tmp / "calls.log"
        self.log.write_text("")
        self.stack = tmp / "stack"
        self.stack.mkdir()
        self.lock = self.stack / ".deploy.lock"
        modules = tmp / "modules"
        (modules / state["kernel"]).mkdir(parents=True)
        self.result = tmp / "result.json"
        self.vars = {
            "stack_dir": str(self.stack),
            "app_user": pwd.getpwuid(os.getuid()).pw_name,
            "app_user_group": grp.getgrgid(os.getgid()).gr_name,
            "host_upgrade_state_dir": str(tmp / "state"),
            "host_upgrade_state_owner": pwd.getpwuid(os.getuid()).pw_name,
            "host_upgrade_log": str(tmp / "upgrade.log"),
            "host_upgrade_modules_dir": str(modules),
            "host_upgrade_reboot_required_file": str(tmp / "reboot-required"),
            "host_upgrade_nftables_dir": str(tmp / "nftables.d"),
            "host_upgrade_nftables_conf": str(tmp / "nftables.conf"),
            "host_upgrade_result_path": str(self.result),
            "host_upgrade_box": "alpha",
            "host_upgrade_poll_delay": 1,
            "host_upgrade_poll_retries": 3,
            # Two poll rounds; production has five of ten minutes each.
            "host_upgrade_timeout": 3,
            "host_upgrade_container_wait": 2,
            "host_upgrade_container_poll": 1,
            "host_upgrade_restart_window": 1,
        }

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def state(self) -> dict[str, Any]:
        return json.loads(self.state_path.read_text())

    def run(self, play: dict[str, Any], *extra: str) -> tuple[int, str]:
        inventory = {"all": {"hosts": {"alpha-host": {
            "ansible_connection": "local",
            "ansible_python_interpreter": sys.executable,
            "ansible_become": False,
        }}}}
        (self.tmp / "inventory.yml").write_text(yaml.safe_dump(inventory))
        (self.tmp / "play.yml").write_text(yaml.safe_dump([play]))
        (self.tmp / "ansible.cfg").write_text("[defaults]\n")
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "ANSIBLE_CONFIG": str(self.tmp / "ansible.cfg"),
            "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
            "ANSIBLE_NOCOLOR": "1",
            "FAKE_STATE": str(self.state_path),
            "FAKE_LOG": str(self.log),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "ansible.cli.playbook", "-i", str(self.tmp / "inventory.yml"),
             str(self.tmp / "play.yml"), *extra],
            cwd=self.tmp, env=env, capture_output=True, text=True, timeout=300,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def upgrade(self, *extra: str, **over: Any) -> tuple[int, str, dict[str, Any] | None]:
        play = {"hosts": "all", "gather_facts": False, "vars": {**self.vars, **over},
                "roles": ["host_upgrade"]}
        code, out = self.run(play, *extra)
        result = json.loads(self.result.read_text()) if self.result.exists() else None
        return code, out, result


@pytest.fixture
def make_box(tmp_path: Path):
    def make(**over: Any) -> Box:
        return Box(tmp_path, _state(**over))

    return make


def _index(calls: list[str], prefix: str) -> int:
    for i, call in enumerate(calls):
        if call.startswith(prefix):
            return i
    raise AssertionError(f"no call {prefix!r} in {calls}")


def test_a_clean_upgrade_runs_detached_and_checks_the_box(make_box) -> None:
    box = make_box()
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result is not None
    assert result["ok"] is True and result["reason"] is None and result["dry_run"] is False
    assert result["packages"] == ["docker-ce", "tailscale"]
    assert result["packages_upgraded"] == 2
    assert result["kernel"] == {"before": "6.1.0-25-amd64", "after": "6.1.0-25-amd64",
                                "newest_installed": "6.1.0-25-amd64"}
    assert result["reboot_required"] is False and result["rebooted"] is False
    assert result["containers"]["before"] == ["api", "postgres"]
    assert result["containers"]["missing"] == []
    assert {c["name"] for c in result["checks"]} >= {
        "docker", "containers", "container_restarts", "nftables", "nftables_tables", "docker_chains",
        "crowdsec", "crowdsec_bouncer", "tailscale",
    }
    assert all(c["ok"] for c in result["checks"]), result["checks"]
    calls = box.calls()
    run = _index(calls, "systemd-run --unit=bay-host-upgrade --collect")
    # apt runs only inside the detached unit, never in the session itself.
    assert [c for c in calls[:run] if c.startswith("apt-get") and "-y" in c] == []
    assert "--force-confold" in box.log.read_text()
    assert _index(calls, "dpkg --force-confdef --force-confold --configure -a") > run
    # No flush seen: the firewall is left alone.
    assert not any(c.startswith("nft -f") for c in calls)
    assert not any(c.startswith("systemctl restart docker") for c in calls)
    assert result["firewall"]["repaired"] is False
    # The lock was taken and released.
    assert not box.lock.exists()


def test_the_lock_is_held_while_apt_runs(make_box, tmp_path: Path) -> None:
    box = make_box()
    seen = tmp_path / "seen-lock"
    # apt-get copies the lock it finds: the upgrade must hold it at that moment.
    real = (box.bin / "apt-get").read_text()
    (box.bin / "apt-get").write_text(real.replace(
        'if "dist-upgrade" in args:',
        f'if "dist-upgrade" in args and "-y" in args:\n'
        f'        import shutil; shutil.copy({str(box.lock)!r}, {str(seen)!r})\n'
        f'    if "dist-upgrade" in args:', 1))
    code, out, result = box.upgrade()
    assert code == 0, out
    holder = json.loads(seen.read_text())
    assert holder["kind"] == "host_upgrade" and holder["unit"] == "bay-host-upgrade"
    assert holder["started_at"] == "2027-01-15T08:00:00Z"
    assert not box.lock.exists()


def test_a_flushed_firewall_is_loaded_then_docker_restarts(make_box) -> None:
    box = make_box(packages=["nftables", "docker-ce"], apt_flushes=True)
    code, out, result = box.upgrade()
    assert code == 0, out
    calls = box.calls()
    reload_at = _index(calls, f"nft -f {box.vars['host_upgrade_nftables_conf']}")
    docker_at = _index(calls, "systemctl restart docker.service")
    bouncer_at = _index(calls, "systemctl restart crowdsec-firewall-bouncer.service")
    # Order matters: the Bay firewall first, Docker after it, then the bouncer.
    assert reload_at < docker_at < bouncer_at
    assert _index(calls, "dpkg --force-confdef") < reload_at
    assert result["firewall"]["repaired"] is True
    assert set(result["firewall"]["why"]) == {
        "the nftables package was upgraded", "nftables.service started again",
        "a Docker chain is missing",
    }
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["docker_chains"]["ok"] is True


def test_a_missing_docker_chain_alone_starts_the_repair(make_box) -> None:
    box = make_box(packages=["curl"], chains=False)
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result["firewall"]["why"] == ["a Docker chain is missing"]
    assert _index(box.calls(), "nft -f") < _index(box.calls(), "systemctl restart docker.service")


def test_a_fresh_deploy_lock_refuses_and_is_kept(make_box) -> None:
    box = make_box()
    held = '{"user": "ci", "started_at": "2027-01-15T07:55:00Z"}\n'
    box.lock.write_text(held)
    os.utime(box.lock, (1_800_000_000 - 300, 1_800_000_000 - 300))
    code, out, result = box.upgrade()
    assert code != 0
    assert result is not None and result["ok"] is False
    assert result["reason"] == "box_locked"
    assert "a deploy by ci since 2027-01-15T07:55:00Z" in result["error"]
    assert box.lock.read_text() == held
    assert not any(c.startswith("apt-get") or c.startswith("systemd-run") for c in box.calls())


def test_a_stale_deploy_lock_is_taken_over(make_box) -> None:
    box = make_box()
    box.lock.write_text('{"user": "ci", "started_at": "2027-01-15T05:00:00Z"}\n')
    os.utime(box.lock, (1_800_000_000 - 7200, 1_800_000_000 - 7200))
    code, out, result = box.upgrade()
    assert code == 0, out
    assert not box.lock.exists()


def test_a_running_upgrade_unit_refuses(make_box) -> None:
    box = make_box()
    state = box.state()
    state["active"].append("bay-host-upgrade")
    box.state_path.write_text(json.dumps(state))
    code, out, result = box.upgrade()
    assert code != 0
    assert result["reason"] == "upgrade_running"
    assert not box.lock.exists()
    assert not any(c.startswith("systemd-run") for c in box.calls())


def test_check_mode_changes_nothing(make_box) -> None:
    box = make_box(packages=["nftables"], apt_flushes=True)
    code, out, result = box.upgrade("--check")
    assert code == 0, out
    assert result["dry_run"] is True
    assert result["packages"] == ["nftables"]
    assert result["packages_upgraded"] == 0
    calls = box.calls()
    for forbidden in ("systemd-run", "apt-get -q -o", "dpkg --force", "nft -f", "systemctl restart"):
        assert not any(c.startswith(forbidden) for c in calls), (forbidden, calls)
    assert not box.lock.exists()
    assert not (box.tmp / "state").exists()
    assert box.state().get("upgraded") is None


def test_a_failed_first_pass_gets_one_repair_pass(make_box) -> None:
    # The live trial: the bouncer's postinst raced a crowdsec restart, apt
    # exited 100. The repair pass waits for crowdsec, finishes dpkg, runs
    # `apt-get -f install` and one more dist-upgrade.
    box = make_box(apt_rc=[100, 0])
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result["ok"] is True
    assert result["apt_repair"] is True
    assert result["apt_first_rc"] == 100 and result["apt_rc"] == 0
    assert result["packages_upgraded"] == 2
    calls = box.calls()
    first = _index(calls, "apt-get -q -y")
    lapi = _index(calls, "cscli lapi status")
    configure = _index(calls, "dpkg --force-confdef --force-confold --configure -a")
    fix = next(i for i, c in enumerate(calls) if c.startswith("apt-get") and "-f install" in c)
    second = max(i for i, c in enumerate(calls) if c.startswith("apt-get") and "dist-upgrade" in c)
    assert first < lapi < configure < fix < second
    assert sum(1 for c in calls if c.startswith("systemd-run")) == 2
    log = Path(box.vars["host_upgrade_log"]).read_text()
    assert "== bay host upgrade repair pass" in log
    assert log.count("upgraded docker-ce tailscale") == 2  # one log, appended
    assert not box.lock.exists()


def test_a_clean_first_pass_runs_no_repair(make_box) -> None:
    box = make_box()
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result["apt_repair"] is False and result["apt_first_rc"] == 0
    assert not any(c.startswith("cscli") or "-f install" in c for c in box.calls())


def test_without_crowdsec_the_repair_does_not_wait_for_it(make_box) -> None:
    box = make_box(apt_rc=[100, 0], installed=["nftables", "tailscaled"],
                   active=["docker", "nftables", "tailscaled"])
    code, out, result = box.upgrade()
    assert result["apt_repair"] is True and result["apt_rc"] == 0
    assert not any(c.startswith("cscli") for c in box.calls())


def test_a_failed_repair_still_runs_the_firewall_and_the_checks(make_box) -> None:
    box = make_box(apt_rc=100, packages=["nftables"], apt_flushes=True)
    code, out, result = box.upgrade()
    assert code != 0
    assert result["ok"] is False and result["reason"] == "failed"
    assert result["failed_task"] == "Fail when apt-get failed"
    assert "exited 100, the repair pass exited 100" in result["error"]
    assert result["apt_repair"] is True
    assert result["apt_first_rc"] == 100 and result["apt_rc"] == 100
    assert result["packages_upgraded"] == 0
    # The firewall repair and the checks ran after the failure.
    assert result["firewall"]["repaired"] is True
    assert {c["name"] for c in result["checks"]} >= {"docker", "containers", "nftables"}
    assert result["containers"]["after"] == ["api", "postgres"]
    calls = box.calls()
    assert _index(calls, "nft -f") < _index(calls, "systemctl restart docker.service")
    assert not box.lock.exists()


def test_a_dirty_dpkg_audit_fails_the_apt_step(make_box) -> None:
    box = make_box(audit="The following packages are only half configured:\n crowdsec-firewall-bouncer-nftables")
    code, out, result = box.upgrade()
    assert code != 0
    assert result["failed_task"] == "Fail when apt-get failed"
    assert "dpkg --audit lists packages" in result["error"]
    assert result["dpkg_audit"][0].startswith("The following packages")
    assert result["checks"] and result["containers"]["after"]


def test_no_reboot_when_the_apt_step_failed(make_box) -> None:
    box = make_box(apt_rc=100)
    (Path(box.vars["host_upgrade_modules_dir"]) / "6.1.0-26-amd64").mkdir()
    code, out, result = box.upgrade(host_upgrade_reboot=True)
    assert code != 0
    assert result["reboot_required"] is True and result["rebooted"] is False
    assert "Not rebooted: the apt step failed" in result["reboot_note"]
    assert result["failed_task"] == "Fail when apt-get failed"


def test_a_check_failure_after_a_clean_apt_names_the_check(make_box) -> None:
    box = make_box(apt_rc=[100, 0], after=["api"])
    code, out, result = box.upgrade()
    assert code != 0
    assert result["failed_task"] == "Fail when a check failed"
    assert result["apt_repair"] is True


def test_a_container_that_does_not_come_back_fails_the_check(make_box) -> None:
    box = make_box(after=["api"])
    code, out, result = box.upgrade()
    assert code != 0
    assert result["reason"] == "failed"
    assert result["failed_task"] == "Fail when a check failed"
    assert "Check failed: containers" in result["error"]
    assert result["containers"]["missing"] == ["postgres"]
    assert not box.lock.exists()


def test_an_unhealthy_container_fails_the_check(make_box) -> None:
    box = make_box(status={"api": "Up 3 minutes (unhealthy)"})
    code, out, result = box.upgrade()
    assert code != 0
    assert result["containers"]["unhealthy"] == ["api"]


def test_a_newer_kernel_needs_a_reboot_and_says_so(make_box) -> None:
    box = make_box()
    (Path(box.vars["host_upgrade_modules_dir"]) / "6.1.0-26-amd64").mkdir()
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result["reboot_required"] is True and result["rebooted"] is False
    assert result["kernel"]["newest_installed"] == "6.1.0-26-amd64"
    assert "A reboot is pending" in out


def test_nothing_to_upgrade_skips_the_unit(make_box) -> None:
    box = make_box(packages=[])
    code, out, result = box.upgrade()
    assert code == 0, out
    assert result["packages_upgraded"] == 0
    assert not any(c.startswith("systemd-run") for c in box.calls())


def test_the_egress_tables_are_checked_when_their_file_exists(make_box) -> None:
    box = make_box()
    nft_dir = Path(box.vars["host_upgrade_nftables_dir"])
    nft_dir.mkdir()
    (nft_dir / "bay-egress.conf").write_text("")
    code, out, result = box.upgrade()
    assert code != 0
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["nftables_tables"]["ok"] is False
    assert "table inet bay_egress" in checks["nftables_tables"]["detail"]


# ── the guard of deploy.yml and provision.yml ───────────────────────────────


def _guard(box: Box) -> tuple[int, str]:
    play = {
        "hosts": "all", "gather_facts": False,
        "vars": {"stack_dir": box.vars["stack_dir"]},
        "tasks": [
            {"name": "guard", "ansible.builtin.include_role": {"name": "host_upgrade",
                                                              "tasks_from": "guard"}},
            {"name": "after the guard", "ansible.builtin.debug": {"msg": "deploy goes on"}},
        ],
    }
    return box.run(play)


@pytest.mark.parametrize(
    ("lock", "age", "unit", "blocked"),
    [
        (None, 0, False, False),
        ({"kind": "host_upgrade", "user": "op", "started_at": "T"}, 60, False, True),
        ({"kind": "host_upgrade", "user": "op", "started_at": "T"}, 7200, False, False),
        ({"kind": "host_upgrade", "user": "op", "started_at": "T"}, 7200, True, True),
        ({"user": "ci", "started_at": "T"}, 60, False, False),
        (None, 0, True, True),
    ],
    ids=["free", "fresh-upgrade", "stale-upgrade", "stale-but-unit-runs", "deploy-lock", "unit-only"],
)
def test_the_guard_stops_a_deploy_while_an_upgrade_runs(
    make_box, lock: dict[str, Any] | None, age: int, unit: bool, blocked: bool
) -> None:
    box = make_box()
    if lock is not None:
        box.lock.write_text(json.dumps(lock))
        os.utime(box.lock, (1_800_000_000 - age, 1_800_000_000 - age))
    if unit:
        state = box.state()
        state["active"].append("bay-host-upgrade")
        box.state_path.write_text(json.dumps(state))
    code, out = _guard(box)
    if blocked:
        assert code != 0
        assert "A host upgrade runs on alpha-host" in out
        assert "deploy goes on" not in out
    else:
        assert code == 0, out
        assert "deploy goes on" in out


@pytest.mark.parametrize("playbook", ["deploy.yml", "provision.yml"])
def test_the_playbooks_run_the_guard_before_any_role(playbook: str) -> None:
    plays = yaml.safe_load((ROOT / playbook).read_text())
    index = 0 if playbook == "deploy.yml" else 1
    play = plays[index]
    pre = play["pre_tasks"]
    names = [t["name"] for t in pre]
    task = pre[names.index("Stop while a host upgrade runs on the box")]
    include = task["ansible.builtin.include_role"]
    assert include == {"name": "host_upgrade", "tasks_from": "guard", "apply": {"tags": ["always"]}}
    assert task["tags"] == ["always"]
    if playbook == "provision.yml":
        # Before the first apt task.
        assert names.index("Stop while a host upgrade runs on the box") < names.index(
            "Bootstrap python3-apt for the apt module"
        )
    else:
        # The first play: before rename_migration and every other role.
        assert names[0] == "Stop while a host upgrade runs on the box"


def test_a_unit_that_never_ends_fails_after_the_timeout(make_box) -> None:
    box = make_box()
    # systemd-run starts nothing: no rc file ever appears.
    (box.bin / "systemd-run").write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    code, out, result = box.upgrade()
    assert code != 0
    assert result["failed_task"] == "Fail when the upgrade unit has not ended in time"
    assert "still runs on the box as the unit bay-host-upgrade" in result["error"]
    assert result["packages_upgraded"] == 0
    # Two rounds, each touched the lock first (the heartbeat).
    assert out.count("Keep the deploy lock fresh while the upgrade runs") == 2
    assert not box.lock.exists()


# ── the filters, directly ───────────────────────────────────────────────────


def _filters() -> Any:
    import importlib.util

    path = ROOT / "roles" / "host_upgrade" / "filter_plugins" / "host_upgrade_filters.py"
    spec = importlib.util.spec_from_file_location("host_upgrade_filters", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_packages_come_from_the_inst_lines() -> None:
    f = _filters()
    lines = [
        "Reading package lists...",
        "Inst nftables [1.0.6-2] (1.0.6-2+deb12u2 Debian-Security:12/stable [amd64])",
        "Inst libnftables1 [1.0.6-2] (1.0.6-2+deb12u2 Debian-Security:12/stable [amd64])",
        "Conf nftables (1.0.6-2+deb12u2 Debian-Security:12/stable [amd64])",
    ]
    assert f.host_upgrade_packages(lines) == ["nftables", "libnftables1"]
    assert f.host_upgrade_packages(None) == []


def test_newest_kernel_sorts_by_version_not_by_text() -> None:
    f = _filters()
    assert f.host_upgrade_newest_kernel(["6.1.0-9-amd64", "6.1.0-25-amd64", "6.1.0-10-amd64"]) == (
        "6.1.0-25-amd64"
    )
    assert f.host_upgrade_newest_kernel([]) == ""


def test_published_port_is_the_lowest_ipv4_tcp_port() -> None:
    f = _filters()
    lines = [
        "",
        "0.0.0.0:8080->80/tcp, [::]:8080->80/tcp",
        "127.0.0.1:9100->9100/tcp",
        "5432/tcp",
        "0.0.0.0:3478->3478/udp",
    ]
    assert f.host_upgrade_published_port(lines) == 8080
    assert f.host_upgrade_published_port(["5432/tcp"]) == 0


def test_containers_settle_only_when_none_is_missing_or_starting() -> None:
    f = _filters()
    before = ["api", "db", "web"]
    state = f.host_upgrade_containers(
        ["api\tUp 1 second (health: starting)", "db\tUp 2 hours", "web\tRestarting (1) 2 seconds ago"],
        before,
    )
    assert state["missing"] == ["web"] and state["starting"] == ["api"]
    assert state["settled"] is False and state["ok"] is False
    done = f.host_upgrade_containers(["api\tUp 1 minute (healthy)", "db\tUp", "web\tUp"], before)
    assert done["settled"] is True and done["ok"] is True


def test_a_container_in_a_restart_loop_fails_and_its_log_is_shown(make_box) -> None:
    # The count grows during the window although the container looks `Up` at each sample.
    box = make_box(restarts={"api": [4, 7]})
    code, out, result = box.upgrade()
    assert code != 0
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["container_restarts"]["ok"] is False
    assert checks["container_restarts"]["detail"] == "restart loop: api (restarted, count 7)"
    assert checks["containers"]["ok"] is True
    assert result["containers"]["restart_loop"] == ["api"]
    assert result["containers"]["logs"]["api"] == [
        "api: listen tcp 100.64.0.1:443: bind: cannot assign requested address"
    ]
    assert "docker logs --tail 20 api" in box.calls()
    assert "cannot assign requested address" in out
    assert "Check failed: container_restarts" in result["error"]


def test_a_restarting_container_fails_the_restart_check(make_box) -> None:
    box = make_box(states={"postgres": "restarting"})
    code, out, result = box.upgrade()
    assert code != 0
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["container_restarts"]["detail"].startswith("restart loop: postgres (restarting")


def test_tailscaled_without_an_address_fails_the_tailscale_check(make_box) -> None:
    box = make_box(tailscale_ip="")
    code, out, result = box.upgrade()
    assert code != 0
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["tailscale"]["ok"] is False
    assert checks["tailscale"]["detail"].endswith("address none")


def test_restarts_compare_the_start_and_the_end_of_the_window() -> None:
    f = _filters()
    start = ["/api\t4\trunning", "/db\t0\trunning"]
    end = ["/api\t4\trunning", "/db\t2\trunning", "/web\t9\trestarting"]
    state = f.host_upgrade_restarts(start, end)
    assert state["grew"] == ["db"] and state["restarting"] == ["web"]
    assert state["looping"] == ["db", "web"]
