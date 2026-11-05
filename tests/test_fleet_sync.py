"""One fleet, several machines (2.12): pull first, claim before the deploy, run lock.

Two clones of one fleet ("machine A" and "machine B") share a bare remote and
the fake box of test_plan_up. No network, no SSH, no Ansible.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
    PLAN_SCHEMA,
    STATUS_TOML,
    FakeBox,
    _git_identity,
    box,
    cli,
    commit_all,
    git,
    lock_of,
    world,
)

from bay_cli import apply as applymod  # noqa: E402
from bay_cli import fleetsync, gitrepo, lockfile  # noqa: E402
from bay_cli import plan as planmod  # noqa: E402
from bay_cli.commands import ops  # noqa: E402
from bay_cli.context import Context  # noqa: E402
from bay_cli.errors import BayError  # noqa: E402
from bay_cli.fleet import GENERATED_SERVICES  # noqa: E402

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def machines(world: dict[str, Path], tmp_path: Path) -> dict[str, Path]:
    """Machine A is the world fleet; machine B is a second clone of its remote.

    The fleet has the in-fleet project ``status`` too, so each machine can
    deploy a project of its own onto the one box env.
    """
    a = world["fleet"]
    path = a / "projects" / "status" / "bay.toml"
    path.parent.mkdir()
    path.write_text(STATUS_TOML)
    (a / "README.md").write_text("fleet notes\n")
    commit_all(a, "add status and a readme")
    remote = tmp_path / "fleet-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(a, "remote", "add", "origin", str(remote))
    git(a, "push", "-q", "-u", "origin", "main")
    b = tmp_path / "machine-b"
    git(tmp_path, "clone", "-q", str(remote), str(b))
    return {"a": a, "b": b, "remote": remote, "app": world["app"], "tmp": tmp_path}


def proj_at(fleet: Path, name: str, app: Path) -> planmod.ProjectRef:
    cwd = app if name == "webapp" else fleet
    return planmod.load_project(Context.for_fleet_root(fleet), name, cwd=cwd)


def up_at(m: dict[str, Path], which: str, name: str, **kw: Any) -> dict[str, Any]:
    return applymod.up(proj_at(m[which], name, m["app"]), planmod.PlanOptions(), **kw)


@contextlib.contextmanager
def as_machine(name: str) -> Iterator[None]:
    """Run the body as another machine: its own host name and machine id.

    The tests run both machines in one process, so a claim of machine A names
    the test process. Machine B has another machine id, so it cannot judge it.
    """
    import hashlib

    real_host, real_tag = fleetsync.hostname, fleetsync.machine_tag
    fleetsync.hostname = lambda: name  # type: ignore[assignment]
    fleetsync.machine_tag = lambda: hashlib.sha256(name.encode()).hexdigest()[:12]  # type: ignore[assignment]
    try:
        yield
    finally:
        fleetsync.hostname = real_host  # type: ignore[assignment]
        fleetsync.machine_tag = real_tag  # type: ignore[assignment]


def services_of(fleet: Path) -> dict[str, Any]:
    data = yaml.safe_load((fleet / GENERATED_SERVICES).read_text().split("\n", 1)[1])
    return {**(data.get("accessories") or {}), **(data.get("services") or {})}


def lock_at(fleet: Path, name: str) -> dict[str, Any]:
    raw = lockfile.read(lockfile.lock_path(fleet, name))
    assert raw is not None
    return raw


def snapshot(repo: Path) -> dict[str, Any]:
    """HEAD, index, status and every work tree byte (the repo cache left out)."""
    files = {}
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if p.is_file() and rel.parts[0] not in (".git", ".bay-cache"):
            files[rel.as_posix()] = p.read_bytes()
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "index": git(repo, "ls-files", "-s"),
        "status": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        "files": files,
    }


def other_sessions_work(repo: Path) -> None:
    """Uncommitted work of another agent session in the same checkout."""
    (repo / "scratch.txt").write_text("untracked notes of another session\n")
    (repo / "README.md").write_text("fleet notes\nedited, not committed\n")
    (repo / "staged.txt").write_text("staged by another session\n")
    git(repo, "add", "staged.txt")


# ── 1. the race ─────────────────────────────────────────────────────────────


def test_b_pulls_first_and_its_compile_holds_a_pin(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    a_up = up_at(m, "a", "webapp")
    assert a_up["pushed"] is True and a_up["reason"] is None
    a_pin = lock_at(m["a"], "webapp")["envs"]["production"]["commit"]
    assert "webapp" not in services_of(m["b"])

    b_up = up_at(m, "b", "status")
    assert b_up["fleet_sync"]["pulled"] >= 2  # A's fleet commit and its receipt
    assert b_up["fleet_sync"]["from"] != b_up["fleet_sync"]["to"]
    assert {"webapp", "status"} <= set(services_of(m["b"]))
    assert lock_at(m["b"], "webapp")["envs"]["production"]["commit"] == a_pin
    assert b_up["pushed"] is True
    assert git(m["remote"], "rev-parse", "main") == git(m["b"], "rev-parse", "HEAD")
    # One history: A's commits are ancestors of B's.
    assert gitrepo.is_ancestor(m["b"], a_up["receipt_commit"], "HEAD") is True


def _land_a_before_b_claims(m: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """B's first push (its claim) runs after A's commits reach the remote."""
    real_push = fleetsync.push_upstream
    calls: list[str] = []

    def push(repo: Path) -> tuple[bool, str | None]:
        if Path(repo).resolve() == m["b"].resolve() and not calls:
            calls.append("claim")
            git(m["a"], "push", "-q")  # A's claim lands between B's commit and B's claim
        return real_push(repo)

    monkeypatch.setattr(fleetsync, "push_upstream", push)
    return calls


def test_b_claim_refused_when_a_lands_after_b_committed(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    # A deploys and commits, but its commits reach the remote only later.
    up_at(m, "a", "webapp", push=False)
    a_head = git(m["a"], "rev-parse", "HEAD")
    # Another session works in B's checkout, and B planned first (bay plan
    # leaves an untracked plan record; bay up makes the same plan again).
    other_sessions_work(m["b"])
    plan = planmod.make_plan(proj_at(m["b"], "status", m["app"]), planmod.PlanOptions())
    planmod.save(Context.for_fleet_root(m["b"]), plan)
    before = snapshot(m["b"])
    assert f"plans/{plan['plan_id']}.json" in before["files"]

    real_push = fleetsync.push_upstream
    calls = _land_a_before_b_claims(m, monkeypatch)
    deployed: list[str] = []

    def b_deploy(cx: Context, box_env: str, **_: Any) -> None:
        deployed.append(box_env)

    with pytest.raises(fleetsync.SyncError) as err:
        up_at(m, "b", "status", deploy=b_deploy, allow_dirty=True)
    assert calls == ["claim"]
    exc = err.value
    assert exc.reason == "claim_refused" and exc.exit_code == 42
    assert exc.box_touched is False
    assert exc.next_cmd is not None and "up production --project status" in exc.next_cmd
    assert str(exc).startswith("Stopped before the box was touched. The fleet remote moved: ")
    assert "Nothing was deployed. Run `" in str(exc) and "` again: it pulls first." in str(exc)
    assert deployed == []  # the box was never touched
    assert snapshot(m["b"]) == before  # HEAD, index, status and bytes as before
    assert git(m["remote"], "rev-parse", "main") == a_head

    # The next bay up pulls A's commits first, and its compile holds A's pin.
    monkeypatch.setattr(fleetsync, "push_upstream", real_push)
    again = up_at(m, "b", "status", allow_dirty=True)
    assert again["fleet_sync"]["pulled"] >= 1
    assert {"webapp", "status"} <= set(services_of(m["b"]))
    assert again["pushed"] is True
    # The other session's work is still there, untouched.
    assert (m["b"] / "scratch.txt").read_text().startswith("untracked notes")
    assert "edited, not committed" in (m["b"] / "README.md").read_text()
    assert "A  staged.txt" in git(m["b"], "status", "--porcelain=v1")


# ── 2 and 3. an ambiguous push, an unreachable remote ───────────────────────


def test_push_that_reports_failure_but_landed_goes_on(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    real_push = fleetsync.push_upstream
    calls: list[str] = []

    def push(repo: Path) -> tuple[bool, str | None]:
        calls.append(str(repo))
        pushed, problem = real_push(repo)
        if len(calls) == 1:
            assert pushed
            return False, "git push timed out"
        return pushed, problem

    monkeypatch.setattr(fleetsync, "push_upstream", push)
    result = up_at(m, "a", "webapp")
    assert result["result"] == "ok" and box.deploys == ["production"]
    assert result["reason"] is None and result["pushed"] is True
    assert git(m["remote"], "rev-parse", "main") == git(m["a"], "rev-parse", "HEAD")


def test_unreachable_remote_at_the_claim_restores_the_checkout(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    other_sessions_work(m["a"])
    before = snapshot(m["a"])
    away = m["tmp"] / "remote-away.git"

    def push(repo: Path) -> tuple[bool, str | None]:
        m["remote"].rename(away)  # the network drops after the start fetch
        return False, "fatal: could not read from remote repository"

    monkeypatch.setattr(fleetsync, "push_upstream", push)
    with pytest.raises(fleetsync.SyncError) as err:
        up_at(m, "a", "webapp", allow_dirty=True)
    away.rename(m["remote"])
    assert err.value.reason == "remote_unreachable" and err.value.exit_code == 41
    assert box.deploys == []
    after = snapshot(m["a"])
    # bay up wrote a new plan record (untracked, as bay plan leaves it).
    new_plans = set(after["files"]) - set(before["files"])
    assert all(p.startswith("plans/") for p in new_plans) and len(new_plans) == 1
    for key in ("head", "index"):
        assert after[key] == before[key]
    assert {k: v for k, v in after["files"].items() if k not in new_plans} == before["files"]
    # The lock file did not exist before the run, so it is gone again.
    assert not lockfile.lock_path(m["a"], "webapp").is_file() or (
        "production" not in lock_at(m["a"], "webapp").get("envs", {})
    )


def test_unreachable_remote_at_the_start_blocks_up(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    m["remote"].rename(m["tmp"] / "gone.git")
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 41, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "remote_unreachable" and doc["box_touched"] is False
    assert doc["fleet"] == {"name": "testfleet", "path": str(m["a"]), "selected_by": "--fleet"}
    assert box.deploys == []


# ── 4. the push after the deploy ────────────────────────────────────────────


def _during_deploy(
    box: FakeBox, monkeypatch: pytest.MonkeyPatch, action: Any
) -> None:
    def deploy(cx: Context, box_env: str, **kw: Any) -> None:
        box.deploy(cx, box_env, **kw)
        action()

    monkeypatch.setattr(applymod, "default_deploy", deploy)


def test_receipt_push_rebases_onto_an_unrelated_commit(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    c = m["tmp"] / "machine-c"

    def elsewhere() -> None:
        git(m["tmp"], "clone", "-q", str(m["remote"]), str(c))
        (c / "docs.md").write_text("unrelated\n")
        commit_all(c, "unrelated change on machine c")
        git(c, "push", "-q")

    _during_deploy(box, monkeypatch, elsewhere)
    result = up_at(m, "a", "webapp")
    assert result["pushed"] is True and result["reason"] is None
    assert git(m["remote"], "rev-parse", "main") == git(m["a"], "rev-parse", "HEAD")
    assert (m["a"] / "docs.md").is_file()
    # receipt_commit names the rebased commit, the one on the remote.
    receipt = result["receipt_commit"]
    assert git(m["remote"], "log", "-1", "--format=%s", receipt).startswith("bay: receipt ")
    assert gitrepo.is_ancestor(m["a"], receipt, "origin/main") is True


def test_receipt_push_with_a_conflict_is_receipt_unpushed(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    c = m["tmp"] / "machine-c"

    def conflicting() -> None:
        git(m["tmp"], "clone", "-q", str(m["remote"]), str(c))
        lock = c / "projects" / "webapp" / "bay.lock"
        lock.write_text(lock.read_text().replace('"pending"', '"failed"'))
        commit_all(c, "a conflicting lock edit")
        git(c, "push", "-q")

    _during_deploy(box, monkeypatch, conflicting)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 44, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "ok" and doc["pushed"] is False
    assert doc["reason"] == "receipt_unpushed" and doc["box_touched"] is True
    assert doc["next"] == "bay fleet push"
    assert "DEPLOYED, but the receipt is not on the remote. Run: bay fleet push" in result.stderr
    assert doc["push_conflicts"] == ["projects/webapp/bay.lock"]
    # The rebase was aborted: no rebase in progress, no tracked change, receipt kept local.
    git_dir = m["a"] / ".git"
    assert not (git_dir / "rebase-merge").exists() and not (git_dir / "rebase-apply").exists()
    assert git(m["a"], "status", "--porcelain=v1", "--untracked-files=no") == ""
    assert git(m["a"], "rev-parse", "HEAD") == doc["receipt_commit"]
    assert lock_at(m["a"], "webapp")["envs"]["production"]["result"] == "ok"


def test_failed_deploy_keeps_its_exit_code_when_the_receipt_is_unpushed(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    c = m["tmp"] / "machine-c"

    def conflicting() -> None:
        git(m["tmp"], "clone", "-q", str(m["remote"]), str(c))
        lock = c / "projects" / "webapp" / "bay.lock"
        lock.write_text(lock.read_text().replace('"pending"', '"ok"'))
        commit_all(c, "a conflicting lock edit")
        git(c, "push", "-q")

    def deploy(cx: Context, box_env: str, **kw: Any) -> None:
        conflicting()
        box.fail = True
        box.deploy(cx, box_env, **kw)

    monkeypatch.setattr(applymod, "default_deploy", deploy)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "failed" and doc["pushed"] is False
    assert doc["reason"] == "deploy_failed" and doc["next"] == "bay fleet push"
    assert "DEPLOY FAILED, but the receipt is not on the remote" in result.stderr


# ── 5. claimed_by and claimed_at ────────────────────────────────────────────


def test_claim_fields_while_pending_and_gone_after(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    monkeypatch.setattr(fleetsync, "hostname", lambda: "machine-a")
    seen: dict[str, Any] = {}

    def look() -> None:
        raw = lock_at(m["a"], "webapp")
        seen["local"] = raw["envs"]["production"]
        seen["problems"] = lockfile.problems(raw)
        on_remote = git(m["remote"], "show", "main:projects/webapp/bay.lock")
        seen["remote"] = json.loads(on_remote)["envs"]["production"]

    _during_deploy(box, monkeypatch, look)
    up_at(m, "a", "webapp")
    for where in ("local", "remote"):
        record = seen[where]
        assert record["result"] == "pending" and record["claimed_by"] == "machine-a"
        assert record["claimed_at"].endswith("Z")
    assert seen["problems"] == []  # the schema accepts both fields
    after = lock_at(m["a"], "webapp")["envs"]["production"]
    assert after["result"] == "ok"
    assert "claimed_by" not in after and "claimed_at" not in after
    assert lockfile.problems(lock_at(m["a"], "webapp")) == []


def test_show_says_deploying_then_half() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)
    fresh = {"result": "pending", "claimed_by": "machine-a", "claimed_at": "2026-11-05T14:26:00Z"}
    word, why = applymod.env_status(fresh, "a" * 12, "a" * 12, None, now=now)
    assert (word, why) == ("deploying", "claimed by machine-a 4 min ago")
    old = {**fresh, "claimed_at": "2026-11-05T13:00:00Z"}
    word, why = applymod.env_status(old, "a" * 12, "a" * 12, None, now=now)
    assert word == "HALF"
    assert why == "an up from machine-a at 2026-11-05T13:00:00Z never reported"
    legacy = {"result": "pending"}
    assert applymod.env_status(legacy, None, None, None, now=now) == (
        "HALF",
        "the last bay up never reported back",
    )


# ── 6. the run lock ─────────────────────────────────────────────────────────

_HOLDER = """
import sys
from collections.abc import Iterator
from pathlib import Path
from bay_cli import fleetsync
with fleetsync.run_lock(Path(sys.argv[1]), "up", "production"):
    print("ready", flush=True)
    sys.stdin.read()
"""


def _hold(fleet: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(fleet)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


def test_second_run_is_refused_with_the_holder_named(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    holder = _hold(m["a"])
    try:
        with pytest.raises(fleetsync.SyncError) as err:
            up_at(m, "a", "webapp")
        assert err.value.reason == "fleet_busy" and err.value.exit_code == 43
        assert str(err.value).startswith(
            f"the fleet is busy: bay up production (pid {holder.pid} on {fleetsync.hostname()}, "
            "since "
        )
        assert box.deploys == []
        result = cli({"fleet": m["a"], "app": m["app"]}, "fleet", "status", "--json")
        assert json.loads(result.stdout)["lock_holder"]["pid"] == holder.pid
        # deploy and provision take the same lock.
        for verb in ("deploy", "provision"):
            run = cli({"fleet": m["a"], "app": m["app"]}, verb, "production")
            assert getattr(run.exception, "reason", None) == "fleet_busy", run.output
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)
    assert up_at(m, "a", "webapp")["result"] == "ok"


def test_a_dead_holder_does_not_block(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    holder = _hold(m["a"])
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)
    sidecar = fleetsync._lock_paths(m["a"])[1]
    assert json.loads(sidecar.read_text())["pid"] == holder.pid  # stale, left behind
    assert fleetsync.lock_holder(m["a"]) is None
    assert up_at(m, "a", "webapp")["result"] == "ok"
    assert not sidecar.exists()


def test_plan_skips_the_pull_when_the_fleet_is_busy(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    up_at(m, "b", "status")  # A is now behind
    holder = _hold(m["a"])
    try:
        busy = cli({"fleet": m["a"], "app": m["app"]}, "plan", "--json")
        doc = json.loads(busy.stdout)
        assert doc["fleet_sync"] is None and doc["verdict"] == "blocked"
        assert any("pull it first" in b for b in doc["blockers"])
        assert "fleet: not pulled, the fleet is busy" in busy.stderr
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)
    free = cli({"fleet": m["a"], "app": m["app"]}, "plan", "--json")
    doc = json.loads(free.stdout)
    assert doc["fleet_sync"]["pulled"] >= 1 and doc["verdict"] != "blocked"
    assert "fleet: pulled " in free.stderr and " from origin/main (" in free.stderr


def test_rollback_holds_the_lock_once(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    up_at(m, "a", "webapp")
    edit = m["app"] / "bay.toml"
    edit.write_text(edit.read_text().replace('LOG_LEVEL = "info"', 'LOG_LEVEL = "debug"'))
    commit_all(m["app"], "debug")
    git(m["app"], "push", "-q", "origin", "main")
    up_at(m, "a", "webapp")
    back = applymod.rollback(proj_at(m["a"], "webapp", m["app"]), planmod.PlanOptions())
    assert back["action"] == "rollback" and back["pushed"] is True
    assert fleetsync.lock_holder(m["a"]) is None


# ── 3 (plan). diverged ──────────────────────────────────────────────────────


def _diverge(m: dict[str, Path]) -> None:
    up_at(m, "b", "status")
    (m["a"] / "local.md").write_text("a local commit\n")
    commit_all(m["a"], "local commit on machine a")


def test_diverged_fleet_blocks_up_with_its_reason(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    _diverge(m)
    deploys = len(box.deploys)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 41, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "diverged" and doc["next"] == "bay fleet push"
    assert doc["box_touched"] is False
    blocker = next(b for b in doc["blockers"] if "diverged" in b)
    assert "1 local commit(s)" in blocker and "run `bay fleet push`" in blocker.lower()
    assert "never merge services.yml, a bay.lock or a vault file by hand" in blocker
    assert len(box.deploys) == deploys


# ── 7. bay deploy and bay provision ─────────────────────────────────────────


@pytest.fixture
def playbooks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    ran: list[tuple[str, str]] = []

    def run(cx: Context, playbook: str, env: str, *_: Any, **__: Any) -> None:
        ran.append((playbook, env))

    monkeypatch.setattr(ops, "_run_playbook", run)
    return ran


def _op(m: dict[str, Path], verb: str, *extra: str, fleet: Path | None = None) -> Any:
    args = [verb, *extra, "production"]
    if verb == "deploy":
        quiet = ["--tags", "deploy_stack", "--skip-validate", "--skip-healthcheck"]
        args = [verb, *quiet, *extra, "production"]
    return cli({"fleet": fleet or m["a"], "app": m["app"]}, *args)


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_refuse_unreachable_unless_offline(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    m["remote"].rename(m["tmp"] / "gone.git")
    refused = _op(m, verb)
    assert getattr(refused.exception, "reason", None) == "remote_unreachable", refused.output
    assert refused.exception.exit_code == 41
    assert playbooks == []
    offline = _op(m, verb, "--offline")
    assert offline.exit_code == 0, offline.output
    assert playbooks == [(verb, "production")]
    assert "WARNING: --offline" in offline.stderr and "bay fleet push" in offline.stderr


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_pull_a_fleet_that_is_behind(
    machines: dict[str, Path], box: FakeBox, playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    up_at(m, "b", "status")
    result = _op(m, verb)
    assert result.exit_code == 0, result.output
    assert playbooks == [(verb, "production")]
    assert git(m["a"], "rev-parse", "HEAD") == git(m["remote"], "rev-parse", "main")
    assert "fleet: pulled " in result.stderr


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_refuse_a_diverged_fleet(
    machines: dict[str, Path], box: FakeBox, playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    _diverge(m)
    result = _op(m, verb)
    assert getattr(result.exception, "reason", None) == "diverged", result.output
    assert playbooks == []


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_refuse_dirty_unless_allowed(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    for i in range(23):
        (m["a"] / f"loose-{i:02d}.yml").write_text("x: 1\n")
    (m["a"] / "README.md").write_text("changed\n")
    refused = _op(m, verb)
    exc = refused.exception
    assert getattr(exc, "reason", None) == "dirty", refused.output
    assert "README.md" in str(exc) and "and 4 more" in str(exc)
    assert "no other machine can see them" in str(exc)
    assert len(exc.paths) == 24
    # A dry run follows the same rules.
    dry = _op(m, verb, "--", "--check")
    assert getattr(dry.exception, "reason", None) == "dirty"
    assert playbooks == []
    allowed = _op(m, verb, "--allow-dirty")
    assert allowed.exit_code == 0, allowed.output
    assert playbooks == [(verb, "production")]


def test_plan_records_and_the_first_deploy_marker_are_not_dirty(
    machines: dict[str, Path], playbooks: list[tuple[str, str]]
) -> None:
    m = machines
    (m["a"] / "plans").mkdir(exist_ok=True)
    (m["a"] / "plans" / "0123456789ab.json").write_text("{}\n")
    (m["a"] / ".first_deploy_done").write_text("")
    assert _op(m, "deploy").exit_code == 0
    assert playbooks == [("deploy", "production")]


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_push_a_fleet_that_is_ahead_first(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    (m["a"] / "group_vars" / "all" / "extra.yml").write_text("---\nextra: 1\n")
    head = commit_all(m["a"], "a config change")
    result = _op(m, verb)
    assert result.exit_code == 0, result.output
    assert git(m["remote"], "rev-parse", "main") == head
    assert playbooks == [(verb, "production")]


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_stop_when_the_push_first_is_refused(
    machines: dict[str, Path], playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = machines
    (m["a"] / "local.md").write_text("x\n")
    commit_all(m["a"], "a local commit")
    hook = m["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused by test >&2\nexit 1\n")
    hook.chmod(0o755)
    result = _op(m, verb)
    exc = result.exception
    assert getattr(exc, "reason", None) == "claim_refused", result.output
    assert exc.exit_code == 42 and str(exc).startswith("Stopped before the box was touched.")
    assert playbooks == []


@pytest.mark.parametrize("verb", ["deploy", "provision"])
def test_playbook_verbs_without_a_remote_check_only_dirty(
    world: dict[str, Path], playbooks: list[tuple[str, str]], verb: str
) -> None:
    m = {"a": world["fleet"], "app": world["app"]}
    assert _op(m, verb).exit_code == 0
    (world["fleet"] / "loose.yml").write_text("x: 1\n")
    assert getattr(_op(m, verb).exception, "reason", None) == "dirty"
    assert playbooks == [(verb, "production")]


def test_playbook_verbs_in_a_bigger_repo_check_only_dirty(
    world: dict[str, Path], tmp_path: Path, playbooks: list[tuple[str, str]]
) -> None:
    outer = tmp_path / "workspace"
    outer.mkdir()
    fleet = outer / "fleet"
    shutil.move(str(world["fleet"]), str(fleet))
    shutil.rmtree(fleet / ".git")
    (outer / "README").write_text("other work\n")
    git(outer, "init", "-q")
    commit_all(outer, "workspace")
    remote = tmp_path / "workspace-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(outer, "remote", "add", "origin", str(remote))
    git(outer, "push", "-q", "-u", "origin", "main")
    # The bigger repo is ahead of its remote: bay never pushes it.
    (outer / "README").write_text("more other work\n")
    commit_all(outer, "more")
    before = git(remote, "rev-parse", "main")
    m = {"a": fleet, "app": world["app"]}
    (outer / "outside-the-fleet.txt").write_text("not the fleet's\n")
    assert _op(m, "deploy").exit_code == 0
    assert git(remote, "rev-parse", "main") == before
    (fleet / "loose.yml").write_text("x: 1\n")
    assert getattr(_op(m, "deploy").exception, "reason", None) == "dirty"
    assert playbooks == [("deploy", "production")]


# ── 8. bay fleet status, pull, push ─────────────────────────────────────────


def _fleet(m: dict[str, Path], *args: str) -> Any:
    return cli({"fleet": m["a"], "app": m["app"]}, "fleet", *args)


def test_fleet_status(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    clean = _fleet(m, "status", "--json")
    assert clean.exit_code == 0, clean.output
    doc = json.loads(clean.stdout)
    assert doc["in_sync"] is True and doc["clean"] is True and doc["upstream"] == "origin/main"
    assert doc["branch"] == "main" and doc["lock_holder"] is None and doc["open_records"] == []
    assert doc["fleet"]["path"] == str(m["a"])

    up_at(m, "b", "status")
    (m["a"] / "loose.yml").write_text("x: 1\n")
    (m["a"] / "plans").mkdir(exist_ok=True)
    (m["a"] / "plans" / "0123456789ab.json").write_text("{}\n")
    behind = _fleet(m, "status", "--json")
    assert behind.exit_code == 41
    doc = json.loads(behind.stdout)
    assert doc["behind"] >= 1 and doc["ahead"] == 0 and doc["dirty_paths"] == ["loose.yml"]
    assert doc["untracked_plans"] == 1
    text = _fleet(m, "status")
    assert "uncommitted: loose.yml" in text.stdout and "behind" in text.stdout


def test_fleet_status_lists_pending_and_failed_records(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    box.fail = True
    with pytest.raises(applymod.DeployFailed):
        up_at(m, "a", "webapp")
    path = lockfile.lock_path(m["a"], "status")
    raw = lock_at(m["a"], "status")
    raw["envs"]["production"].update(
        {"result": "pending", "claimed_by": "machine-b", "claimed_at": "2026-11-05T14:00:00Z"}
    )
    lockfile.write(path, raw)
    doc = json.loads(_fleet(m, "status", "--json").stdout)
    rows = {(r["project"], r["env"]): r for r in doc["open_records"]}
    assert rows[("webapp", "production")]["result"] == "failed"
    assert rows[("status", "production")]["claimed_by"] == "machine-b"


def test_fleet_pull(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    assert _fleet(m, "pull").exit_code == 0
    up_at(m, "b", "status")
    pulled = _fleet(m, "pull")
    assert pulled.exit_code == 0, pulled.output
    assert "fleet: pulled " in pulled.stdout
    assert git(m["a"], "rev-parse", "HEAD") == git(m["remote"], "rev-parse", "main")


def test_fleet_pull_refuses_a_diverged_fleet(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    _diverge(m)
    head = git(m["a"], "rev-parse", "HEAD")
    result = _fleet(m, "pull")
    assert getattr(result.exception, "reason", None) == "diverged"
    assert git(m["a"], "rev-parse", "HEAD") == head


def test_fleet_push_rebases_a_diverged_fleet(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    _diverge(m)
    result = _fleet(m, "push")
    assert result.exit_code == 0, result.output
    assert "after a rebase" in result.stdout
    assert git(m["a"], "rev-parse", "HEAD") == git(m["remote"], "rev-parse", "main")
    assert (m["a"] / "local.md").is_file() and (m["a"] / "projects" / "status").is_dir()


def test_fleet_push_conflict_aborts_and_names_the_files(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    (m["b"] / "README.md").write_text("machine b's notes\n")
    commit_all(m["b"], "b readme")
    git(m["b"], "push", "-q")
    (m["a"] / "README.md").write_text("machine a's notes\n")
    head = commit_all(m["a"], "a readme")
    result = _fleet(m, "push")
    exc = result.exception
    assert getattr(exc, "reason", None) == "diverged", result.output
    assert exc.paths == ["README.md"] and "README.md" in str(exc)
    assert "never merge services.yml, a bay.lock or a vault file by hand" in (exc.hint or "")
    assert git(m["a"], "rev-parse", "HEAD") == head
    assert git(m["a"], "status", "--porcelain=v1", "--untracked-files=no") == ""
    assert not (m["a"] / ".git" / "rebase-merge").exists()


# ── 9. no remote, and a fleet inside a bigger repo ──────────────────────────


def test_fleet_without_a_remote_ups_as_before(
    world: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    pushes: list[str] = []
    real = fleetsync.push_upstream

    def push(repo: Path) -> tuple[bool, str | None]:
        pushes.append(str(repo))
        return real(repo)

    monkeypatch.setattr(fleetsync, "push_upstream", push)
    result = applymod.up(
        planmod.load_project(Context.for_fleet_root(world["fleet"]), "webapp", cwd=world["app"]),
        planmod.PlanOptions(),
    )
    assert result["result"] == "ok" and result["pushed"] is False
    assert result["push_error"] is None and result["reason"] is None
    assert result["fleet_sync"] is None and pushes == []
    record = lock_of(world)["envs"]["production"]
    assert record["result"] == "ok" and "claimed_by" not in record


def test_fleet_inside_a_bigger_repo_is_not_claimed(
    world: dict[str, Path], tmp_path: Path, box: FakeBox
) -> None:
    outer = tmp_path / "workspace"
    outer.mkdir()
    fleet = outer / "fleet"
    shutil.move(str(world["fleet"]), str(fleet))
    shutil.rmtree(fleet / ".git")
    git(outer, "init", "-q")
    commit_all(outer, "workspace")
    remote = tmp_path / "workspace-remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(outer, "remote", "add", "origin", str(remote))
    git(outer, "push", "-q", "-u", "origin", "main")
    before = git(remote, "rev-parse", "main")
    result = applymod.up(
        planmod.load_project(Context.for_fleet_root(fleet), "webapp", cwd=world["app"]),
        planmod.PlanOptions(),
    )
    assert result["result"] == "ok" and box.deploys == ["production"]
    assert "inside a larger repo" in result["push_skipped"]
    assert result["reason"] is None and result["fleet_sync"] is None
    assert git(remote, "rev-parse", "main") == before


def test_no_push_skips_the_claim(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    before = git(m["remote"], "rev-parse", "main")
    result = up_at(m, "a", "webapp", push=False)
    assert result["result"] == "ok" and result["pushed"] is False
    assert git(m["remote"], "rev-parse", "main") == before


def test_state_reports_each_field(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    st = fleetsync.state(m["a"])
    assert st.in_sync and not st.diverged and st.dirty_paths == [] and st.fetched
    _diverge(m)
    st = fleetsync.state(m["a"])
    assert st.diverged and st.ahead == 1 and st.behind >= 1 and not st.in_sync
    m["remote"].rename(m["tmp"] / "gone.git")
    st = fleetsync.state(m["a"])
    assert st.unreachable and not st.in_sync


def test_up_json_carries_the_outcome_fields(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] is None and doc["box_touched"] is True and doc["next"] is None
    assert doc["fleet"] == {"name": "testfleet", "path": str(m["a"]), "selected_by": "--fleet"}
    assert doc["fleet_sync"] is None and doc["pushed"] is True
    assert "fleet: claimed" in result.stderr


def test_a_partial_undo_names_the_paths_it_could_not_restore(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    up_at(m, "b", "status")  # services.yml now exists at the remote; A pulls it first
    hook = m["remote"] / "hooks" / "pre-receive"
    services = str(GENERATED_SERVICES)
    real_run = gitrepo._run
    calls: list[str] = []

    def run(repo: Path, *args: str, **kw: Any) -> Any:
        if args[:1] == ("restore",) and args[-1] == services:
            calls.append(args[-1])
            return subprocess.CompletedProcess(["git", *args], 1, "", "error: forced by test")
        if args[:1] == ("push",) and not hook.exists():
            # The remote refuses this run's claim, after the start fetch and pull.
            hook.write_text("#!/bin/sh\necho refused by test >&2\nexit 1\n")
            hook.chmod(0o755)
        return real_run(repo, *args, **kw)

    monkeypatch.setattr(gitrepo, "_run", run)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 42, result.output
    doc = json.loads(result.stdout)
    assert calls == [services]
    assert doc["reason"] == "claim_refused" and doc["box_touched"] is False
    assert doc["undo_failed"] == [services]
    assert "The undo was PARTIAL" in doc["error"] and services in doc["error"]
    assert "is undone." not in doc["error"]
    # The other paths were still put back: the lock file is as at HEAD.
    assert git(m["a"], "status", "--porcelain=v1", "--", "projects/webapp/bay.lock") == ""
    assert len(box.deploys) == 1  # only machine B's up deployed


def test_a_full_undo_says_so_and_lists_nothing(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    hook = m["remote"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused by test >&2\nexit 1\n")
    hook.chmod(0o755)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 42, result.output
    doc = json.loads(result.stdout)
    assert doc["undo_failed"] == [] and "The fleet commit of this run is undone." in doc["error"]


# ── the plan record collision ───────────────────────────────────────────────


def test_same_plan_on_two_machines_no_longer_blocks_the_pull(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    # B plans the same thing A then applies: the same content-hashed plan id.
    b_plan = cli({"fleet": m["b"], "app": m["app"]}, "plan", "--json")
    plan_id = json.loads(b_plan.stdout)["plan_id"]
    a_up = up_at(m, "a", "webapp")
    assert a_up["plan_id"] == plan_id
    record = Path("plans") / f"{plan_id}.json"
    assert (m["b"] / record).is_file()
    assert git(m["b"], "ls-files", "--", str(record)) == ""  # untracked in B
    pulled = cli({"fleet": m["b"], "app": m["app"]}, "fleet", "pull")
    assert pulled.exit_code == 0, pulled.output
    assert "fleet: removed 1 local plan record(s) that the remote now holds" in pulled.stderr
    assert git(m["b"], "rev-parse", "HEAD") == git(m["remote"], "rev-parse", "main")
    assert git(m["b"], "ls-files", "--", str(record)) == str(record)  # now the remote's copy


def test_only_colliding_untracked_plan_records_are_removed(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    (m["a"] / "plans").mkdir(exist_ok=True)
    (m["a"] / "plans" / "aaaaaaaaaaaa.json").write_text('{"from": "a"}\n')
    commit_all(m["a"], "a plan record")
    git(m["a"], "push", "-q")
    (m["b"] / "plans").mkdir(exist_ok=True)
    (m["b"] / "plans" / "aaaaaaaaaaaa.json").write_text('{"from": "b"}\n')
    (m["b"] / "plans" / "bbbbbbbbbbbb.json").write_text('{"only": "b"}\n')
    (m["b"] / "plans" / "bbbbbbbbbbbb.approved").write_text("approved\n")
    git(m["b"], "fetch", "-q")
    pulled = fleetsync.fast_forward(m["b"])
    assert pulled.count == 1
    assert (m["b"] / "plans" / "aaaaaaaaaaaa.json").read_text() == '{"from": "a"}\n'
    assert (m["b"] / "plans" / "bbbbbbbbbbbb.json").read_text() == '{"only": "b"}\n'
    assert (m["b"] / "plans" / "bbbbbbbbbbbb.approved").is_file()


def test_a_colliding_non_plan_file_still_refuses(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    (m["a"] / "plans").mkdir(exist_ok=True)
    (m["a"] / "plans" / "aaaaaaaaaaaa.json").write_text('{"from": "a"}\n')
    (m["a"] / "notes.md").write_text("from a\n")
    commit_all(m["a"], "a plan record and a note")
    git(m["a"], "push", "-q")
    (m["b"] / "plans").mkdir(exist_ok=True)
    (m["b"] / "plans" / "aaaaaaaaaaaa.json").write_text('{"from": "b"}\n')
    (m["b"] / "notes.md").write_text("from b, untracked\n")
    head = git(m["b"], "rev-parse", "HEAD")
    result = cli({"fleet": m["b"], "app": m["app"]}, "fleet", "pull")
    exc = result.exception
    assert getattr(exc, "reason", None) == "dirty", result.output
    assert "notes.md" in str(exc)
    assert git(m["b"], "rev-parse", "HEAD") == head
    assert (m["b"] / "notes.md").read_text() == "from b, untracked\n"
    # The merge did not run, so the plan record B removed for it is back.
    assert (m["b"] / "plans" / "aaaaaaaaaaaa.json").read_text() == '{"from": "b"}\n'
    assert "removed" not in result.stderr


# ── discovery step 5 meets the fleet verbs ──────────────────────────────────


def _invoke_in(monkeypatch: pytest.MonkeyPatch, cwd: Path, *args: str) -> Any:
    from typer.testing import CliRunner

    from bay_cli.cli import app

    monkeypatch.chdir(cwd)
    return CliRunner().invoke(app, list(args))


def _status_json(monkeypatch: pytest.MonkeyPatch, cwd: Path) -> dict[str, Any]:
    result = _invoke_in(monkeypatch, cwd, "fleet", "status", "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_fleet_verbs_find_the_fleet_by_discovery_step_5(
    machines: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BAY_FLEET", raising=False)
    monkeypatch.delenv("BAY_FLEET_NAME", raising=False)
    # 5a: a folder inside the fleet. (Not projects/status: its bay.toml names
    # the fleet, and step 3 looks for that name under ~/.config/bay/fleets.)
    doc = _status_json(monkeypatch, m["a"] / "group_vars")
    assert doc["fleet"]["path"] == str(m["a"].resolve())
    assert doc["fleet"]["selected_by"] == "the fleet folder you stand in"
    # 5b: a folder next to the only fleet nearby.
    lone = tmp_path / "lone"
    git(tmp_path, "clone", "-q", str(m["remote"]), str(lone / "fleet"))
    (lone / "work").mkdir()
    doc = _status_json(monkeypatch, lone / "work")
    assert doc["fleet"]["path"] == str((lone / "fleet").resolve())
    assert doc["fleet"]["selected_by"] == "the only fleet nearby"
    # bay up carries the same value in its JSON outcome.
    m["remote"].rename(m["tmp"] / "gone.git")
    result = _invoke_in(monkeypatch, m["a"], "up", "--project", "status", "--json")
    assert result.exit_code == 41, result.output
    up = json.loads(result.stdout)
    assert up["fleet"]["selected_by"] == "the fleet folder you stand in"


# ── review 1: a fresh claim of another machine keeps the box env ────────────


def test_b_cannot_deploy_while_a_claim_is_in_flight(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    seen: dict[str, Any] = {}

    def a_deploy(cx: Context, box_env: str, **kw: Any) -> None:
        # A's claim is on the remote; A has not touched the box yet.
        with as_machine("machine-b"):
            with pytest.raises(applymod.Refused) as err:
                up_at(m, "b", "status")
            seen["refused"] = err.value
            seen["plan"] = cli(
                {"fleet": m["b"], "app": m["app"]}, "plan", "--project", "status", "--json"
            )
            seen["up"] = cli(
                {"fleet": m["b"], "app": m["app"]}, "up", "--project", "status", "--json"
            )
        box.deploy(cx, box_env, **kw)

    a = up_at(m, "a", "webapp", deploy=a_deploy)
    assert a["result"] == "ok" and a["pushed"] is True
    refused = seen["refused"]
    assert refused.reason == "env_busy" and refused.exit_code == 43
    holder = refused.plan["holder"]
    assert holder["claimed_by"] == fleetsync.hostname()
    assert holder["project"] == "webapp" and holder["box_env"] == "production"
    assert holder["plan_id"] == a["plan_id"]
    assert set(holder) >= {"claimed_by", "claimed_at", "plan_id", "expires_at"}
    message = next(b for b in refused.plan["blockers"] if "is deploying" in b)
    assert message.startswith(f"{fleetsync.hostname()} is deploying production since ")
    assert f"(plan {a['plan_id']}). Wait for it. If that run is dead, the claim expires at " in (
        message
    )
    assert message.endswith(", or pass --take-over.")
    # bay plan shows it; bay up --json refuses with exit 43 and names the holder.
    plan = json.loads(seen["plan"].stdout)
    assert plan["reason"] == "env_busy" and plan["holder"]["plan_id"] == a["plan_id"]
    import jsonschema

    jsonschema.validate(plan, PLAN_SCHEMA)
    up = json.loads(seen["up"].stdout)
    assert seen["up"].exit_code == 43 and up["reason"] == "env_busy"
    assert up["holder"]["claimed_by"] == fleetsync.hostname() and up["box_touched"] is False
    # B never touched the box: A deployed once.
    assert box.deploys == ["production"]
    # The claim is not hashed: the refused plan keeps its own id.
    assert planmod.body_sha256(refused.plan) == refused.plan["plan_sha256"]


def test_take_over_deploys_and_is_recorded(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    seen: dict[str, Any] = {}

    def a_deploy(cx: Context, box_env: str, **kw: Any) -> None:
        with as_machine("machine-b"):
            seen["b"] = up_at(m, "b", "status", take_over=True)

    # A's own deployer never ran the playbook, so no receipt is from A's deploy.
    with pytest.raises(applymod.DeployFailed):
        up_at(m, "a", "webapp", deploy=a_deploy)
    b = seen["b"]
    assert b["result"] == "ok"
    assert b["took_over"]["project"] == "webapp"
    assert b["took_over"]["claimed_by"] == fleetsync.hostname()
    assert box.deploys == ["production"]  # only B deployed (A's deployer did not)


def test_an_old_pending_claim_does_not_block(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    up_at(m, "a", "webapp")
    raw = lock_at(m["a"], "webapp")
    record = raw["envs"]["production"]
    record.update(
        {"result": "pending", "claimed_by": "machine-a", "claimed_at": "2026-01-01T00:00:00Z"}
    )
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], "a pending claim from long ago")
    git(m["a"], "push", "-q")
    plan = planmod.make_plan(proj_at(m["a"], "status", m["app"]), planmod.PlanOptions())
    assert plan["holder"] is None and plan["reason"] is None
    # A fresh one does.
    record["claimed_at"] = fleetsync.now_iso()
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], "a fresh pending claim")
    git(m["a"], "push", "-q")
    plan = planmod.make_plan(proj_at(m["a"], "status", m["app"]), planmod.PlanOptions())
    assert plan["reason"] == "env_busy" and plan["holder"]["claimed_by"] == "machine-a"
    # A record with no claimed_run (an older writer) blocks, also from this host
    # with the same plan: Bay cannot judge it.
    record.update({"claimed_by": fleetsync.hostname(), "plan_id": plan["plan_id"]})
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    cx = Context.for_fleet_root(m["a"])
    assert planmod.foreign_claim(cx, {"production"}, str(plan["plan_id"])) is not None
    # This process's own claim is its own.
    record["claimed_run"] = fleetsync.run_id()
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    assert planmod.foreign_claim(cx, {"production"}, str(plan["plan_id"])) is None


# ── review 6: box_touched means Ansible started ─────────────────────────────


def test_box_touched_is_false_when_validation_stops_the_deploy(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from bay_cli.commands import validate

    m = machines
    started: list[str] = []
    monkeypatch.setattr(applymod, "default_deploy", applymod._REAL_DEFAULT_DEPLOY)
    monkeypatch.setattr(validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=2))
    monkeypatch.setattr(ops, "_run_playbook", lambda *a, **k: started.append("playbook"))
    with pytest.raises(applymod.DeployFailed) as err:
        up_at(m, "a", "webapp")
    assert started == []
    assert err.value.result["box_touched"] is False
    assert err.value.result["reason"] == "deploy_failed"


def test_a_validation_failure_in_the_deploy_step_is_not_a_stopped_claim(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """review 4, item 5: the receipt step records it; the unwind stays out."""
    from types import SimpleNamespace

    from bay_cli.commands import validate

    m = machines
    monkeypatch.setattr(applymod, "default_deploy", applymod._REAL_DEFAULT_DEPLOY)
    monkeypatch.setattr(validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=2))
    monkeypatch.setattr(ops, "_run_playbook", lambda *a, **k: None)
    with pytest.raises(applymod.DeployFailed):
        up_at(m, "a", "webapp")
    err = capsys.readouterr().err
    assert "STOPPED AFTER THE CLAIM" not in err
    assert "pending lock records" not in err
    assert "putting the fleet back" not in err
    remote_lock = git(m["a"], "show", "@{u}:projects/webapp/bay.lock")
    assert '"result": "failed"' in remote_lock


def test_box_touched_is_true_for_a_failure_after_the_playbook_started(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from bay_cli.commands import validate

    m = machines
    real_write = lockfile.write

    def playbook(cx: Context, playbook: str, env: str, *_: Any, **__: Any) -> None:
        box.deploy(cx, env)

        def broken(path: Path, data: Any) -> None:
            raise lockfile.LockWriteError("the lock breaks its schema (test)")

        monkeypatch.setattr(lockfile, "write", broken)

    monkeypatch.setattr(applymod, "default_deploy", applymod._REAL_DEFAULT_DEPLOY)
    monkeypatch.setattr(validate, "run_validation", lambda *a, **k: SimpleNamespace(total_issues=0))
    monkeypatch.setattr(ops, "_run_playbook", playbook)
    monkeypatch.setattr(ops, "_invalidate_rig_cache", lambda *_: None)
    monkeypatch.setattr(ops, "_run_post_deploy_healthcheck", lambda *a, **k: None)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    monkeypatch.setattr(lockfile, "write", real_write)
    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert doc["box_touched"] is True
    assert doc["error"].startswith("LockWriteError after the deploy started:")
    assert box.deploys == ["production"]


# ── review 7 and 8: reasons ─────────────────────────────────────────────────


def test_an_unpushed_app_commit_has_its_own_reason(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    app_toml = m["app"] / "bay.toml"
    app_toml.write_text(app_toml.read_text().replace('LOG_LEVEL = "info"', 'LOG_LEVEL = "x"'))
    commit_all(m["app"], "not pushed")
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "app_commit_unpushed"
    assert doc["next"] == f"git -C {m['app']} push"
    assert doc["box_touched"] is False
    assert box.deploys == []


def test_a_failed_remove_is_deploy_failed_and_says_so(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    w = {"fleet": m["a"], "app": m["app"]}
    up_at(m, "a", "webapp")
    plan = json.loads(cli(w, "remove", "webapp", "--json").stdout)
    assert cli(w, "approve", plan["plan_id"], "--reason", "retired").exit_code == 0
    box.fail = True
    monkeypatch.setattr(
        fleetsync,
        "push_with_rebase",
        lambda repo, **_: fleetsync.PushResult(False, "the remote refused (test)"),
    )
    result = cli(w, "up", "production", "--plan-id", plan["plan_id"], "--json")
    box.fail = False
    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert doc["result"] == "failed"
    assert doc["reason"] == "deploy_failed"
    assert doc["next"] == "bay fleet push" and doc["box_touched"] is True
    said = result.output + (result.stderr if result.stderr_bytes is not None else "")
    assert "DEPLOY FAILED, but the receipt is not on the remote" in said
    assert "DEPLOYED, but" not in said


# ── review 3, A: an error before the fleet commit leaves the tree as it was ──


def test_a_failed_fleet_commit_puts_back_what_the_run_wrote(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    (m["a"] / "notes.txt").write_text("another session's work\n")  # an untracked file
    before = snapshot(m["a"])
    real = gitrepo.commit_paths
    calls: list[int] = []

    def flaky(repo: Path, paths: list[Path], message: str) -> str:
        if not calls:
            calls.append(1)
            raise gitrepo.GitError("Unable to create '.git/index.lock': File exists.")
        return real(repo, paths, message)

    monkeypatch.setattr(gitrepo, "commit_paths", flaky)
    with pytest.raises(BayError) as err:
        up_at(m, "a", "webapp", allow_dirty=True)
    assert "cannot commit the fleet repo" in str(err.value)
    assert snapshot(m["a"]) == before  # the plan record is gone, the locks are back
    assert box.deploys == []
    # The retry is not blocked by the leftover of the failed run.
    result = up_at(m, "a", "webapp", allow_dirty=True)
    assert result["result"] == "ok"


def test_ctrl_c_before_the_claim_push_undoes_the_commit(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    m = machines
    before = snapshot(m["a"])

    def interrupted(repo: Path) -> tuple[bool, str | None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(fleetsync, "push_upstream", interrupted)
    with pytest.raises(KeyboardInterrupt):
        up_at(m, "a", "webapp")
    assert snapshot(m["a"]) == before
    assert "STOPPED before the box was touched (interrupted)" in capsys.readouterr().err
    assert box.deploys == []


def test_a_second_ctrl_c_cannot_cut_the_unwind_in_half(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """review 4, item 7: SIGINT is ignored while the unwind runs, then comes back."""
    import signal

    m = machines
    before = snapshot(m["a"])
    handler = signal.getsignal(signal.SIGINT)
    seen: list[Any] = []
    real_undo = fleetsync.undo

    def interrupted(repo: Path) -> tuple[bool, str | None]:
        raise KeyboardInterrupt

    def undo(*a: Any, **k: Any) -> Any:
        seen.append(signal.getsignal(signal.SIGINT))
        os.kill(os.getpid(), signal.SIGINT)  # the second Ctrl-C, in the middle
        return real_undo(*a, **k)

    monkeypatch.setattr(fleetsync, "push_upstream", interrupted)
    monkeypatch.setattr(fleetsync, "undo", undo)
    with pytest.raises(KeyboardInterrupt):
        up_at(m, "a", "webapp")
    assert seen == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGINT) is handler
    assert snapshot(m["a"]) == before
    err = capsys.readouterr().err
    assert "Bay is putting the fleet back" in err
    assert "STOPPED before the box was touched (interrupted)" in err


def test_an_unwind_that_cannot_tell_if_the_claim_landed_undoes_nothing(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """review 4, item 7: the fetch in the unwind has 10 s; on a timeout, keep the commit."""
    m = machines
    real_push, real_fetch = fleetsync.push_upstream, fleetsync._fetch
    limits: list[int] = []
    pushed: list[bool] = []

    def pushed_then_interrupted(repo: Path) -> Any:
        real_push(repo)
        pushed.append(True)
        raise KeyboardInterrupt

    def slow_fetch(repo: Path, *, timeout: int = 60) -> str | None:
        if not pushed:  # the plan's own fetch
            return real_fetch(repo, timeout=timeout)
        limits.append(timeout)
        return "git fetch timed out"

    monkeypatch.setattr(fleetsync, "push_upstream", pushed_then_interrupted)
    monkeypatch.setattr(fleetsync, "_fetch", slow_fetch)
    with pytest.raises(KeyboardInterrupt):
        up_at(m, "a", "webapp")
    assert limits == [applymod.UNWIND_FETCH_SECONDS] == [10]
    # The commit is on the remote, and the unwind did not undo it locally.
    assert git(m["a"], "rev-parse", "HEAD") == git(m["remote"], "rev-parse", "main")
    assert '"result": "pending"' in git(m["a"], "show", "HEAD:projects/webapp/bay.lock")
    err = capsys.readouterr().err
    assert "STOPPED DURING THE CLAIM (interrupted)" in err
    assert "cannot tell" in err and "git fetch timed out" in err
    assert "branch -r --contains" in err
    assert "STOPPED before the box was touched" not in err


def test_ctrl_c_during_the_deploy_with_json_is_one_document(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review 5, item 4: after the box was touched, exit 130 and one JSON document."""
    m = machines

    def interrupted(cx: Context, box_env: str, **kw: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(applymod, "default_deploy", interrupted)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 130, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "interrupted" and doc["box_touched"] is True
    assert doc["next"] == f"bay --fleet {m['a']} up production --project webapp"
    assert doc["fleet"]["name"] == "testfleet"
    assert lock_at(m["a"], "webapp")["envs"]["production"]["result"] == "pending"
    assert '"result": "pending"' in git(m["a"], "show", "@{u}:projects/webapp/bay.lock")


def test_ctrl_c_before_the_box_with_json_is_one_document(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    before = snapshot(m["a"])

    def interrupted(repo: Path) -> tuple[bool, str | None]:
        raise KeyboardInterrupt

    monkeypatch.setattr(fleetsync, "push_upstream", interrupted)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 130, result.output
    doc = json.loads(result.stdout)
    assert doc["reason"] == "interrupted" and doc["box_touched"] is False
    assert snapshot(m["a"]) == before
    assert box.deploys == []


def test_ctrl_c_after_the_claim_says_what_the_remote_holds(
    machines: dict[str, Path], box: FakeBox, capsys: pytest.CaptureFixture[str]
) -> None:
    m = machines

    def interrupted(cx: Context, box_env: str, **kw: Any) -> None:
        raise KeyboardInterrupt

    # An injected deployer counts as the playbook start, so stop just before it.
    real_run = applymod.run_deployer

    def stop_before(deployer: Any, cx: Context, target: str, **kw: Any) -> None:
        raise KeyboardInterrupt

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(applymod, "run_deployer", stop_before)
        with pytest.raises(KeyboardInterrupt):
            up_at(m, "a", "webapp", deploy=interrupted)
    assert real_run is not None
    err = capsys.readouterr().err
    assert "STOPPED AFTER THE CLAIM (interrupted)" in err
    assert "it repairs this host's own stopped run" in err
    remote_lock = git(m["a"], "show", "@{u}:projects/webapp/bay.lock")
    assert '"result": "pending"' in remote_lock
    record = lock_at(m["a"], "webapp")["envs"]["production"]
    assert record["claimed_run"] == fleetsync.run_id()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _pending_with_run(
    m: dict[str, Path], run: str, message: str, *, by: str | None = None
) -> None:
    raw = lock_at(m["a"], "webapp")
    raw["envs"]["production"].update(
        {"result": "pending", "plan_id": "0" * 12, "claimed_by": by or fleetsync.hostname(),
         "claimed_at": fleetsync.now_iso(), "claimed_run": run}
    )
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], message)
    git(m["a"], "push", "-q")


def _plan_status(m: dict[str, Path]) -> dict[str, Any]:
    return planmod.make_plan(proj_at(m["a"], "status", m["app"]), planmod.PlanOptions())


def _my_parts() -> list[str]:
    parts = fleetsync.run_id().split(":")
    assert len(parts) == 4, "the test needs /etc/machine-id and /proc"
    return parts


def test_a_retry_after_a_crash_on_this_host_goes_through(
    machines: dict[str, Path], box: FakeBox
) -> None:
    """The reviewer's sequence: the run dies after its claim; the next run needs no flag."""
    m = machines

    class Crash(BaseException):
        pass

    def crash(cx: Context, box_env: str, **kw: Any) -> None:
        raise Crash()

    with pytest.raises(Crash):
        up_at(m, "a", "webapp", deploy=crash)
    raw = lock_at(m["a"], "webapp")
    record = raw["envs"]["production"]
    assert record["result"] == "pending" and record["claimed_run"] == fleetsync.run_id()
    # The crashed run was another process of this machine: its pid is gone now.
    machine, ns, _, start = _my_parts()
    record["claimed_run"] = f"{machine}:{ns}:{_dead_pid()}:{start}"
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], "the crashed run's claim")
    git(m["a"], "push", "-q")
    plan = planmod.make_plan(proj_at(m["a"], "webapp", m["app"]), planmod.PlanOptions())
    assert plan["holder"] is None and plan["reason"] is None
    assert any("repairs this host's own stopped run" in n for n in plan["notes"])
    assert up_at(m, "a", "webapp")["result"] == "ok"


def test_a_reused_pid_with_another_start_time_goes_through(
    machines: dict[str, Path], box: FakeBox
) -> None:
    m = machines
    up_at(m, "a", "webapp")
    machine, ns, pid, start = _my_parts()
    _pending_with_run(m, f"{machine}:{ns}:{pid}:{int(start) + 1}", "a reused pid")
    plan = _plan_status(m)
    assert plan["holder"] is None and plan["reason"] is None


def test_a_live_run_of_this_machine_blocks(machines: dict[str, Path], box: FakeBox) -> None:
    m = machines
    up_at(m, "a", "webapp")
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        _pending_with_run(m, fleetsync.run_id(sleeper.pid), "a live run on this host")
        assert _plan_status(m)["reason"] == "env_busy"
    finally:
        sleeper.kill()
        sleeper.wait()


@pytest.mark.parametrize("change", ["machine", "pidns", "empty", "garbage"])
def test_a_claim_this_machine_cannot_judge_blocks(
    machines: dict[str, Path], box: FakeBox, change: str
) -> None:
    """Same host name, but another machine id, another pid namespace, or no run name."""
    m = machines
    up_at(m, "a", "webapp")
    machine, ns, _, start = _my_parts()
    dead = _dead_pid()
    run = {
        "machine": f"{'0' if machine[0] != '0' else '1'}{machine[1:]}:{ns}:{dead}:{start}",
        "pidns": f"{machine}:{int(ns) + 1}:{dead}:{start}",
        "empty": "",
        "garbage": f"{machine}:{ns}:not-a-pid",
    }[change]
    _pending_with_run(m, run, f"a claim with {change}")
    plan = _plan_status(m)
    assert plan["reason"] == "env_busy"
    assert plan["holder"]["claimed_by"] == fleetsync.hostname()


def test_a_dead_run_of_another_host_name_blocks(machines: dict[str, Path], box: FakeBox) -> None:
    """review 5, A1: a clone can share the machine id and the first pid namespace."""
    m = machines
    up_at(m, "a", "webapp")
    machine, ns, _, start = _my_parts()
    _pending_with_run(m, f"{machine}:{ns}:{_dead_pid()}:{start}", "a clone's claim", by="clone-b")
    plan = _plan_status(m)
    assert plan["reason"] == "env_busy" and plan["holder"]["claimed_by"] == "clone-b"


@pytest.mark.parametrize("signal_says", ["permission", "alive"])
def test_a_run_that_proc_hides_blocks(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch, signal_says: str
) -> None:
    """review 5, A2: hidepid hides another user's process from /proc; kill(pid, 0) still sees it."""
    m = machines
    up_at(m, "a", "webapp")
    machine, ns, _, start = _my_parts()
    hidden = _dead_pid()
    _pending_with_run(m, f"{machine}:{ns}:{hidden}:{start}", "a hidden run")
    real_start, real_kill = fleetsync.proc_start, os.kill

    def kill(pid: int, sig: int) -> None:
        if pid != hidden:
            return real_kill(pid, sig)
        if signal_says == "permission":
            raise PermissionError(1, "Operation not permitted")
        return None

    monkeypatch.setattr(fleetsync, "proc_start", lambda pid: fleetsync.GONE if pid == hidden else real_start(pid))
    monkeypatch.setattr(os, "kill", kill)
    assert _plan_status(m)["reason"] == "env_busy"


def test_a_machine_with_no_machine_id_writes_an_empty_run_and_never_judges(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    up_at(m, "a", "webapp")
    machine, ns, _, start = _my_parts()
    _pending_with_run(m, f"{machine}:{ns}:{_dead_pid()}:{start}", "a dead run")
    monkeypatch.setattr(fleetsync, "machine_tag", lambda: None)
    assert fleetsync.claim_fields()["claimed_run"] == ""
    assert _plan_status(m)["reason"] == "env_busy"


# ── review 3, I: --allow-dirty never covers the files only Bay writes ───────


@pytest.mark.parametrize("path", ["group_vars/all/services.yml", "projects/webapp/bay.lock"])
def test_a_hand_edit_in_a_bay_file_refuses_even_with_allow_dirty(
    machines: dict[str, Path], box: FakeBox, playbooks: list[tuple[str, str]], path: str
) -> None:
    m = machines
    up_at(m, "a", "webapp")
    target = m["a"] / path
    # A hand edit that still parses (a lock must stay valid JSON to be planned).
    edit = "\n# a hand edit\n" if path.endswith(".yml") else "\n"
    target.write_text(target.read_text() + edit)

    def without_plans(snap: dict[str, Any]) -> dict[str, Any]:
        # A refused plan keeps its record under plans/, as every refused plan does.
        files = {k: v for k, v in snap["files"].items() if not k.startswith("plans/")}
        status = "\n".join(
            line for line in snap["status"].splitlines() if not line[3:].startswith("plans/")
        )
        return {**snap, "files": files, "status": status}

    before = without_plans(snapshot(m["a"]))
    for flags in ({}, {"allow_dirty": True}):
        with pytest.raises(applymod.Refused) as err:
            up_at(m, "a", "webapp", **flags)
        assert err.value.reason == "dirty" and err.value.exit_code == 41
        text = str(err.value)
        assert "only Bay writes" in text and path in text
        assert f"git checkout HEAD -- {path}" in text
    assert without_plans(snapshot(m["a"])) == before  # the hand edit is still there
    # bay deploy --allow-dirty refuses it too.
    deployed = _op(m, "deploy", "--allow-dirty")
    assert getattr(deployed.exception, "reason", None) == "dirty", deployed.output
    assert "only Bay writes" in str(deployed.exception)
    assert playbooks == []


@pytest.mark.parametrize("path", ["group_vars/all/services.yml", "projects/webapp/bay.lock"])
def test_the_restore_hint_also_undoes_a_staged_edit(
    machines: dict[str, Path], box: FakeBox, path: str
) -> None:
    """review 4, item 4: `git checkout -- <path>` restores from the index, so it keeps a staged edit."""
    import re

    m = machines
    up_at(m, "a", "webapp")
    target = m["a"] / path
    clean = target.read_text()
    target.write_text(clean + ("\n# a hand edit\n" if path.endswith(".yml") else "\n"))
    git(m["a"], "add", path)
    with pytest.raises(applymod.Refused) as err:
        up_at(m, "a", "webapp")
    hint = re.search(r"\(git checkout HEAD -- ([^)]*)\)", str(err.value))
    assert hint is not None, str(err.value)
    git(m["a"], "checkout", "HEAD", "--", *hint.group(1).split())
    assert target.read_text() == clean
    assert git(m["a"], "status", "--porcelain", "--", path) == ""


def test_a_compiled_working_tree_services_file_deploys_with_allow_dirty(
    machines: dict[str, Path], box: FakeBox, playbooks: list[tuple[str, str]]
) -> None:
    """review 4, item 3: `bay compile --working-tree` writes a file whose header matches."""
    m = machines
    up_at(m, "a", "webapp")
    toml = m["a"] / "projects" / "status" / "bay.toml"
    toml.write_text(toml.read_text().replace('MODE = "one"', 'MODE = "two"'))
    compiled = cli({"fleet": m["a"], "app": m["app"]}, "compile", "--working-tree")
    assert compiled.exit_code == 0, compiled.output
    services = "group_vars/all/services.yml"
    assert services in git(m["a"], "status", "--porcelain")
    refused = _op(m, "deploy")
    assert getattr(refused.exception, "reason", None) == "dirty", refused.output
    text = str(refused.exception) + " " + str(getattr(refused.exception, "hint", ""))
    assert "only Bay writes" not in text
    assert "Commit them" in text and "--allow-dirty" in text and services in text
    assert playbooks == []
    deployed = _op(m, "deploy", "--allow-dirty")
    assert deployed.exit_code == 0, deployed.output
    assert playbooks == [("deploy", "production")]
    # A hand edit after the compile breaks the header: refused again, also with the flag.
    path = m["a"] / services
    path.write_text(path.read_text() + "# a hand edit\n")
    again = _op(m, "deploy", "--allow-dirty")
    assert getattr(again.exception, "reason", None) == "dirty", again.output
    assert "only Bay writes" in str(again.exception)
    assert playbooks == [("deploy", "production")]


# ── review 3, G: a status probe cannot make a starting run busy ─────────────


def test_the_run_lock_waits_out_a_status_probe(machines: dict[str, Path]) -> None:
    import fcntl
    import threading

    m = machines
    with fleetsync.run_lock(m["a"], "up", "production"):
        pass
    lock_path, _ = fleetsync._lock_paths(m["a"])  # type: ignore[misc]
    fd = os.open(lock_path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # what lock_holder holds for a moment
    timer = threading.Timer(0.3, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
    timer.start()
    try:
        with fleetsync.run_lock(m["a"], "up", "production") as info:
            assert info is not None and info["pid"] == os.getpid()
    finally:
        timer.join()


def test_a_run_lock_held_longer_than_the_wait_is_busy(
    machines: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    m = machines
    monkeypatch.setattr(fleetsync, "LOCK_WAIT_SECONDS", 0.2)
    with fleetsync.run_lock(m["a"], "up", "production"):
        pass
    lock_path, _ = fleetsync._lock_paths(m["a"])  # type: ignore[misc]
    fd = os.open(lock_path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        with pytest.raises(fleetsync.SyncError) as err:
            with fleetsync.run_lock(m["a"], "up", "production"):
                pass
        assert err.value.reason == "fleet_busy"
    finally:
        os.close(fd)


# ── review 3, H: a claim from a clock that is ahead ─────────────────────────


def test_a_claim_in_the_future_blocks_until_it_expires_and_says_the_clocks_differ(
    machines: dict[str, Path], box: FakeBox
) -> None:
    from datetime import UTC, datetime, timedelta

    m = machines
    up_at(m, "a", "webapp")
    raw = lock_at(m["a"], "webapp")
    ahead = datetime.now(UTC) + timedelta(minutes=45)
    raw["envs"]["production"].update(
        {"result": "pending", "claimed_by": "machine-z",
         "claimed_at": ahead.strftime("%Y-%m-%dT%H:%M:%SZ")}
    )
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], "a claim from a clock 45 min ahead")
    git(m["a"], "push", "-q")
    plan = planmod.make_plan(proj_at(m["a"], "status", m["app"]), planmod.PlanOptions())
    assert plan["reason"] == "env_busy"
    holder = plan["holder"]
    assert holder["expires_at"] == (ahead + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert holder["clock_ahead_seconds"] > 44 * 60
    message = next(b for b in plan["blockers"] if "is deploying" in b)
    assert "the clocks of this machine and machine-z differ" in message
    # Past that point it no longer blocks.
    later = ahead + timedelta(minutes=31)
    assert planmod.foreign_claim(
        Context.for_fleet_root(m["a"]), {"production"}, "x" * 12, now=later
    ) is None


def test_a_claim_far_in_the_future_names_take_over(
    machines: dict[str, Path], box: FakeBox
) -> None:
    """review 4, item 8: no new rule for a far clock; the way out is --take-over."""
    from datetime import UTC, datetime, timedelta

    m = machines
    up_at(m, "a", "webapp")
    raw = lock_at(m["a"], "webapp")
    ahead = datetime.now(UTC) + timedelta(days=400)
    raw["envs"]["production"].update(
        {"result": "pending", "claimed_by": "machine-z",
         "claimed_at": ahead.strftime("%Y-%m-%dT%H:%M:%SZ")}
    )
    lockfile.write(lockfile.lock_path(m["a"], "webapp"), raw)
    commit_all(m["a"], "a claim from a clock 400 days ahead")
    git(m["a"], "push", "-q")
    plan = planmod.make_plan(proj_at(m["a"], "status", m["app"]), planmod.PlanOptions())
    assert plan["reason"] == "env_busy"
    message = next(b for b in plan["blockers"] if "is deploying" in b)
    assert "--take-over" in message
    assert "the clocks of this machine and machine-z differ" in message
    assert up_at(m, "a", "webapp", take_over=True)["result"] == "ok"


# ── review 3, E: a git failure in the push step keeps the result ────────────


def _git_breaks_on_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(repo: Path, **_: Any) -> Any:
        raise fleetsync.SyncError(
            "git status failed: fatal: bad config line 3", reason="git_failed"
        )

    monkeypatch.setattr(fleetsync, "push_with_rebase", broken)


def test_a_git_failure_after_an_ok_deploy_is_receipt_unpushed(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    _git_breaks_on_push(monkeypatch)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 44, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "ok" and doc["reason"] == "receipt_unpushed"
    assert doc["pushed"] is False and "bad config line 3" in doc["push_error"]
    assert doc["box_touched"] is True and doc["next"] == "bay fleet push"


def test_a_git_failure_after_a_failed_deploy_keeps_deploy_failed(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    _git_breaks_on_push(monkeypatch)
    box.fail = True
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "failed" and doc["reason"] == "deploy_failed"
    assert "bad config line 3" in doc["push_error"]


# ── review 3, F: a receipt that cannot be read is not a missing receipt ─────


def test_an_unreadable_receipt_is_tried_again(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    monkeypatch.setattr(applymod, "RECEIPT_READ_PAUSE", 0)
    real = box.read
    calls: list[int] = []

    def flaky(cx: Context, box_env: str) -> list[dict[str, Any]]:
        if not box.deploys:  # the plan's own read
            return real(cx, box_env)
        calls.append(1)
        if len(calls) < 3:
            return [{"env": box_env, "box": "192.0.2.11", "receipt": None,
                     "error": "unreachable: ssh timed out"}]
        return real(cx, box_env)

    monkeypatch.setattr(planmod, "default_receipt_reader", flaky)
    result = up_at(m, "a", "webapp")
    assert result["result"] == "ok" and len(calls) == 3
    assert result["receipt_unread"] == []


def test_a_receipt_that_stays_unreadable_says_the_deploy_ran(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    monkeypatch.setattr(applymod, "RECEIPT_READ_PAUSE", 0)
    calls: list[int] = []

    def never(cx: Context, box_env: str) -> list[dict[str, Any]]:
        if box.deploys:  # not the plan's own read
            calls.append(1)
        return [{"env": box_env, "box": "192.0.2.11", "receipt": None,
                 "error": "unreachable: ssh timed out"}]

    monkeypatch.setattr(planmod, "default_receipt_reader", never)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json")
    assert result.exit_code == 1, result.output
    doc = json.loads(result.stdout)
    assert len(calls) == applymod.RECEIPT_READ_TRIES
    assert doc["result"] == "failed" and doc["reason"] == "deploy_failed"
    assert doc["error"].startswith("the deploy ran, but the receipt of box box-1")
    assert doc["receipt_unread"] == [
        {"box": "box-1", "host": "192.0.2.11", "error": "unreachable: ssh timed out"}
    ]
    assert doc["next"].endswith("up production --project webapp")
    assert box.deploys == ["production"]
    assert lock_at(m["a"], "webapp")["envs"]["production"]["result"] == "failed"


# ── review 3, J: no fleet or several, with --json, is a JSON document ───────


def _two_fleets(tmp_path: Path) -> Path:
    for name in ("alpha", "beta"):
        (tmp_path / "many" / name).mkdir(parents=True)
        (tmp_path / "many" / name / "bay.fleet.toml").write_text(f'name = "{name}"\n')
    return tmp_path / "many"


@pytest.mark.parametrize("argv", [["status", "--json"], ["fleet", "status", "--json"],
                                  ["up", "--json"], ["plan", "--json"]])
def test_several_fleets_with_json_is_a_json_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    from bay_cli import cli as climod

    monkeypatch.chdir(_two_fleets(tmp_path))
    monkeypatch.setattr(sys, "argv", ["bay", *argv])
    with pytest.raises(SystemExit) as done:
        climod._main()
    assert done.value.code != 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["reason"] == "fleet_ambiguous"
    assert [Path(c).name for c in doc["candidates"]] == ["alpha", "beta"]
    assert doc["error"].startswith("more than one fleet lives in ")


@pytest.mark.parametrize("argv", [["status", "--json"], ["up", "--json"]])
def test_no_fleet_with_json_is_a_json_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    from bay_cli import cli as climod

    empty = tmp_path / "work" / "empty"
    empty.mkdir(parents=True)
    monkeypatch.chdir(empty)
    monkeypatch.setattr(sys, "argv", ["bay", *argv])
    with pytest.raises(SystemExit):
        climod._main()
    doc = json.loads(capsys.readouterr().out)
    assert doc["reason"] == "no_fleet" and doc["candidates"] == []


def test_a_failed_fleet_commit_of_a_remove_puts_back_what_it_wrote(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = machines
    w = {"fleet": m["a"], "app": m["app"]}
    up_at(m, "a", "webapp")
    plan = json.loads(cli(w, "remove", "webapp", "--json").stdout)
    assert cli(w, "approve", plan["plan_id"], "--reason", "retired").exit_code == 0
    before = snapshot(m["a"])

    def broken(repo: Path, paths: list[Path], message: str) -> str:
        raise gitrepo.GitError("Unable to create '.git/index.lock': File exists.")

    monkeypatch.setattr(gitrepo, "commit_paths", broken)
    result = cli(w, "up", "production", "--plan-id", plan["plan_id"], "--json")
    assert result.exit_code != 0
    assert json.loads(result.stdout)["box_touched"] is False
    assert snapshot(m["a"]) == before
    assert box.deploys == ["production"]  # only the first up


# ── review 5, items 5 to 8: what a person sees ──────────────────────────────


def test_the_env_busy_message_names_the_time_zone(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """item 7: local time with its zone, so it can be set against the UTC in the JSON."""
    import re
    import time

    m = machines
    up_at(m, "a", "webapp")
    _pending_with_run(m, "", "a claim of another run")
    monkeypatch.setenv("TZ", "EST+5")
    time.tzset()
    try:
        plan = _plan_status(m)
    finally:
        monkeypatch.undo()
        time.tzset()
    message = next(b for b in plan["blockers"] if "is deploying" in b)
    assert re.search(r"since \d\d:\d\d EST[ .]", message), message
    assert re.search(r"expires at \d\d:\d\d EST,", message), message


def test_the_env_busy_plan_id_is_the_claimers_plan(
    machines: dict[str, Path], box: FakeBox
) -> None:
    """item 8: the id in the text is holder.plan_id, the plan of the run that claimed."""
    m = machines
    seen: dict[str, Any] = {}
    real = applymod.default_deploy

    def a_deploy(cx: Context, box_env: str, **kw: Any) -> None:
        with as_machine("machine-b"):
            try:
                up_at(m, "b", "status")
            except applymod.Refused as exc:
                seen["plan"] = exc.plan
        seen["claimed"] = lock_at(m["a"], "webapp")["envs"]["production"]["plan_id"]
        real(cx, box_env, **kw)

    result = up_at(m, "a", "webapp", deploy=a_deploy)
    holder = seen["plan"]["holder"]
    assert holder["plan_id"] == seen["claimed"] == result["plan_id"]
    message = next(b for b in seen["plan"]["blockers"] if "is deploying" in b)
    assert f"(plan {result['plan_id']})" in message
    assert git(m["a"], "cat-file", "-t", f"@{{u}}:plans/{result['plan_id']}.json") == "blob"
    # A record with no plan id: the text leaves the id out.
    no_id = planmod.env_busy_message({**holder, "plan_id": None})
    assert "(plan" not in no_id and "None" not in no_id


def test_an_alert_under_log_reaches_the_terminal_too(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """item 5: with --log, fd 2 is the log; an alert also goes to the saved terminal fd."""
    from bay_cli.commands import project_cmd

    log = tmp_path / "up.log"
    with project_cmd.routed_output(True, log):
        fleetsync.alert("Bay is putting the fleet back.")
        fleetsync.notice("a progress line")
    err = capfd.readouterr().err
    assert "Bay is putting the fleet back." in err
    assert "a progress line" not in err
    text = log.read_text()
    assert "Bay is putting the fleet back." in text and "a progress line" in text
    assert fleetsync.TERMINAL == []


@pytest.mark.parametrize("when", ["before", "during"])
def test_an_interrupt_with_json_and_log_shows_on_stderr(
    machines: dict[str, Path], box: FakeBox, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    m = machines
    log = m["tmp"] / "up.log"

    def interrupted(*a: Any, **k: Any) -> Any:
        raise KeyboardInterrupt

    if when == "before":
        monkeypatch.setattr(fleetsync, "push_upstream", interrupted)
    else:
        monkeypatch.setattr(applymod, "default_deploy", interrupted)
    result = cli({"fleet": m["a"], "app": m["app"]}, "up", "--json", "--log", str(log))
    assert result.exit_code == 130
    doc = json.loads(result.stdout)
    assert doc["reason"] == "interrupted"
    again = f"bay --fleet {m['a']} up production --project webapp"
    if when == "before":
        lines = ["Bay is putting the fleet back", "STOPPED before the box was touched"]
    else:
        lines = ["INTERRUPTED DURING THE DEPLOY", "may be half deployed", "stays pending", again]
    for line in lines:
        assert line in result.stderr, result.stderr
        assert line in log.read_text()
