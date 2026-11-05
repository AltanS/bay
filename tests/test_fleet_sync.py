"""One fleet, several machines (2.12): pull first, claim before the deploy, run lock.

Two clones of one fleet ("machine A" and "machine B") share a bare remote and
the fake box of test_plan_up. No network, no SSH, no Ansible.
"""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import (  # noqa: E402,F401  (fixtures are used by name)
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
    real_push = gitrepo.push
    calls: list[str] = []

    def push(repo: Path) -> tuple[bool, str | None]:
        if Path(repo).resolve() == m["b"].resolve() and not calls:
            calls.append("claim")
            git(m["a"], "push", "-q")  # A's claim lands between B's commit and B's claim
        return real_push(repo)

    monkeypatch.setattr(gitrepo, "push", push)
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

    real_push = gitrepo.push
    calls = _land_a_before_b_claims(m, monkeypatch)
    deployed: list[str] = []

    def b_deploy(cx: Context, box_env: str, **_: Any) -> None:
        deployed.append(box_env)

    with pytest.raises(fleetsync.SyncError) as err:
        up_at(m, "b", "status", deploy=b_deploy)
    assert calls == ["claim"]
    exc = err.value
    assert exc.reason == "claim_refused" and exc.exit_code == 42
    assert exc.box_touched is False
    assert exc.next_cmd is not None and "up production --project status" in exc.next_cmd
    assert str(exc).startswith("Stopped before the box was touched. The fleet remote moved: ")
    assert "Nothing was deployed. Run bay up again: it pulls first." in str(exc)
    assert deployed == []  # the box was never touched
    assert snapshot(m["b"]) == before  # HEAD, index, status and bytes as before
    assert git(m["remote"], "rev-parse", "main") == a_head

    # The next bay up pulls A's commits first, and its compile holds A's pin.
    monkeypatch.setattr(gitrepo, "push", real_push)
    again = up_at(m, "b", "status")
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
    real_push = gitrepo.push
    calls: list[str] = []

    def push(repo: Path) -> tuple[bool, str | None]:
        calls.append(str(repo))
        pushed, problem = real_push(repo)
        if len(calls) == 1:
            assert pushed
            return False, "git push timed out"
        return pushed, problem

    monkeypatch.setattr(gitrepo, "push", push)
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

    monkeypatch.setattr(gitrepo, "push", push)
    with pytest.raises(fleetsync.SyncError) as err:
        up_at(m, "a", "webapp")
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
    sidecar = m["a"] / ".git" / fleetsync.HOLDER_NAME
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
    real = gitrepo.push

    def push(repo: Path) -> tuple[bool, str | None]:
        pushes.append(str(repo))
        return real(repo)

    monkeypatch.setattr(gitrepo, "push", push)
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
