"""Fleet sync against git itself (2.12 review): where a push goes, fail closed, locks.

Plain git repos in a temporary folder. No fleet, no box. Each test names the
review finding it guards.
"""

from __future__ import annotations

import fcntl
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_plan_up import _git_identity  # noqa: E402,F401  (autouse fixture)

from bay_cli import fleetsync, gitrepo  # noqa: E402


def g(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    """``origin`` (bare), ``fork`` (bare) and ``a``, a clone of origin with one commit."""
    origin, fork, a = tmp_path / "origin.git", tmp_path / "fork.git", tmp_path / "a"
    for bare in (origin, fork):
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(a)], check=True, capture_output=True)
    (a / "lock").write_text("1\n")
    g(a, "add", ".")
    g(a, "commit", "-q", "-m", "init")
    g(a, "push", "-q", "-u", "origin", "main")
    g(a, "remote", "add", "fork", str(fork))
    return {"origin": origin, "fork": fork, "a": a, "tmp": tmp_path}


def _commit(repo: Path, rel: str, text: str, message: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    g(repo, "add", rel)
    g(repo, "commit", "-q", "-m", message)
    return g(repo, "rev-parse", "HEAD")


def _clone(origin: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True, capture_output=True)


def _rev(bare: Path) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", "main"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() or None


def _other_machine_moves(r: dict[str, Path]) -> None:
    b = r["tmp"] / "b"
    _clone(r["origin"], b)
    _commit(b, "other", "1\n", "B")
    g(b, "push", "-q")


# ── 2. the claim goes to the branch @{u} names ──────────────────────────────


@pytest.mark.parametrize(
    "config", [("branch.main.pushRemote", "fork"), ("remote.pushDefault", "fork")]
)
def test_claim_pushes_to_the_tracked_branch_whatever_the_push_config(
    repos: dict[str, Path], config: tuple[str, str]
) -> None:
    a = repos["a"]
    g(a, "config", *config)
    before = g(a, "rev-parse", "HEAD")
    commit = _commit(a, "lock", "2\n", "claim")
    said: list[str] = []
    undo = fleetsync.Undo(before=before, restore=["lock"])
    fleetsync.claim(a, commit, undo, again="x", say=said.append)
    assert _rev(repos["origin"]) == commit
    assert _rev(repos["fork"]) is None
    assert said == ["fleet: claimed (pushed the fleet commit before the deploy)"]


def test_claim_whose_push_lands_elsewhere_is_refused(repos: dict[str, Path]) -> None:
    a = repos["a"]
    g(a, "config", "remote.origin.pushurl", str(repos["fork"]))
    before = g(a, "rev-parse", "HEAD")
    commit = _commit(a, "lock", "2\n", "claim")
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.claim(
            a, commit, fleetsync.Undo(before=before, restore=["lock"]), again="x", say=print
        )
    assert err.value.reason == "claim_refused" and err.value.exit_code == 42
    assert "the push did not reach the tracked branch" in str(err.value)
    assert _rev(repos["origin"]) == before
    # The run's commit is undone: the deploy never started on a claim nobody can see.
    assert g(a, "rev-parse", "HEAD") == before
    assert (a / "lock").read_text() == "1\n"


def test_receipt_and_fleet_push_go_to_the_tracked_branch(repos: dict[str, Path]) -> None:
    a = repos["a"]
    g(a, "config", "branch.main.pushRemote", "fork")
    commit = _commit(a, "receipt", "r\n", "receipt")
    done = fleetsync.push_with_rebase(a)
    assert done.pushed and done.problem is None
    assert _rev(repos["origin"]) == commit and _rev(repos["fork"]) is None
    g(a, "config", "remote.origin.pushurl", str(repos["fork"]))
    _commit(a, "receipt", "r2\n", "receipt 2")
    done = fleetsync.push_with_rebase(a)
    assert not done.pushed
    assert "did not reach the tracked branch" in str(done.problem)
    assert _rev(repos["origin"]) == commit


def test_deploy_push_ahead_whose_push_lands_elsewhere_refuses(repos: dict[str, Path]) -> None:
    a = repos["a"]
    g(a, "config", "remote.origin.pushurl", str(repos["fork"]))
    _commit(a, "lock", "2\n", "local")
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.prepare_playbook(a, verb="deploy")
    assert err.value.reason == "claim_refused"
    assert "did not reach the tracked branch" in str(err.value)


def test_branch_with_no_upstream_stays_no_upstream(repos: dict[str, Path]) -> None:
    a = repos["a"]
    g(a, "branch", "--unset-upstream")
    assert fleetsync.upstream_target(a) is None
    st = fleetsync.state(a)
    assert st.no_upstream and st.upstream is None
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.prepare_playbook(a, verb="deploy")
    assert err.value.reason == "no_upstream"


# ── 5. the receipt rebase and a duplicate plan record ───────────────────────


def test_receipt_rebase_removes_a_plan_record_the_remote_holds(repos: dict[str, Path]) -> None:
    a = repos["a"]
    b = repos["tmp"] / "b"
    _clone(repos["origin"], b)
    _commit(b, "plans/p1.json", '{"a": 1}\n', "B up")
    g(b, "push", "-q")
    # A has the same plan record untracked, and a receipt commit to push.
    (a / "plans").mkdir()
    (a / "plans" / "p1.json").write_text('{"a": 1, "created": 2}\n')
    _commit(a, "receipt", "r\n", "A receipt")
    done = fleetsync.push_with_rebase(a)
    assert done.pushed and done.rebased == 1, done
    assert (a / "plans" / "p1.json").read_text() == '{"a": 1}\n'
    assert g(a, "status", "--porcelain") == ""


def test_failed_receipt_rebase_puts_the_plan_record_back(repos: dict[str, Path]) -> None:
    a = repos["a"]
    b = repos["tmp"] / "b"
    _clone(repos["origin"], b)
    _commit(b, "plans/p1.json", '{"a": 1}\n', "B up")
    _commit(b, "lock", "b\n", "B lock")
    g(b, "push", "-q")
    (a / "plans").mkdir()
    (a / "plans" / "p1.json").write_text("mine\n")
    _commit(a, "lock", "a\n", "A lock")  # conflicts with B's lock
    done = fleetsync.push_with_rebase(a)
    assert not done.pushed and done.conflicts == ["lock"]
    assert (a / "plans" / "p1.json").read_text() == "mine\n"
    assert not fleetsync._rebase_in_progress(a)


# ── 9. fail closed on git errors ────────────────────────────────────────────


def _fail_git(monkeypatch: pytest.MonkeyPatch, word: str, *, rc: int | None) -> None:
    real = gitrepo._run

    def run(repo: Path, *args: str, **kw: Any) -> subprocess.CompletedProcess[str] | None:
        if word in args:
            if rc is None:
                return None
            return subprocess.CompletedProcess(args, rc, "", "fatal: index file corrupt")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitrepo, "_run", run)


@pytest.mark.parametrize("rc", [None, 128])
def test_dirty_paths_fails_closed(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch, rc: int | None
) -> None:
    (repos["a"] / "scratch").write_text("x\n")
    _fail_git(monkeypatch, "status", rc=rc)
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.dirty_paths(repos["a"])
    assert err.value.reason == "git_failed"
    assert "git status" in str(err.value)
    assert ("timed out" if rc is None else "index file corrupt") in str(err.value)
    with pytest.raises(fleetsync.SyncError):
        fleetsync.prepare_playbook(repos["a"], verb="deploy", allow_dirty=False)


@pytest.mark.parametrize("word", ["remote", "--show-toplevel"])
def test_can_push_and_state_raise_when_git_fails(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    _fail_git(monkeypatch, word, rc=None)
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.can_push(repos["a"])
    assert err.value.reason == "git_failed"
    with pytest.raises(fleetsync.SyncError):
        fleetsync.state(repos["a"])


def test_no_remote_and_inside_a_bigger_repo_are_not_errors(tmp_path: Path) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    g(lone, "init", "-q")
    _commit(lone, "f", "1\n", "one")
    assert fleetsync.can_push(lone) is False
    assert fleetsync.state(lone).no_remote is True
    inner = lone / "fleet"
    inner.mkdir()
    assert fleetsync.where(inner) == fleetsync.INSIDE
    assert fleetsync.can_push(inner) is False
    assert fleetsync.where(tmp_path / "nothing") == fleetsync.NOT_GIT


# ── 10. a refused claim on a fleet that was already ahead ───────────────────


def test_refused_claim_on_a_fleet_already_ahead_says_fleet_push(repos: dict[str, Path]) -> None:
    a = repos["a"]
    _commit(a, "earlier", "1\n", "an earlier local commit")
    _other_machine_moves(repos)
    before = g(a, "rev-parse", "HEAD")
    commit = _commit(a, "lock", "2\n", "claim")
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.claim(
            a, commit, fleetsync.Undo(before=before, restore=["lock"]), again="bay up x", say=print
        )
    assert err.value.reason == "claim_refused"
    assert err.value.next_cmd == "bay fleet push"
    assert "Run `bay fleet push` (it rebases them), then `bay up x`." in str(err.value)
    assert "it pulls first" not in str(err.value)


def test_refused_claim_on_a_fleet_only_behind_says_run_again(repos: dict[str, Path]) -> None:
    a = repos["a"]
    _other_machine_moves(repos)
    before = g(a, "rev-parse", "HEAD")
    commit = _commit(a, "lock", "2\n", "claim")
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.claim(
            a, commit, fleetsync.Undo(before=before, restore=["lock"]), again="bay up x", say=print
        )
    assert err.value.next_cmd == "bay up x"
    assert "Run `bay up x` again: it pulls first." in str(err.value)


# ── 11. the run lock, the abort, the fast-forward timeout ───────────────────


def test_fleets_inside_one_repo_have_their_own_run_lock(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    repo.mkdir()
    g(repo, "init", "-q")
    for name in ("one", "two"):
        _commit(repo, f"{name}/bay.fleet.toml", f'name = "{name}"\n', name)
    with fleetsync.run_lock(repo / "one", "up", "production"):
        # The other fleet of the same repo is not busy.
        assert fleetsync.lock_holder(repo / "two") is None
        with fleetsync.run_lock(repo / "two", "up", "production") as info:
            assert info is not None
    assert fleetsync.lock_key(repo / "one") != fleetsync.lock_key(repo / "two")


_HOLD = """
import sys
from pathlib import Path
from bay_cli import fleetsync
with fleetsync.run_lock(Path(sys.argv[1]), "up", "production"):
    print("ready", flush=True)
    sys.stdin.read()
"""


def test_worktrees_and_symlinks_of_one_fleet_share_the_lock(repos: dict[str, Path]) -> None:
    a = repos["a"]
    wt = repos["tmp"] / "wt"
    g(a, "worktree", "add", "-q", "--detach", str(wt))
    link = repos["tmp"] / "link"
    link.symlink_to(a)
    assert fleetsync.lock_key(a) == fleetsync.lock_key(wt) == fleetsync.lock_key(link)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(a)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        for other in (wt, link):
            with pytest.raises(fleetsync.SyncError) as err:
                with fleetsync.run_lock(other, "up", "production"):
                    pass
            assert err.value.reason == "fleet_busy"
            assert (fleetsync.lock_holder(other) or {}).get("pid") == holder.pid
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)


def test_lock_holder_looks_with_a_shared_probe(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = repos["a"]
    with fleetsync.run_lock(a, "up", "production"):
        pass  # the lock file exists now
    seen: list[int] = []
    real = fcntl.flock

    def flock(fd: int, op: int) -> None:
        seen.append(op)
        real(fd, op)

    monkeypatch.setattr(fcntl, "flock", flock)
    assert fleetsync.lock_holder(a) is None
    assert seen and all(not op & fcntl.LOCK_EX for op in seen)
    assert seen[0] & fcntl.LOCK_SH and seen[0] & fcntl.LOCK_NB


def test_a_failed_rebase_abort_is_reported_with_the_fix(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = repos["a"]
    b = repos["tmp"] / "b"
    _clone(repos["origin"], b)
    _commit(b, "lock", "b\n", "B lock")
    g(b, "push", "-q")
    _commit(a, "lock", "a\n", "A lock")
    real = gitrepo._run

    def run(repo: Path, *args: str, **kw: Any) -> subprocess.CompletedProcess[str] | None:
        if args[:2] == ("rebase", "--abort"):
            return subprocess.CompletedProcess(args, 1, "", "error: cannot abort")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitrepo, "_run", run)
    ok, problem, conflicts = fleetsync.pull_rebase(a)
    assert not ok and conflicts == ["lock"]
    assert "git rebase --abort error: cannot abort" in str(problem)
    assert f"Finish it by hand: git -C {a} rebase --abort" in str(problem)
    monkeypatch.setattr(gitrepo, "_run", real)
    g(a, "rebase", "--abort")


def test_a_fast_forward_that_times_out_says_so(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = repos["a"]
    _other_machine_moves(repos)
    g(a, "fetch", "-q")
    _fail_git(monkeypatch, "merge", rc=None)
    with pytest.raises(fleetsync.SyncError) as err:
        fleetsync.fast_forward(a)
    assert "timed out" in str(err.value)
    assert "Local edits are in the way" not in (err.value.hint or "")
    assert err.value.reason == "git_failed"
