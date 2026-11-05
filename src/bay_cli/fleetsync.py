"""One fleet, several machines: keep the fleet checkout in step with its remote.

The fleet repo's remote is the only channel between machines. This module
reads the sync state after one ``git fetch`` (:func:`state`), fast-forwards a
checkout that is only behind (:func:`fast_forward`), takes the local run lock
that keeps two sessions on one checkout apart (:func:`run_lock`), and pushes
the fleet commit of ``bay up`` before the deploy (:func:`claim`). A refused
claim undoes the run's commit (:func:`undo`), so the checkout is as it was.

Nothing here merges or rebases, except :func:`push_with_rebase`: it runs
``git pull --rebase`` after a refused push of commits that were made after a
deploy (the receipt), and aborts the rebase when git refuses or conflicts.

A fleet with no remote, and a fleet that is a folder of a bigger repo, is
never fast-forwarded, claimed or pushed here.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bay_cli import gitrepo
from bay_cli.errors import BayError, ErrorCode

#: Exit codes. 40 is the first image of ``bay up`` (:data:`bay_cli.apply.FIRST_IMAGE_EXIT`).
EXIT_NOT_IN_SYNC = 41
EXIT_CLAIM_REFUSED = 42
EXIT_BUSY = 43
EXIT_RECEIPT_UNPUSHED = 44

REASON_DIVERGED = "diverged"
REASON_UNREACHABLE = "remote_unreachable"
REASON_NO_UPSTREAM = "no_upstream"
REASON_DIRTY = "dirty"
REASON_CLAIM_REFUSED = "claim_refused"
REASON_BUSY = "fleet_busy"
REASON_RECEIPT_UNPUSHED = "receipt_unpushed"
REASON_DEPLOY_FAILED = "deploy_failed"

#: Not counted as local edits: plan records (``bay plan`` writes them, untracked)
#: and the marker ``bay deploy`` writes after the first deploy.
DIRTY_EXCLUDE = ("plans", ".first_deploy_done")

LOCK_NAME = "bay-run.lock"
HOLDER_NAME = "bay-run.json"

#: A pending env record younger than this shows as ``deploying`` in ``bay show``.
CLAIM_FRESH_SECONDS = 30 * 60

NEVER_MERGE = (
    "never merge services.yml, a bay.lock or a vault file by hand"
)

Echo = Callable[[str], None]


class SyncError(BayError):
    """A fleet sync refusal. ``reason`` and ``exit_code`` are machine-readable."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        exit_code: int = EXIT_NOT_IN_SYNC,
        box_touched: bool = False,
        next_cmd: str | None = None,
        hint: str | None = None,
        paths: Sequence[str] = (),
        undo_failed: Sequence[str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.CONFLICT, hint=hint)
        self.reason = reason
        self._exit_code = exit_code
        self.box_touched = box_touched
        self.next_cmd = next_cmd
        self.paths = list(paths)
        #: A refused claim: the paths its undo could not put back (None: no undo ran).
        self.undo_failed = list(undo_failed) if undo_failed is not None else None

    @property
    def exit_code(self) -> int:
        return self._exit_code


def notice(line: str) -> None:
    """One line on stderr (with ``--log``, stderr goes to the log). Never on stdout."""
    print(line, file=sys.stderr, flush=True)


def hostname() -> str:
    return socket.gethostname().split(".")[0] or "unknown"


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── state ───────────────────────────────────────────────────────────────────


@dataclass
class State:
    """The fleet checkout against its remote, after one fetch."""

    is_git: bool = True
    no_remote: bool = False
    not_toplevel: bool = False
    #: The fetch failed: git's last line.
    unreachable: str | None = None
    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    dirty_paths: list[str] = field(default_factory=list)
    fetched: bool = False

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0

    @property
    def no_upstream(self) -> bool:
        return (
            self.is_git and not self.no_remote and not self.unreachable and self.upstream is None
        )

    @property
    def in_sync(self) -> bool:
        if not self.is_git or self.unreachable or self.no_upstream:
            return False
        return self.ahead == 0 and self.behind == 0

    def to_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["diverged"] = self.diverged
        doc["in_sync"] = self.in_sync
        return doc


def _out(repo: Path, *args: str, timeout: int = 30) -> str | None:
    return gitrepo._out(repo, *args, timeout=timeout)


def _fetch(repo: Path) -> str | None:
    """``git fetch`` (no prompt, 60 s). A problem line, or None when it worked."""
    proc = gitrepo._run(repo, "fetch", "--quiet", "--no-tags", timeout=60)
    if proc is None:
        return "git fetch timed out"
    if proc.returncode != 0:
        return gitrepo._last_line(str(proc.stderr)) or "git fetch failed"
    return None


def dirty_paths(repo: Path) -> list[str]:
    """Tracked-modified and untracked paths of the fleet, relative to it (plans/ left out)."""
    spec = [".", *(f":(exclude){e}" for e in DIRTY_EXCLUDE)]
    proc = gitrepo._run(
        repo,
        "-c",
        "core.quotepath=off",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--",
        *spec,
    )
    if proc is None or proc.returncode != 0:
        return []
    out: list[str] = []
    parts = proc.stdout.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the source path of a rename follows
        out.append(path)
    top = gitrepo.toplevel(repo)
    if top is not None and top.resolve() != repo.resolve():
        prefix = repo.resolve().relative_to(top.resolve()).as_posix() + "/"
        out = [p[len(prefix) :] if p.startswith(prefix) else p for p in out]
    return sorted(set(out))


def state(repo: Path, *, fetch: bool = True) -> State:
    """The sync state of the fleet at ``repo``. ``fetch=False`` reads the last fetch."""
    if not gitrepo.is_repo(repo) or gitrepo.head(repo) is None:
        return State(is_git=False)
    st = State(not_toplevel=not gitrepo.is_toplevel(repo))
    st.dirty_paths = dirty_paths(repo)
    st.branch = _out(repo, "symbolic-ref", "--short", "-q", "HEAD") or None
    remotes = _out(repo, "remote")
    if not remotes:
        st.no_remote = True
        return st
    if fetch:
        problem = _fetch(repo)
        if problem:
            st.unreachable = problem
            return st
        st.fetched = True
    st.upstream = (
        _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or None
    )
    if st.upstream is None:
        return st
    counts = _out(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts:
        ahead, behind = counts.split()
        st.ahead, st.behind = int(ahead), int(behind)
    return st


def diverged_message(st: State) -> str:
    return (
        f"the fleet and {st.upstream or 'its remote'} have diverged: {st.ahead} local "
        f"commit(s) and {st.behind} remote commit(s). Run `bay fleet push` (it rebases your "
        f"commits), {NEVER_MERGE}"
    )


# ── fast-forward ────────────────────────────────────────────────────────────


@dataclass
class Pulled:
    count: int
    old: str
    new: str
    upstream: str

    def to_dict(self) -> dict[str, Any]:
        return {"pulled": self.count, "from": self.old, "to": self.new}

    def line(self) -> str:
        return (
            f"fleet: pulled {self.count} commit{'s' if self.count != 1 else ''} from "
            f"{self.upstream} ({self.old[:7]}..{self.new[:7]})"
        )


def _git_refusal(stderr: str) -> str:
    """git's refusal, short: the ``error:``/``fatal:`` line and the paths it names."""
    lines = [ln.rstrip() for ln in stderr.splitlines() if ln.strip()]
    keep: list[str] = []
    for ln in lines:
        if ln.startswith(("error:", "fatal:")) or ln.startswith(("\t", "  ")):
            keep.append(ln.strip())
    return "; ".join(keep[:12]) or (lines[-1] if lines else "git refused")


#: The folder of plan records: the only place a fast-forward ever deletes a file.
PLANS = "plans"


def _plan_collisions(repo: Path) -> list[str]:
    """Untracked files under ``plans/`` that the upstream commit tracks.

    Plan ids are content hashes, so two machines that plan the same thing
    write the same ``plans/<id>.json``. The merge would refuse to overwrite
    the local untracked copy; the remote one records the same plan.
    """
    untracked = _out(repo, "ls-files", "--others", "--", f"{PLANS}/")
    upstream = _out(repo, "ls-tree", "-r", "--name-only", "@{u}", "--", f"{PLANS}/")
    if not untracked or not upstream:
        return []
    held = set(upstream.splitlines())
    return sorted(
        rel
        for rel in untracked.splitlines()
        if rel in held and rel.startswith(f"{PLANS}/") and ".." not in Path(rel).parts
    )


def fast_forward(repo: Path) -> Pulled:
    """``git merge --ff-only @{u}``. Raises :class:`SyncError` when git refuses.

    First it removes the local untracked plan records that the upstream
    commit holds (:func:`_plan_collisions`), and prints how many. Nothing
    else is ever deleted. When the merge then fails, they are put back.
    """
    old = gitrepo.head(repo) or ""
    upstream = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "@{u}"
    count = int(_out(repo, "rev-list", "--count", "HEAD..@{u}") or 0)
    removed: dict[str, bytes] = {}
    for rel in _plan_collisions(repo):
        path = repo / rel
        try:
            removed[rel] = path.read_bytes()
            path.unlink()
        except OSError:
            removed.pop(rel, None)
    proc = gitrepo._run(repo, "merge", "--ff-only", "--quiet", "--no-stat", "@{u}", timeout=120)
    if (proc is None or proc.returncode != 0) and removed:
        for rel, data in removed.items():
            with contextlib.suppress(OSError):
                if not (repo / rel).exists():
                    (repo / rel).write_bytes(data)
    elif removed:
        notice(f"fleet: removed {len(removed)} local plan record(s) that the remote now holds")
    if proc is None or proc.returncode != 0:
        detail = _git_refusal(str(proc.stderr) if proc is not None else "git merge timed out")
        raise SyncError(
            f"cannot fast-forward the fleet to {upstream}: {detail}",
            reason=REASON_DIRTY,
            hint="Local edits are in the way. Commit them (and run `bay fleet push`), "
            "or move them away, then run the command again.",
        )
    return Pulled(count, old, gitrepo.head(repo) or "", upstream)


def pull_if_behind(repo: Path, say: Echo = notice) -> Pulled | None:
    """Fast-forward a fleet that is only behind its remote. None when there was nothing to do.

    A fleet with no remote or inside a bigger repo is left alone, and so is
    one that is unreachable, ahead or diverged: the plan reports those.
    """
    if not gitrepo.is_repo(repo) or gitrepo.head(repo) is None:
        return None
    if not gitrepo.is_toplevel(repo):
        return None
    st = state(repo)
    if st.no_remote or st.unreachable or st.upstream is None:
        return None
    if st.behind and not st.ahead:
        pulled = fast_forward(repo)
        say(pulled.line())
        return pulled
    return None


# ── run lock ────────────────────────────────────────────────────────────────

#: Locks this process holds: a nested take (bay rollback runs bay up) is a no-op.
_held: dict[Path, dict[str, Any]] = {}


def _lock_dir(repo: Path) -> Path | None:
    if not gitrepo.is_repo(repo):
        return None
    common = _out(repo, "rev-parse", "--git-common-dir")
    if not common:
        return None
    path = Path(common)
    return path if path.is_absolute() else (repo / path).resolve()


def _since(started_at: str) -> str:
    try:
        when = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return "unknown time"
    return when.astimezone().strftime("%H:%M")


def busy_message(holder: dict[str, Any] | None) -> str:
    if not holder:
        return "the fleet is busy: another bay run holds the lock"
    what = " ".join(str(p) for p in ("bay", holder.get("verb"), holder.get("env")) if p)
    return (
        f"the fleet is busy: {what} (pid {holder.get('pid')} on {holder.get('host')}, "
        f"since {_since(str(holder.get('started_at')))})"
    )


def _read_holder(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def run_lock(repo: Path, verb: str, env: str | None = None) -> Iterator[dict[str, Any] | None]:
    """Hold the fleet's run lock for one verb. Raises :class:`SyncError` (``fleet_busy``).

    A non-blocking ``flock`` on ``<git common dir>/bay-run.lock``; the holder
    is in ``bay-run.json`` beside it. The kernel drops the flock when the
    holder dies, so a stale holder file never blocks. A fleet that is not a
    git repo takes no lock (yields None).
    """
    lock_dir = _lock_dir(repo)
    if lock_dir is None:
        yield None
        return
    lock_path = lock_dir / LOCK_NAME
    if lock_path in _held:
        yield _held[lock_path]
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        holder = _read_holder(lock_dir / HOLDER_NAME)
        raise SyncError(
            busy_message(holder),
            reason=REASON_BUSY,
            exit_code=EXIT_BUSY,
            hint="Wait for that run to finish, then run the command again.",
        ) from None
    info = {
        "pid": os.getpid(),
        "verb": verb,
        "env": env,
        "host": hostname(),
        "started_at": now_iso(),
        "cwd": str(Path.cwd()),
    }
    holder_path = lock_dir / HOLDER_NAME
    try:
        tmp = holder_path.with_name(holder_path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(info) + "\n")
        tmp.replace(holder_path)
    except OSError:
        pass
    _held[lock_path] = info
    try:
        yield info
    finally:
        _held.pop(lock_path, None)
        current = _read_holder(holder_path)
        if current is not None and current.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                holder_path.unlink()
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def lock_holder(repo: Path) -> dict[str, Any] | None:
    """The holder of the run lock, or None when no run holds it."""
    lock_dir = _lock_dir(repo)
    if lock_dir is None:
        return None
    lock_path = lock_dir / LOCK_NAME
    if lock_path in _held:
        return _held[lock_path]
    if not lock_path.exists():
        return None
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return _read_holder(lock_dir / HOLDER_NAME) or {}
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


#: The fast-forward of the outer :func:`write_run`, per lock this process holds.
_pulled: dict[Path, Pulled | None] = {}


@contextlib.contextmanager
def write_run(repo: Path, verb: str, env: str | None) -> Iterator[Pulled | None]:
    """The run lock plus the fast-forward of ``bay up`` and ``bay rollback``.

    Nested in the same process (``bay rollback`` runs ``bay up``), it takes
    nothing and fetches nothing: it yields what the outer run pulled.
    """
    lock_dir = _lock_dir(repo)
    key = lock_dir / LOCK_NAME if lock_dir is not None else None
    if key is not None and key in _held:
        yield _pulled.get(key)
        return
    with run_lock(repo, verb, env):
        pulled = pull_if_behind(repo)
        if key is not None:
            _pulled[key] = pulled
        try:
            yield pulled
        finally:
            if key is not None:
                _pulled.pop(key, None)


# ── claim and undo ──────────────────────────────────────────────────────────


def can_push(repo: Path) -> bool:
    """True when ``bay up`` pushes this fleet: a top-level repo with a remote."""
    return (
        gitrepo.is_repo(repo)
        and gitrepo.is_toplevel(repo)
        and bool((_out(repo, "remote") or "").split())
    )


@dataclass
class Undo:
    """What a refused claim puts back. Paths are relative to the fleet."""

    before: str
    #: Restored from ``before`` in the index and the work tree (gone when absent there).
    restore: list[str] = field(default_factory=list)
    #: Only unstaged (restored in the index when ``before`` has them).
    unstage: list[str] = field(default_factory=list)
    #: Work tree bytes to put back after the unstage (None: leave the file).
    contents: dict[str, bytes | None] = field(default_factory=dict)


def _rel(repo: Path, path: Path | str) -> str:
    p = Path(path)
    return p.relative_to(repo).as_posix() if p.is_absolute() else p.as_posix()


@dataclass
class UndoResult:
    """What :func:`undo` did. ``state``: ``done``, ``partial``, ``moved`` or ``reset_failed``."""

    state: str
    #: Paths that could not be put back (``partial``).
    failed: list[str] = field(default_factory=list)
    detail: str | None = None


def _ok(proc: subprocess.CompletedProcess[str] | None) -> bool:
    return proc is not None and proc.returncode == 0


def undo(repo: Path, commit: str, plan: Undo) -> UndoResult:
    """Undo this run's fleet commit, path by path.

    ``git reset --soft`` to the commit before the run, then each path of the
    run alone: no ``reset --hard``, no other path, so the uncommitted work of
    other sessions in this checkout stays. Every git call is checked. A path
    that cannot be put back is listed in ``failed``, and the other paths are
    still done. Nothing is done when HEAD moved past ``commit`` (``moved``) or
    the reset fails (``reset_failed``).
    """
    if gitrepo.head(repo) != commit:
        return UndoResult("moved")
    if commit != plan.before:
        proc = gitrepo._run(repo, "reset", "--soft", "--quiet", plan.before)
        if not _ok(proc):
            detail = gitrepo._last_line(str(proc.stderr)) if proc is not None else "timed out"
            return UndoResult("reset_failed", detail=detail or "git reset failed")
    failed: list[str] = []
    for rel in plan.restore:
        if gitrepo.has_path(repo, plan.before, rel):
            done = _ok(gitrepo._run(
                repo, "restore", f"--source={plan.before}", "--staged", "--worktree", "--", rel
            ))
        else:
            done = _ok(
                gitrepo._run(repo, "rm", "--cached", "--quiet", "--ignore-unmatch", "--", rel)
            )
            try:
                (repo / rel).unlink(missing_ok=True)
            except OSError:
                done = False
        if not done:
            failed.append(rel)
    for rel in plan.unstage:
        if gitrepo.has_path(repo, plan.before, rel):
            done = _ok(
                gitrepo._run(repo, "restore", f"--source={plan.before}", "--staged", "--", rel)
            )
        else:
            done = _ok(
                gitrepo._run(repo, "rm", "--cached", "--quiet", "--ignore-unmatch", "--", rel)
            )
        data = plan.contents.get(rel)
        if data is not None:
            try:
                (repo / rel).write_bytes(data)
            except OSError:
                done = False
        if not done:
            failed.append(rel)
    return UndoResult("partial" if failed else "done", failed)


def _undo_line(result: UndoResult) -> str:
    if result.state == "done":
        return "The fleet commit of this run is undone."
    if result.state == "moved":
        return "HEAD moved after the fleet commit of this run, so it was not undone."
    if result.state == "reset_failed":
        return (
            f"The fleet commit of this run was NOT undone: git reset failed ({result.detail}). "
            "It is still the local HEAD; run `git reset --soft HEAD~1` in the fleet."
        )
    return (
        "The undo was PARTIAL: the fleet commit is undone, but these paths could not be put "
        f"back: {', '.join(result.failed)}. Check them with git status in the fleet."
    )


def _newest_remote(repo: Path) -> tuple[int, str]:
    """``(count, '<short sha> "<subject>"')`` of the remote commits HEAD lacks."""
    count = int(_out(repo, "rev-list", "--count", "HEAD..@{u}") or 0)
    top = _out(repo, "log", "-1", "--format=%h%x00%s", "@{u}") or ""
    sha, _, subject = top.partition("\0")
    return count, f'{sha} "{subject}"' if sha else "unknown"


def claim(
    repo: Path,
    commit: str,
    plan: Undo,
    *,
    again: str,
    say: Echo,
) -> None:
    """Push the fleet commit before the deploy. Raises :class:`SyncError` after an undo.

    A refused push is checked with a fetch: when the remote branch already
    holds ``commit`` the push landed (a timeout hid it) and the run goes on.
    """
    pushed, problem = gitrepo.push(repo)
    if pushed:
        say("fleet: claimed (pushed the fleet commit before the deploy)")
        return
    fetch_problem = _fetch(repo)
    if fetch_problem is None and gitrepo.is_ancestor(repo, commit, "@{u}") is True:
        say(f"fleet: the push said `{problem}`, but the remote has the commit; going on")
        return
    count = 0
    if fetch_problem is not None:
        reason, code = REASON_UNREACHABLE, EXIT_NOT_IN_SYNC
        why = f"The fleet remote cannot be reached: {fetch_problem}."
    else:
        reason, code = REASON_CLAIM_REFUSED, EXIT_CLAIM_REFUSED
        count, newest = _newest_remote(repo)
        why = (
            f"The fleet remote moved: {count} new commit(s) ({newest})."
            if count
            else f"The fleet remote refused the push: {problem}."
        )
    undone = undo(repo, commit, plan)
    tail = _undo_line(undone)
    again_line = (
        "Run bay up again: it pulls first." if reason == REASON_CLAIM_REFUSED and count else ""
    )
    message = f"Stopped before the box was touched. {why} Nothing was deployed. {again_line} {tail}"
    message = " ".join(message.split())
    raise SyncError(
        message, reason=reason, exit_code=code, next_cmd=again, undo_failed=undone.failed
    )


# ── the push after the deploy, and bay fleet push ───────────────────────────


@dataclass
class PushResult:
    pushed: bool
    problem: str | None = None
    rebased: int = 0
    conflicts: list[str] = field(default_factory=list)


def _conflicts(repo: Path) -> list[str]:
    out = _out(repo, "-c", "core.quotepath=off", "diff", "--name-only", "--diff-filter=U")
    return [line for line in (out or "").splitlines() if line]


def _rebase_in_progress(repo: Path) -> bool:
    git_dir = _out(repo, "rev-parse", "--git-dir")
    if not git_dir:
        return False
    path = Path(git_dir) if Path(git_dir).is_absolute() else repo / git_dir
    return (path / "rebase-merge").exists() or (path / "rebase-apply").exists()


def pull_rebase(repo: Path) -> tuple[bool, str | None, list[str]]:
    """``git pull --rebase`` once. ``(ok, problem, conflicting files)``; aborts on refusal."""
    proc = gitrepo._run(
        repo, "pull", "--rebase", "--no-autostash", "--quiet", "--no-tags", timeout=180
    )
    if proc is not None and proc.returncode == 0:
        return True, None, []
    detail = _git_refusal(
        (str(proc.stderr) + "\n" + str(proc.stdout)) if proc is not None else "git pull timed out"
    )
    conflicts = _conflicts(repo)
    if _rebase_in_progress(repo):
        gitrepo._run(repo, "rebase", "--abort")
    return False, detail, conflicts


def push_with_rebase(repo: Path, *, attempts: int = 2) -> PushResult:
    """Push; on a refusal, ``git pull --rebase`` and push again, at most ``attempts`` times."""
    pushed, problem = gitrepo.push(repo)
    if pushed:
        return PushResult(True)
    result = PushResult(False, problem)
    for _ in range(attempts):
        ok, rebase_problem, conflicts = pull_rebase(repo)
        if not ok:
            result.problem = rebase_problem
            result.conflicts = conflicts
            return result
        result.rebased += 1
        pushed, problem = gitrepo.push(repo)
        if pushed:
            result.pushed, result.problem = True, None
            return result
        result.problem = problem
    return result


# ── bay deploy and bay provision ────────────────────────────────────────────

#: At most this many dirty paths are named in a refusal.
DIRTY_LIST_MAX = 20


def _path_list(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:DIRTY_LIST_MAX])
    more = len(paths) - DIRTY_LIST_MAX
    return shown + (f" and {more} more" if more > 0 else "")


def dirty_refusal(paths: Sequence[str], flag: str = "--allow-dirty") -> SyncError:
    return SyncError(
        f"the fleet has {len(paths)} uncommitted path(s): {_path_list(paths)}. They would "
        "deploy, and no other machine can see them",
        reason=REASON_DIRTY,
        hint=f"Commit them and run `bay fleet push`, or pass {flag} to deploy them anyway.",
        paths=paths,
    )


def prepare_playbook(
    repo: Path,
    *,
    verb: str,
    offline: bool = False,
    allow_dirty: bool = False,
    say: Echo = notice,
) -> Pulled | None:
    """The fleet checks of ``bay deploy`` and ``bay provision``, before the playbook.

    Ansible reads the work tree, so the box gets what is on disk. In order:
    an unreachable remote refuses (unless ``offline``), a diverged fleet
    refuses, uncommitted paths refuse (unless ``allow_dirty``), a fleet that
    is only behind is fast-forwarded, and a fleet that is ahead is pushed
    first (the claim; a refused push stops the run). A fleet with no remote
    or inside a bigger repo gets the dirty check only. Raises
    :class:`SyncError`; returns the fast-forward, if one ran.
    """
    if not gitrepo.is_repo(repo) or gitrepo.head(repo) is None:
        return None
    remote = not offline and gitrepo.is_toplevel(repo)
    st = state(repo, fetch=remote)
    remote = remote and not st.no_remote
    if remote:
        if st.unreachable:
            raise SyncError(
                f"cannot fetch the fleet remote: {st.unreachable}",
                reason=REASON_UNREACHABLE,
                hint=f"Fix the remote, or pass --offline to {verb} what is on this machine.",
            )
        if st.upstream is None:
            raise SyncError(
                "the fleet branch tracks no remote branch",
                reason=REASON_NO_UPSTREAM,
                hint="Set one with git branch -u origin/<branch>, or pass --offline.",
            )
        if st.diverged:
            raise SyncError(diverged_message(st), reason=REASON_DIVERGED, next_cmd="bay fleet push")
    if st.dirty_paths and not allow_dirty:
        raise dirty_refusal(st.dirty_paths)
    pulled: Pulled | None = None
    if remote and st.behind:
        pulled = fast_forward(repo)
        say(pulled.line())
    elif remote and st.ahead:
        _push_ahead(repo, st, verb, say)
    if offline:
        say(
            f"WARNING: --offline: this {verb} runs what is on this machine. The result is not "
            "on the fleet remote. Run `bay fleet push` after it."
        )
    return pulled


def _push_ahead(repo: Path, st: State, verb: str, say: Echo) -> None:
    """Push the local commits before the playbook. Raises :class:`SyncError` when refused."""
    pushed, problem = gitrepo.push(repo)
    if pushed:
        say(f"fleet: pushed {st.ahead} local commit(s) to {st.upstream} before the {verb}")
        return
    head = gitrepo.head(repo) or "HEAD"
    fetch_problem = _fetch(repo)
    if fetch_problem is None and gitrepo.is_ancestor(repo, head, "@{u}") is True:
        say(f"fleet: the push said `{problem}`, but the remote has the commits; going on")
        return
    if fetch_problem is not None:
        raise SyncError(
            f"Stopped before the box was touched. The fleet remote cannot be reached: "
            f"{fetch_problem}.",
            reason=REASON_UNREACHABLE,
            hint=f"Fix the remote, or pass --offline to {verb} what is on this machine.",
        )
    count, newest = _newest_remote(repo)
    why = (
        f"The fleet remote moved: {count} new commit(s) ({newest})."
        if count
        else f"The fleet remote refused the push: {problem}."
    )
    raise SyncError(
        f"Stopped before the box was touched. {why} Nothing was deployed.",
        reason=REASON_CLAIM_REFUSED,
        exit_code=EXIT_CLAIM_REFUSED,
        next_cmd="bay fleet push",
        hint=f"Run `bay fleet push` (it rebases your commits), then the {verb} again.",
    )
