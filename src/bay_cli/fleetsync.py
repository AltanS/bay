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
import time
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
#: A git call on the fleet failed or timed out: Bay cannot tell the state, so it stops.
REASON_GIT_FAILED = "git_failed"

#: Not counted as local edits: plan records (``bay plan`` writes them, untracked)
#: and the marker ``bay deploy`` writes after the first deploy.
DIRTY_EXCLUDE = ("plans", ".first_deploy_done")

#: The run lock and its holder file, per fleet: ``bay-run-<key>.lock`` and
#: ``bay-run-<key>.json`` in the git common dir (:func:`lock_key`).
LOCK_PREFIX = "bay-run"

#: A pending env record younger than this shows as ``deploying`` in ``bay show``.
CLAIM_FRESH_SECONDS = 30 * 60

#: Plan and up reasons for the two run blockers (:func:`bay_cli.plan.add_run_blockers`).
REASON_ENV_BUSY = "env_busy"


def claim_time(record: Any) -> datetime | None:
    """``claimed_at`` of a pending env record as a time, or None."""
    if not isinstance(record, dict) or record.get("result") != "pending":
        return None
    try:
        return datetime.strptime(str(record.get("claimed_at")), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def claim_fields() -> dict[str, Any]:
    """The claim keys of a pending env record: this machine, now, this run.

    ``claimed_by`` is the host name, for people only: Bay never compares it.
    ``claimed_run`` names this process so that only this machine can judge it
    (:func:`run_id`, :func:`claim_state`).
    """
    return {"claimed_by": hostname(), "claimed_at": now_iso(), "claimed_run": run_id()}


#: Mixed into the machine id before the hash, so the record never holds the raw id.
_MACHINE_SALT = b"bay-claim"


def machine_tag() -> str | None:
    """The first 12 hex of sha256(``/etc/machine-id`` bytes + ``bay-claim``), or None."""
    import hashlib

    try:
        raw = Path("/etc/machine-id").read_bytes().strip()
    except OSError:
        return None
    if not raw:
        return None
    return hashlib.sha256(raw + _MACHINE_SALT).hexdigest()[:12]


def pid_namespace() -> str | None:
    """The inode of this process's pid namespace (``readlink /proc/self/ns/pid``), or None."""
    try:
        link = os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
    inode = link.partition("[")[2].rstrip("]")
    return inode if inode.isdigit() else None


#: ``proc_start`` result for a pid that does not exist.
GONE = "gone"


def proc_start(pid: int) -> str | None:
    """Field 22 of ``/proc/<pid>/stat`` (the start time), :data:`GONE`, or None (cannot tell)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return GONE
    except OSError:
        return None
    # The command name (field 2) may hold spaces and parentheses: split after the last ")".
    rest = text.rpartition(")")[2].split()
    # rest[0] is field 3, so field 22 is rest[19].
    if len(rest) < 20 or not rest[19].isdigit():
        return None
    return rest[19]


def run_id(pid: int | None = None) -> str:
    """``<machine>:<pidns>:<pid>:<start>`` for ``pid`` (default: this process), or ``""``.

    Empty when any part cannot be read (no ``/etc/machine-id``, no ``/proc``):
    such a claim is never judged, it blocks until it expires.
    """
    pid = os.getpid() if pid is None else pid
    machine, ns, start = machine_tag(), pid_namespace(), proc_start(pid)
    if machine is None or ns is None or start in (None, GONE):
        return ""
    return f"{machine}:{ns}:{pid}:{start}"


#: :func:`claim_state` results.
CLAIM_OWN, CLAIM_DEAD, CLAIM_LIVE = "own", "dead", "live"


def claim_state(record: Any) -> str:
    """``own`` (this process's claim), ``dead`` (provably gone) or ``live`` (blocks).

    Dead only when all hold: the ``claimed_run`` machine and pid namespace are
    this process's, and that pid is gone or its start time differs (a reused
    pid). Anything Bay cannot judge is live: another machine or namespace, no
    ``claimed_run``, an empty or unparsable one, or a ``/proc`` that cannot
    be read.
    """
    run = record.get("claimed_run") if isinstance(record, dict) else None
    if not isinstance(run, str) or not run:
        return CLAIM_LIVE
    mine = run_id()
    if not mine:
        return CLAIM_LIVE
    if run == mine:
        return CLAIM_OWN
    parts = run.split(":")
    if len(parts) != 4:
        return CLAIM_LIVE
    machine, ns, pid_text, start = parts
    my_machine, my_ns, _, _ = mine.split(":")
    if machine != my_machine or ns != my_ns or not pid_text.isdigit() or not start.isdigit():
        return CLAIM_LIVE
    pid = int(pid_text)
    if pid <= 0:
        return CLAIM_LIVE
    now_start = proc_start(pid)
    if now_start == GONE:
        return CLAIM_DEAD
    if now_start is None:
        return CLAIM_LIVE
    return CLAIM_DEAD if now_start != start else CLAIM_LIVE


def fresh_claim(record: Any, now: datetime | None = None) -> bool:
    """True for a pending record claimed less than :data:`CLAIM_FRESH_SECONDS` ago.

    A claim in the future (the other machine's clock is ahead) is fresh until
    ``claimed_at`` plus the limit. A claimer whose clock is far behind writes
    a ``claimed_at`` that looks old: that is not detected.
    """
    when = claim_time(record)
    if when is None:
        return False
    age = ((now or datetime.now(UTC)) - when).total_seconds()
    return age < CLAIM_FRESH_SECONDS


#: A ``claimed_at`` more than this far ahead of this machine's clock gets a note.
CLOCK_SKEW_NOTE_SECONDS = 120


def clock_ahead(record: Any, now: datetime | None = None) -> int | None:
    """Seconds the claim is ahead of this clock, when more than :data:`CLOCK_SKEW_NOTE_SECONDS`."""
    when = claim_time(record)
    if when is None:
        return None
    ahead = (when - (now or datetime.now(UTC))).total_seconds()
    return int(ahead) if ahead > CLOCK_SKEW_NOTE_SECONDS else None


def local_hhmm(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


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


def _cmd(args: Sequence[str]) -> str:
    """``git <subcommand>`` for a message (the ``-c key=value`` options left out)."""
    words: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "-c":
            skip = True
            continue
        words.append(arg)
        if not arg.startswith("-"):
            break
    return "git " + " ".join(words)


def git_failed(args: Sequence[str], proc: subprocess.CompletedProcess[str] | None) -> SyncError:
    """The refusal for a git call that failed or timed out. It names the git error."""
    if proc is None:
        why = "timed out"
    else:
        why = gitrepo._last_line(str(proc.stderr)) or f"exited with {proc.returncode}"
    return SyncError(
        f"cannot read the fleet repo: {_cmd(args)} {why}",
        reason=REASON_GIT_FAILED,
        hint="Fix the git problem in the fleet checkout, then run the command again.",
    )


def _must(
    repo: Path, *args: str, ok: Sequence[int] = (0,), timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Run git; raise :func:`git_failed` when it times out or exits with a code not in ``ok``."""
    proc = gitrepo._run(repo, *args, timeout=timeout)
    if proc is None or proc.returncode not in ok:
        raise git_failed(args, proc)
    return proc


#: :func:`where` values.
NOT_GIT, TOPLEVEL, INSIDE = "not_git", "toplevel", "inside"


def where(repo: Path) -> str:
    """Where the fleet sits: :data:`NOT_GIT`, :data:`TOPLEVEL` or :data:`INSIDE` a bigger repo.

    Raises :class:`SyncError` (``git_failed``) when git fails for another
    reason than "not a git repository": a failed git is never read as "no repo".
    """
    if not repo.is_dir():
        return NOT_GIT
    proc = gitrepo._run(repo, "rev-parse", "--show-toplevel")
    if proc is None:
        raise git_failed(("rev-parse", "--show-toplevel"), proc)
    if proc.returncode != 0:
        if "not a git repository" in str(proc.stderr):
            return NOT_GIT
        raise git_failed(("rev-parse", "--show-toplevel"), proc)
    top = Path(str(proc.stdout).strip())
    return TOPLEVEL if top.resolve() == repo.resolve() else INSIDE


def remotes(repo: Path) -> list[str]:
    """The remotes of the fleet repo. Raises :class:`SyncError` when git fails."""
    return str(_must(repo, "remote").stdout).split()


def has_head(repo: Path) -> bool:
    """True when the repo has a commit. Raises :class:`SyncError` when git fails."""
    proc = _must(repo, "rev-parse", "--verify", "--quiet", "HEAD", ok=(0, 1, 128))
    return proc.returncode == 0 and bool(str(proc.stdout).strip())


def upstream_target(repo: Path) -> tuple[str, str] | None:
    """``(remote, refs/heads/<branch>)`` that ``@{u}`` names, or None: no upstream.

    Read from git's own branch config, so a push goes there and nowhere else:
    ``push.default``, ``branch.<name>.pushRemote`` and ``remote.pushDefault``
    play no part. A branch that tracks a local branch has no remote: None.
    """
    branch = _must(repo, "symbolic-ref", "--quiet", "HEAD", ok=(0, 1))
    ref = str(branch.stdout).strip()
    if branch.returncode != 0 or not ref:
        return None
    proc = _must(
        repo,
        "for-each-ref",
        "--format=%(upstream:remotename)%00%(upstream:remoteref)",
        ref,
    )
    remote, _, target = str(proc.stdout).strip().partition("\0")
    if not remote or remote == "." or not target.startswith("refs/heads/"):
        return None
    return remote, target


def push_upstream(repo: Path) -> tuple[bool, str | None]:
    """``git push <remote> HEAD:<branch>`` to the branch ``@{u}`` names. ``(pushed, problem)``.

    The remote and the branch are named on the command line (:func:`upstream_target`).
    A ``remote.<name>.pushurl`` still applies: :func:`landed` checks the result.
    """
    target = upstream_target(repo)
    if target is None:
        return False, "the fleet branch tracks no remote branch"
    remote, ref = target
    proc = gitrepo._run(repo, "push", "--quiet", remote, f"HEAD:{ref}", timeout=120)
    if proc is None:
        return False, "git push timed out"
    if proc.returncode != 0:
        return False, gitrepo._last_line(str(proc.stderr)) or "git push failed"
    return True, None


def landed(repo: Path, commit: str, *, timeout: int = 60) -> tuple[bool, str | None]:
    """Fetch, then: does the tracked branch hold ``commit``? ``(held, fetch problem)``.

    A fetch problem (it failed, or took longer than ``timeout`` seconds) means
    Bay cannot tell; ``held`` is then False.
    """
    problem = _fetch(repo, timeout=timeout)
    if problem is not None:
        return False, problem
    return gitrepo.is_ancestor(repo, commit, "@{u}") is True, None


def _upstream_name(repo: Path) -> str:
    target = upstream_target(repo)
    return f"{target[0]}/{target[1].removeprefix('refs/heads/')}" if target else "@{u}"


def _fetch(repo: Path, *, timeout: int = 60) -> str | None:
    """``git fetch`` (no prompt, ``timeout`` s). A problem line, or None when it worked."""
    proc = gitrepo._run(repo, "fetch", "--quiet", "--no-tags", timeout=timeout)
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
        # Fail closed: an unreadable status is never "nothing uncommitted".
        raise git_failed(("status",), proc)
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
    top = Path(str(_must(repo, "rev-parse", "--show-toplevel").stdout).strip())
    if top.resolve() != repo.resolve():
        prefix = repo.resolve().relative_to(top.resolve()).as_posix() + "/"
        out = [p[len(prefix) :] if p.startswith(prefix) else p for p in out]
    return sorted(set(out))


def state(repo: Path, *, fetch: bool = True) -> State:
    """The sync state of the fleet at ``repo``. ``fetch=False`` reads the last fetch."""
    kind = where(repo)
    if kind == NOT_GIT or not has_head(repo):
        return State(is_git=False)
    st = State(not_toplevel=kind == INSIDE)
    st.dirty_paths = dirty_paths(repo)
    branch = _must(repo, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1))
    st.branch = str(branch.stdout).strip() or None
    if not remotes(repo):
        st.no_remote = True
        return st
    if fetch:
        problem = _fetch(repo)
        if problem:
            st.unreachable = problem
            return st
        st.fetched = True
    if upstream_target(repo) is None:
        return st
    st.upstream = _upstream_name(repo)
    counts = str(
        _must(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").stdout
    ).split()
    st.ahead, st.behind = int(counts[0]), int(counts[1])
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


def _set_aside_plans(repo: Path) -> dict[str, bytes]:
    """Remove the untracked plan records the upstream holds (:func:`_plan_collisions`).

    Returns their bytes, so :func:`_settle_plans` can put them back.
    """
    removed: dict[str, bytes] = {}
    for rel in _plan_collisions(repo):
        path = repo / rel
        try:
            removed[rel] = path.read_bytes()
            path.unlink()
        except OSError:
            removed.pop(rel, None)
    return removed


def _settle_plans(repo: Path, removed: dict[str, bytes], *, ok: bool) -> None:
    """After the merge or the rebase: put the records back when it failed, else say so."""
    if not removed:
        return
    if not ok:
        for rel, data in removed.items():
            with contextlib.suppress(OSError):
                if not (repo / rel).exists():
                    # git may have removed the emptied plans/ folder.
                    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                    (repo / rel).write_bytes(data)
        return
    notice(f"fleet: removed {len(removed)} local plan record(s) that the remote now holds")


def fast_forward(repo: Path) -> Pulled:
    """``git merge --ff-only @{u}``. Raises :class:`SyncError` when git refuses.

    First it removes the local untracked plan records that the upstream
    commit holds (:func:`_plan_collisions`), and prints how many. Nothing
    else is ever deleted. When the merge then fails, they are put back.
    """
    old = gitrepo.head(repo) or ""
    upstream = _out(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or "@{u}"
    count = int(_out(repo, "rev-list", "--count", "HEAD..@{u}") or 0)
    removed = _set_aside_plans(repo)
    proc = gitrepo._run(repo, "merge", "--ff-only", "--quiet", "--no-stat", "@{u}", timeout=120)
    _settle_plans(repo, removed, ok=proc is not None and proc.returncode == 0)
    if proc is None:
        raise SyncError(
            f"cannot fast-forward the fleet to {upstream}: git merge timed out after 120 s",
            reason=REASON_GIT_FAILED,
            hint="Check the fleet checkout (a slow disk, a git lock held by another "
            "process), then run the command again.",
        )
    if proc.returncode != 0:
        detail = _git_refusal(str(proc.stderr))
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
    if where(repo) != TOPLEVEL or not has_head(repo):
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


def lock_key(repo: Path) -> str:
    """A short, stable hash of the fleet's real path inside its repo.

    Two fleets that are folders of one bigger repo get two keys, so they never
    block each other. Every worktree of the repo, and every path that resolves
    to the same folder (a symlink), gets the same key, so they share one lock.
    """
    import hashlib

    top = Path(str(_must(repo, "rev-parse", "--show-toplevel").stdout).strip()).resolve()
    rel = repo.resolve().relative_to(top).as_posix()
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:12]


def _lock_paths(repo: Path) -> tuple[Path, Path] | None:
    """``(lock file, holder file)`` of the fleet at ``repo``, or None: not a git repo."""
    if where(repo) == NOT_GIT:
        return None
    common = Path(str(_must(repo, "rev-parse", "--git-common-dir").stdout).strip())
    base = common if common.is_absolute() else (repo / common).resolve()
    stem = f"{LOCK_PREFIX}-{lock_key(repo)}"
    return base / f"{stem}.lock", base / f"{stem}.json"


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


#: How long a run tries for the run lock before it says the fleet is busy. A
#: ``bay fleet status`` probe holds a shared lock for a moment; this wait keeps
#: it from making a run that starts at that moment busy.
LOCK_WAIT_SECONDS = 1.0


def _flock_within(fd: int, seconds: float) -> bool:
    """Take the exclusive flock on ``fd``, trying again for up to ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


@contextlib.contextmanager
def run_lock(repo: Path, verb: str, env: str | None = None) -> Iterator[dict[str, Any] | None]:
    """Hold the fleet's run lock for one verb. Raises :class:`SyncError` (``fleet_busy``).

    An exclusive ``flock`` on ``<git common dir>/bay-run-<key>.lock``, tried for
    up to :data:`LOCK_WAIT_SECONDS`
    (:func:`lock_key`: one lock per fleet folder); the holder is in the
    ``.json`` beside it. The kernel drops the flock when the holder dies, so a
    stale holder file never blocks. A fleet that is not a git repo takes no
    lock (yields None).
    """
    paths = _lock_paths(repo)
    if paths is None:
        yield None
        return
    lock_path, holder_path = paths
    if lock_path in _held:
        yield _held[lock_path]
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    if not _flock_within(fd, LOCK_WAIT_SECONDS):
        os.close(fd)
        holder = _read_holder(holder_path)
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
    """The holder of the run lock, or None when no run holds it.

    Looks with a shared, non-blocking probe: it never takes the lock a run
    would take, so a ``bay fleet status`` cannot make a ``bay up`` busy.
    """
    paths = _lock_paths(repo)
    if paths is None:
        return None
    lock_path, holder_path = paths
    if lock_path in _held:
        return _held[lock_path]
    if not lock_path.exists():
        return None
    fd = os.open(lock_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return _read_holder(holder_path) or {}
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
    paths = _lock_paths(repo)
    key = paths[0] if paths is not None else None
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
    """True when ``bay up`` pushes this fleet: a top-level repo with a remote.

    False for no repo, no remote, or a fleet inside a bigger repo. A git call
    that fails raises :class:`SyncError` (``git_failed``): it never skips the claim.
    """
    return where(repo) == TOPLEVEL and bool(remotes(repo))


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
    #: Removed from the work tree after the unstage (a file this run wrote first).
    drop: list[str] = field(default_factory=list)


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
    for rel in plan.drop:
        try:
            (repo / rel).unlink(missing_ok=True)
        except OSError:
            failed.append(rel)
    return UndoResult("partial" if failed else "done", failed)


def undo_line(result: UndoResult, *, committed: bool = True) -> str:
    """:func:`_undo_line`; ``committed=False`` when the run stopped before its commit."""
    if not committed and result.state == "done":
        return "The files this run wrote are back as they were."
    if not committed and result.state == "partial":
        return (
            "The undo was PARTIAL: these paths could not be put back: "
            f"{', '.join(result.failed)}. Check them with git status in the fleet."
        )
    return _undo_line(result)


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

    The push names the remote and the branch of ``@{u}`` (:func:`push_upstream`).
    After every push, also one that reports success, a fetch checks that the
    tracked branch holds ``commit`` (:func:`landed`). Only then is the claim
    made. A refused push whose commit the remote already holds (a timeout hid
    it) goes on too.
    """
    pushed, problem = push_upstream(repo)
    held, fetch_problem = landed(repo, commit)
    if held:
        if pushed:
            say("fleet: claimed (pushed the fleet commit before the deploy)")
        else:
            say(f"fleet: the push said `{problem}`, but the remote has the commit; going on")
        return
    count = 0
    next_cmd: str | None = again
    if fetch_problem is not None:
        reason, code = REASON_UNREACHABLE, EXIT_NOT_IN_SYNC
        why = f"The fleet remote cannot be reached: {fetch_problem}."
    else:
        reason, code = REASON_CLAIM_REFUSED, EXIT_CLAIM_REFUSED
        count, newest = _newest_remote(repo)
        if pushed:
            why = (
                f"The push reported success, but {_upstream_name(repo)} does not hold the "
                "fleet commit: the push did not reach the tracked branch. Check "
                "remote.<name>.pushurl in the fleet's git config."
            )
            next_cmd = None
        elif count:
            why = f"The fleet remote moved: {count} new commit(s) ({newest})."
        else:
            why = f"The fleet remote refused the push: {problem}."
            next_cmd = None
    undone = undo(repo, commit, plan)
    tail = _undo_line(undone)
    again_line = ""
    if reason == REASON_CLAIM_REFUSED and count and not pushed:
        if gitrepo.is_ancestor(repo, plan.before, "@{u}") is True:
            again_line = f"Run `{again}` again: it pulls first."
        else:
            # The fleet had local commits before this run: it is diverged now,
            # and the same command would be blocked. Push them first.
            again_line = (
                "The fleet also has local commits that the remote does not have. "
                f"Run `bay fleet push` (it rebases them), then `{again}`."
            )
            next_cmd = "bay fleet push"
    message = f"Stopped before the box was touched. {why} Nothing was deployed. {again_line} {tail}"
    message = " ".join(message.split())
    raise SyncError(
        message, reason=reason, exit_code=code, next_cmd=next_cmd, undo_failed=undone.failed
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


def _abort_rebase(repo: Path) -> str | None:
    """``git rebase --abort`` when a rebase is in progress. A problem line, or None."""
    if not _rebase_in_progress(repo):
        return None
    proc = gitrepo._run(repo, "rebase", "--abort", timeout=60)
    if _ok(proc) and not _rebase_in_progress(repo):
        return None
    why = "timed out" if proc is None else (gitrepo._last_line(str(proc.stderr)) or "failed")
    return (
        f"git rebase --abort {why}: the fleet is still in the middle of a rebase. "
        f"Finish it by hand: git -C {repo} rebase --abort, then git -C {repo} status"
    )


def pull_rebase(repo: Path) -> tuple[bool, str | None, list[str]]:
    """Fetch and rebase onto ``@{u}`` once. ``(ok, problem, conflicting files)``.

    Like :func:`fast_forward`, it first removes the untracked plan records
    that the upstream holds, and puts them back when the rebase fails. A
    refused or conflicting rebase is aborted; when the abort fails too, the
    problem says so and names the command to finish it.
    """
    if upstream_target(repo) is None:
        return False, "the fleet branch tracks no remote branch", []
    problem = _fetch(repo)
    if problem is not None:
        return False, problem, []
    removed = _set_aside_plans(repo)
    proc = gitrepo._run(repo, "rebase", "--no-autostash", "--quiet", "@{u}", timeout=180)
    if _ok(proc):
        _settle_plans(repo, removed, ok=True)
        return True, None, []
    detail = _git_refusal(
        (str(proc.stderr) + "\n" + str(proc.stdout)) if proc is not None else "git rebase timed out"
    )
    conflicts = _conflicts(repo)
    abort_problem = _abort_rebase(repo)
    _settle_plans(repo, removed, ok=False)
    if abort_problem:
        detail = f"{detail}; {abort_problem}"
    return False, detail, conflicts


def _push_checked(repo: Path) -> tuple[bool, str | None, bool]:
    """Push to the tracked branch and check that it holds HEAD.

    ``(pushed, problem, refused)``: ``refused`` is True when git refused the
    push (a rebase may help), False when it went somewhere else.
    """
    pushed, problem = push_upstream(repo)
    if not pushed:
        return False, problem, True
    head = gitrepo.head(repo) or "HEAD"
    held, fetch_problem = landed(repo, head)
    if held:
        return True, None, False
    if fetch_problem is not None:
        return False, f"pushed, but the fetch to check it failed: {fetch_problem}", False
    return (
        False,
        f"the push reported success, but {_upstream_name(repo)} does not hold the commit: "
        "the push did not reach the tracked branch (check remote.<name>.pushurl)",
        False,
    )


def push_with_rebase(repo: Path, *, attempts: int = 2) -> PushResult:
    """Push; on a refusal, rebase onto the remote and push again, at most ``attempts`` times.

    Every push names the remote and branch of ``@{u}`` and is checked with a
    fetch (:func:`_push_checked`).
    """
    pushed, problem, refused = _push_checked(repo)
    if pushed:
        return PushResult(True)
    result = PushResult(False, problem)
    if not refused:
        return result
    for _ in range(attempts):
        ok, rebase_problem, conflicts = pull_rebase(repo)
        if not ok:
            result.problem = rebase_problem
            result.conflicts = conflicts
            return result
        result.rebased += 1
        pushed, problem, refused = _push_checked(repo)
        if pushed:
            result.pushed, result.problem = True, None
            return result
        result.problem = problem
        if not refused:
            return result
    return result


# ── bay deploy and bay provision ────────────────────────────────────────────

#: At most this many dirty paths are named in a refusal.
DIRTY_LIST_MAX = 20


def _path_list(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:DIRTY_LIST_MAX])
    more = len(paths) - DIRTY_LIST_MAX
    return shown + (f" and {more} more" if more > 0 else "")


#: Files only Bay writes: the generated services file and every project lock.
OWNED_SERVICES = "group_vars/all/services.yml"


def bay_owned(rel: str) -> bool:
    """True for a fleet path that only Bay writes: ``services.yml`` or a ``bay.lock``."""
    parts = Path(rel).parts
    return rel == OWNED_SERVICES or (
        len(parts) == 3 and parts[0] == "projects" and parts[2] == "bay.lock"
    )


def hand_edited(repo: Path, rel: str) -> bool:
    """True for an uncommitted path that ``--allow-dirty`` never covers.

    Every ``bay.lock``. A ``services.yml`` only when its hash header does not
    match its body (a hand edit, no header, or a deleted file): one that
    ``bay compile --working-tree`` wrote is an ordinary dirty file.
    """
    if not bay_owned(rel):
        return False
    if rel != OWNED_SERVICES:
        return True
    from bay_cli import compiler

    try:
        text = (repo / rel).read_text()
    except (OSError, UnicodeDecodeError):
        return True
    recorded, body = compiler.split_header(text)
    return recorded is None or recorded != compiler.body_digest(body)


def owned_dirty_message(paths: Sequence[str]) -> str:
    return (
        f"the fleet has uncommitted edits in {len(paths)} file(s) that only Bay writes: "
        f"{_path_list(paths)}. --allow-dirty does not cover them. Restore each with "
        f"`git checkout HEAD -- <path>` in the fleet (git checkout HEAD -- "
        f"{' '.join(paths[:3])}), then run the command again. Change bay.toml or "
        "bay.fleet.toml instead"
    )


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
    refuses, uncommitted paths refuse (unless ``allow_dirty``; a hand edit in a
    file only Bay writes always refuses, :func:`hand_edited`), a fleet that
    is only behind is fast-forwarded, and a fleet that is ahead is pushed
    first (the claim; a refused push stops the run). A fleet with no remote
    or inside a bigger repo gets the dirty check only. Raises
    :class:`SyncError`; returns the fast-forward, if one ran.
    """
    kind = where(repo)
    if kind == NOT_GIT or not has_head(repo):
        return None
    remote = not offline and kind == TOPLEVEL
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
    owned = [p for p in st.dirty_paths if hand_edited(repo, p)]
    if owned:
        raise SyncError(owned_dirty_message(owned), reason=REASON_DIRTY, paths=owned)
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
    pushed, problem = push_upstream(repo)
    head = gitrepo.head(repo) or "HEAD"
    held, fetch_problem = landed(repo, head)
    if held:
        if pushed:
            say(f"fleet: pushed {st.ahead} local commit(s) to {st.upstream} before the {verb}")
        else:
            say(f"fleet: the push said `{problem}`, but the remote has the commits; going on")
        return
    if pushed and fetch_problem is None:
        raise SyncError(
            f"Stopped before the box was touched. The push reported success, but "
            f"{_upstream_name(repo)} does not hold the fleet commits: the push did not "
            "reach the tracked branch. Nothing was deployed.",
            reason=REASON_CLAIM_REFUSED,
            exit_code=EXIT_CLAIM_REFUSED,
            hint="Check remote.<name>.pushurl in the fleet's git config.",
        )
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
