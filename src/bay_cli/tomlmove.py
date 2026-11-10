"""``bay toml move <name> --to fleet``: move a project's bay.toml from its app repo into the fleet.

The mirror of ``bay adopt`` (``bay toml move <name> --to repo``,
:mod:`bay_cli.adopt`). The project keeps its bay.toml in its app repo today
(the lock names ``repo`` and ``toml_path``). After the move:

* the fleet holds ``projects/<name>/bay.toml``: the bytes of the file at the
  PINNED app commit, not at the branch head. Every file a mount reads from
  beside the toml lands beside it in the folder, at the same relative path.
  A ``fleet:`` mount, and a mount the compile reads from the fleet's
  ``files/`` tree, stay where they are. The file keeps its ``fleet =`` line
  (the schema needs it in either place). No ``[build] repo`` is added: the
  compile takes the lock ``repo`` when the toml names none, so the compiled
  build repo string stays the same, and a move back gives the same bytes;
* the lock is in fleet form: ``repo`` stays (it is the repo the box builds
  from), ``toml_path`` goes, ``commit`` and every env ``commit`` are the move
  commit (for a project in the fleet the pin is a fleet commit). Each env
  records ``adopted.from_app_commit`` (the app commit it ran before the move)
  and loses ``previous``, which pins an app commit that a fleet pin cannot roll
  back to. Every other adopted name stays, so no container, volume, database
  or config path is renamed;
* two fleet commits: ``bay: move <name> bay.toml to the fleet`` (the files),
  then ``bay: pin <name> after the move`` (the lock, which names the first
  commit). The pin cannot sit in the commit it names.

Nothing is pushed. ``bay up`` accepts a fleet that is only ahead, and it pushes
the fleet with its own claim. The first ``bay up`` moves no code: a project in
the fleet sends no code target, so the running image stays. It writes the box
a ``rebuild.sh`` without the hold-guard keys (``bay_toml_path``,
``bay_toml_hash``, ``bay_build_hash``, ``bay_toml_files``): those are the only
compiled difference. ``bay plan`` ignores them for the project's containers, so it shows
no step for them. It shows one ``bay-webhook`` recreate step (safe) for each box of the
box environment that runs a webhook receiver: the config file of each receiver holds
``bay_toml_path``, and the hash of that file is a label of its container.
After that ``bay up``, the push that deletes bay.toml from the app repo is a
normal code push.

Bay refuses before it changes anything when a precondition fails. ``check``
reports the files and the lock change and writes nothing.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli import bay_toml, gitrepo, layout, lockfile, reposource
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import FILES_DIR, LOCK_FILE, PROJECTS_DIR

MOVE_MESSAGE = "bay: move {name} bay.toml to the fleet"
PIN_MESSAGE = "bay: pin {name} after the move"
#: The refusal of ``bay rollback`` straight after a move to the fleet (docs/plan.md).
ROLLBACK_AFTER_MOVE = (
    "bay toml move put the bay.toml in the fleet, so there is no earlier fleet pin to return to"
)
#: Shown in a ``--check`` lock diff where the move commit will go.
_PENDING = "<the move commit>"


@dataclass
class _Copy:
    """One file that moves: app repo path -> fleet path (both relative)."""

    source: str
    dest: str


@dataclass
class MovePlan:
    """Everything a move to the fleet does, worked out before anything is written."""

    name: str
    fleet_name: str
    repo: str
    toml_path: str
    from_app_commit: str
    #: Where the files of the pinned commit were extracted.
    work: Path
    copies: list[_Copy] = field(default_factory=list)
    #: Fleet files a mount reads that stay where they are.
    kept: list[str] = field(default_factory=list)
    #: App repo paths the operator deletes after the bay up.
    delete: list[str] = field(default_factory=list)
    #: App repo paths that stay, because another project reads them.
    keep_in_repo: list[str] = field(default_factory=list)
    lock_before: dict[str, Any] = field(default_factory=dict)
    lock_after: dict[str, Any] = field(default_factory=dict)
    envs: list[str] = field(default_factory=list)
    primary_env: str = "production"
    notes: list[str] = field(default_factory=list)


def move_to_fleet(cx: Context, cwd: Path, name: str, *, check: bool = False) -> dict[str, Any]:
    """Move project ``name`` of the fleet at ``cx`` from its app repo into the fleet.

    ``cwd`` is where the command ran: a checkout of the app repo there is read
    before the fleet's repo cache. Returns a result document. With ``check``
    nothing is written. Raises BayError on any refused precondition, before
    anything changes.
    """
    from bay_cli import fleetsync

    with tempfile.TemporaryDirectory(prefix="bay-move-") as tmp:
        if check:
            if layout.pending(cx.fleet_root).changed:
                raise BayError(
                    "the fleet still keeps its locks in the old place",
                    code=ErrorCode.CONFLICT,
                    hint="Run `bay compile` or `bay up` once (it moves them), then "
                    "`bay toml move --check` again.",
                )
            notes = _sync_gate(cx, check=True)
            plan = _prepare(cx, cwd, name, Path(tmp))
            plan.notes[:0] = notes
            return _result(plan, check=True)
        with fleetsync.run_lock(cx.fleet_root, "toml move"):
            notes = _sync_gate(cx, check=False)
            layout.ensure_for_write(cx.fleet_root)
            plan = _prepare(cx, cwd, name, Path(tmp))
            plan.notes[:0] = notes
            return _apply(cx, plan)


# ── the fleet sync gate ─────────────────────────────────────────────────────


def _sync_gate(cx: Context, *, check: bool) -> list[str]:
    """The fleet checks of ``bay up``, before the move reads or writes anything.

    An unreachable remote, a branch with no upstream, a diverged fleet and
    uncommitted fleet files refuse (exit 41), and so does the Bay version
    floor of ``bay.compat.json`` (exit 45). A fleet that is only behind is
    fast-forwarded (with ``check``, only noted). A fleet that is ahead is
    fine: the next ``bay up`` pushes it.
    """
    from bay_cli import compat, fleetsync

    root = cx.fleet_root
    kind = fleetsync.where(root)
    if kind == fleetsync.NOT_GIT or not fleetsync.has_head(root):
        raise BayError(f"the fleet {root} is not a git repo with a commit")
    remote = kind == fleetsync.TOPLEVEL
    st = fleetsync.state(root, fetch=remote)
    remote = remote and not st.no_remote
    if remote:
        if st.unreachable:
            raise fleetsync.SyncError(
                f"cannot fetch the fleet remote: {st.unreachable}",
                reason=fleetsync.REASON_UNREACHABLE,
                hint="Fix the remote, then run the move again.",
            )
        if st.upstream is None:
            raise fleetsync.SyncError(
                "the fleet branch tracks no remote branch",
                reason=fleetsync.REASON_NO_UPSTREAM,
                hint="Set one with git branch -u origin/<branch>.",
            )
        if st.diverged:
            raise fleetsync.SyncError(
                fleetsync.diverged_message(st),
                reason=fleetsync.REASON_DIVERGED,
                next_cmd="bay fleet push",
            )
    owned = [p for p in st.dirty_paths if fleetsync.hand_edited(root, p)]
    if owned:
        raise fleetsync.SyncError(
            fleetsync.owned_dirty_message(owned), reason=fleetsync.REASON_DIRTY, paths=owned
        )
    if st.dirty_paths:
        raise fleetsync.SyncError(
            f"the fleet has {len(st.dirty_paths)} uncommitted path(s): "
            f"{', '.join(st.dirty_paths[:5])}{' ...' if len(st.dirty_paths) > 5 else ''}",
            reason=fleetsync.REASON_DIRTY,
            hint="A move commits only the move. Commit the paths and run `bay fleet push`, "
            "or drop them, then move.",
            paths=st.dirty_paths,
        )
    compat.require(cx, allow_older=False)
    notes: list[str] = []
    if remote and st.behind:
        if check:
            notes.append(
                f"the fleet is {st.behind} commit(s) behind {st.upstream}; the move "
                "fast-forwards it first, so this check may differ"
            )
        else:
            pulled = fleetsync.fast_forward(root)
            fleetsync.notice(pulled.line())
    return notes


# ── preconditions and the plan ──────────────────────────────────────────────


def _prepare(cx: Context, cwd: Path, name: str, work: Path) -> MovePlan:
    from bay_cli.plan import _join, _mount_sources, load_fleet_doc, load_project, read_wanted

    fleet_root = cx.fleet_root
    fleet_doc = load_fleet_doc(cx)
    fleet_name = str(fleet_doc.get("name") or fleet_root.name)
    folder = f"{PROJECTS_DIR}/{name}"
    lock_rel = f"{folder}/{LOCK_FILE}"
    if (fleet_root / folder / "bay.toml").is_file():
        raise BayError(
            f"{name} already lives in fleet {fleet_name} ({folder}/bay.toml)",
            code=ErrorCode.CONFLICT,
            hint="--to fleet moves a project whose bay.toml is in its app repo. "
            f"`bay toml move {name} --to repo` moves it the other way.",
        )
    try:
        raw = lockfile.read(lockfile.lock_path(fleet_root, name))
    except ValueError as exc:
        raise BayError(f"{lock_rel} is not valid JSON: {exc}") from None
    if raw is None:
        raise BayError(
            f"project {name} is not in fleet {fleet_name}",
            code=ErrorCode.NOT_FOUND,
            hint=f"bay toml move works on a project the fleet knows ({lock_rel}).",
        )
    repo = str(raw.get("repo") or "")
    if not repo:
        raise BayError(f"{lock_rel} names no repo", hint="Set repo in the lock (bay init does).")
    pin = str(raw.get("commit") or "")
    if not pin:
        raise BayError(
            f"{name} has no pinned commit yet",
            code=ErrorCode.CONFLICT,
            hint="Run `bay up` once, so the move has a pinned bay.toml to copy.",
        )

    records = {str(e): (r or {}) for e, r in (raw.get("envs") or {}).items()}
    for env, record in sorted(records.items()):
        result = record.get("result")
        if record.get("claimed_by") or result == "pending":
            raise BayError(
                f"{name} {env}: a bay up claimed it and has not ended (result pending)",
                code=ErrorCode.CONFLICT,
                hint="A move starts from a settled state. Let that bay up end, or repair "
                "it with another bay up, then move.",
            )
        if result == "failed":
            raise BayError(
                f"{name} {env}: the last bay up failed (result failed)",
                code=ErrorCode.CONFLICT,
                hint="A move starts from a settled state. Run `bay up` until it ends ok, "
                "then move.",
            )
    frozen = sorted(e for e, r in records.items() if r.get("frozen"))
    if frozen:
        raise BayError(
            f"{name} is frozen by bay rollback in {', '.join(frozen)}",
            code=ErrorCode.CONFLICT,
            hint="The freeze names an app commit. Clear it with a bay up to a newer "
            "commit first, then move.",
        )

    proj = load_project(cx, name, cwd=cwd)
    toml_rel = proj.toml_path
    toml_dir = str(Path(toml_rel).parent)
    src, full = _locate(cx, repo, pin, cwd, name)
    data = gitrepo.show_file(src, full, toml_rel)
    if data is None:
        raise BayError(f"{toml_rel} is not in commit {full[:12]} of {repo}")
    doc = _parse(data, f"{toml_rel} at {full[:12]}")
    if doc.get("name") != name:
        raise BayError(
            f"{toml_rel} at {full[:12]}: name is {doc.get('name')!r}, not {name!r}",
            code=ErrorCode.CONFLICT,
        )
    if fleet_doc.get("name") and doc.get("fleet") != fleet_doc.get("name"):
        raise BayError(
            f"{toml_rel} at {full[:12]}: fleet is {doc.get('fleet')!r}, but this fleet is "
            f"{fleet_doc.get('name')!r}",
            code=ErrorCode.CONFLICT,
        )
    problems = bay_toml.validate(doc)
    if problems:
        raise BayError(
            f"{toml_rel} at {full[:12]} is not valid:\n  "
            + "\n  ".join(str(p) for p in problems)
        )
    envs = sorted(str(e) for e in (doc.get("deploy") or {}))
    pinned_track = sorted(e for e in envs if bay_toml.track(doc, e) == "pin")
    if pinned_track:
        raise BayError(
            f"{name} sets track = \"pin\" in {', '.join(pinned_track)}; a project in the fleet "
            "cannot use it (the pin is a fleet commit, not a code commit)",
            code=ErrorCode.CONFLICT,
            hint='Set track = "branch" (or remove the line) in the app repo, push, run '
            "`bay up`, then move.",
        )
    sources = _mount_sources(doc)
    for rel in sources:
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise BayError(f"{toml_rel}: from = {rel!r} must stay beside the bay.toml")

    # Every env pin and WANTED must carry the same bay.toml and mounted files as
    # the project pin: the move copies what runs, nothing newer.
    def signature(where: Path, commit: str, docs: list[dict[str, Any]]) -> dict[str, str | None]:
        rels = [toml_rel]
        for d in docs:
            rels.extend(_join(toml_dir, m) for m in _mount_sources(d))
        return {r: gitrepo.object_id(where, commit, r) for r in dict.fromkeys(rels)}

    def changed(a: dict[str, str | None], b: dict[str, str | None]) -> list[str]:
        return [r for r in a if a[r] != b.get(r)]

    for env, record in sorted(records.items()):
        other = record.get("commit")
        if not other or str(other) == pin:
            continue
        where, other_full = _locate(cx, repo, str(other), cwd, name)
        if other_full == full:
            continue
        other_doc = _parse(gitrepo.show_file(where, other_full, toml_rel) or b"", toml_rel)
        both = [doc, other_doc]
        diff = changed(signature(src, full, both), signature(where, other_full, both))
        if diff:
            raise BayError(
                f"{name} {env} runs another config ({', '.join(diff)} at {other_full[:12]}) than "
                f"the project pin {full[:12]}",
                code=ErrorCode.CONFLICT,
                hint="Run `bay up` for every environment first, so they share one pin.",
            )
    for env in envs:
        wanted = read_wanted(proj, None, env)
        if wanted.problems or wanted.commit is None:
            raise BayError(
                f"{name} {env}: " + "; ".join(wanted.problems or ["no WANTED commit"]),
                hint="Fix the app repo or its cache (bay plan shows the same problem), then move.",
            )
        docs = [doc, *([wanted.doc] if wanted.doc else [])]
        diff = changed(
            signature(src, full, docs), signature(proj.checkout, wanted.commit, docs)
        )
        if diff:
            raise BayError(
                f"{name} {env}: {', '.join(diff)} changed after bay up pinned it (pinned "
                f"{full[:12]}, WANTED {wanted.commit[:12]})",
                code=ErrorCode.CONFLICT,
                hint="Run `bay plan` and `bay up` first, so the move moves exactly what runs.",
            )

    others = [p for p in gitrepo.tracked_files(fleet_root, folder) if p != lock_rel]
    if others:
        raise BayError(
            f"{folder} holds files other than {LOCK_FILE}: {', '.join(others[:5])}",
            code=ErrorCode.CONFLICT,
            hint="Move them away and commit, so the folder takes only the moved files.",
        )

    plan = MovePlan(
        name=name,
        fleet_name=fleet_name,
        repo=repo,
        toml_path=toml_rel,
        from_app_commit=full,
        work=work,
        envs=envs,
        primary_env=str(fleet_doc.get("primary_env", "production")),
    )

    # The files: bay.toml, then every mount read from beside it at the pin.
    present = [s for s in sources if gitrepo.has_path(src, full, _join(toml_dir, s))]
    try:
        gitrepo.extract(src, full, [toml_rel, *(_join(toml_dir, s) for s in present)], work)
    except gitrepo.GitError as exc:
        raise BayError(f"cannot read commit {full[:12]} of {repo}: {exc}") from None
    plan.copies.append(_Copy(toml_rel, f"{folder}/bay.toml"))
    for rel in present:
        app_rel = _join(toml_dir, rel)
        path = work / app_rel
        files = sorted(p for p in path.rglob("*") if not p.is_dir()) if path.is_dir() else [path]
        for item in files:
            sub = item.relative_to(work / toml_dir if toml_dir not in ("", ".") else work)
            source = item.relative_to(work).as_posix()
            plan.copies.append(_Copy(source, f"{folder}/{sub.as_posix()}"))
    targets = _targets(raw)
    for rel in sources:
        if rel in present:
            continue
        for target in sorted(targets.get(rel) or {f"{name}/{rel}"}):
            if (fleet_root / FILES_DIR / target).exists():
                plan.kept.append(f"{FILES_DIR}/{target}")
    plan.delete = [toml_rel, *(_join(toml_dir, s) for s in present)]

    # Another project that keeps its bay.toml in the same app repo.
    plan.keep_in_repo, notes = _shared_repo(cx, cwd, name, repo, toml_rel, plan.delete)
    plan.delete = [p for p in plan.delete if p not in plan.keep_in_repo]
    plan.notes.extend(notes)

    plan.lock_before = copy.deepcopy(raw)
    plan.lock_after = _new_lock(raw, envs)
    found = lockfile.problems(_with_commit(plan.lock_after, "0" * 40))
    if found:
        raise BayError("the moved lock would not be valid: " + "; ".join(found))
    return plan


def _locate(cx: Context, repo: str, commit: str, cwd: Path, name: str) -> tuple[Path, str]:
    found = reposource.find_commit(cx.fleet_root, repo, commit, cwd=cwd)
    if found.path is None or found.commit is None:
        if found.searched_checkout is not None:
            tried = (
                f"the repo cache of {repo} and the checkout {found.searched_checkout}, "
                "where you are"
            )
        else:
            tried = f"the repo cache of {repo}, and you are not in a checkout of that repo"
        cause = f" ({found.detail})" if found.detail else ""
        raise BayError(
            f"{name}: Bay cannot find the pinned commit {commit[:12]}. It looked in "
            f"{tried}{cause}.",
            hint="The most likely cause: the pinned commit is not pushed to the remote yet. "
            "Push it, or run the move inside the checkout that has it. If it is pushed, "
            "check the repo URL in the lock and your access to it.",
        )
    return found.path, found.commit


def _parse(data: bytes, label: str) -> dict[str, Any]:
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BayError(f"{label}: {exc}") from None
    return doc


def _targets(raw: dict[str, Any]) -> dict[str, set[str]]:
    """``{from: {adopted targets}}`` over every environment of the lock."""
    out: dict[str, set[str]] = {}
    for record in (raw.get("envs") or {}).values():
        files = ((record or {}).get("adopted") or {}).get("files") or {}
        for key, target in files.items():
            out.setdefault(str(key), set()).add(str(target))
    return out


def _shared_repo(
    cx: Context, cwd: Path, name: str, repo: str, toml_rel: str, moving: list[str]
) -> tuple[list[str], list[str]]:
    """``(app repo paths to keep, notes)`` for the projects that share the repo.

    Another project whose bay.toml lives in the same app repo keeps working:
    the move copies, it removes nothing there. The files that project reads
    too stay in the app repo after the move. Two locks that name the same
    bay.toml of one repo are ambiguous: refused.
    """
    from bay_cli.adopt import _overlaps
    from bay_cli.plan import _join, _mount_sources, lock_files

    keep: list[str] = []
    notes: list[str] = []
    root = cx.fleet_root
    for stem, path in lock_files(root):
        if stem == name or (root / PROJECTS_DIR / stem / "bay.toml").is_file():
            continue
        try:
            other = lockfile.read(path) or {}
        except (OSError, ValueError):
            continue
        if not reposource.same_repo(str(other.get("repo") or ""), repo):
            continue
        other_toml = str(other.get("toml_path", "bay.toml"))
        if other_toml == toml_rel:
            raise BayError(
                f"projects {name} and {stem} both name {toml_rel} in {repo}",
                code=ErrorCode.CONFLICT,
                hint="One bay.toml belongs to one project. Fix toml_path in one lock first.",
            )
        reads = [other_toml]
        commit = other.get("commit")
        if commit:
            found = reposource.find_commit(root, repo, str(commit), cwd=cwd)
            data = (
                gitrepo.show_file(found.path, found.commit, other_toml)
                if found.path is not None and found.commit is not None
                else None
            )
            if data is not None:
                try:
                    doc = tomllib.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, tomllib.TOMLDecodeError):
                    doc = {}
                odir = str(Path(other_toml).parent)
                reads.extend(_join(odir, m) for m in _mount_sources(doc))
        hits = [p for p in moving if any(_overlaps(p, r) for r in reads)]
        keep.extend(h for h in hits if h not in keep)
        notes.append(
            f"{stem} also keeps its bay.toml in {repo}. After the bay up, the push that "
            f"deletes the moved files may build and recreate {stem} too, unless its "
            "[build] watch leaves those files out"
        )
        if hits:
            notes.append(f"{stem} reads {', '.join(hits)} too, so they stay in the app repo")
    return keep, notes


def _new_lock(raw: dict[str, Any], envs: list[str]) -> dict[str, Any]:
    """The lock in fleet form; ``commit`` fields are filled in once the move commit exists."""
    lock = copy.deepcopy(raw)
    lock.pop("toml_path", None)
    records = lock.setdefault("envs", {})
    for env in sorted(set(records) | set(envs)):
        record = dict(records.get(env) or {})
        before = record.get("commit") or raw.get("commit")
        record.pop("previous", None)
        adopted = dict(record.get("adopted") or {})
        # The marks of an earlier move into the app repo mean nothing now.
        adopted.pop("from_fleet_commit", None)
        adopted.pop("app_commit", None)
        if before:
            adopted["from_app_commit"] = str(before)
        record["adopted"] = adopted
        records[env] = record
    return lock


def _with_commit(lock: dict[str, Any], commit: str) -> dict[str, Any]:
    """``lock`` with the project pin, and every environment that had a pin, at ``commit``."""
    out = copy.deepcopy(lock)
    out["commit"] = commit
    for record in (out.get("envs") or {}).values():
        if record.get("commit"):
            record["commit"] = commit
    return out


# ── applying ────────────────────────────────────────────────────────────────


def _apply(cx: Context, plan: MovePlan) -> dict[str, Any]:
    from bay_cli.adopt import _lock_diff

    root = cx.fleet_root
    written: list[Path] = []
    for item in plan.copies:
        dest = root / item.dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.work / item.source, dest, follow_symlinks=False)
        written.append(dest)
    try:
        move_commit = gitrepo.commit_paths(root, written, MOVE_MESSAGE.format(name=plan.name))
    except gitrepo.GitError as exc:
        for path in written:
            path.unlink(missing_ok=True)
        raise BayError(
            f"cannot commit the move in the fleet: {exc}",
            hint="The copied files were removed again. Fix the problem, then move again.",
        ) from None

    lock_file = lockfile.lock_path(root, plan.name)
    old_bytes = lock_file.read_bytes()
    final = _with_commit(plan.lock_after, move_commit)
    try:
        lockfile.write(lock_file, final)
        lock_commit = gitrepo.commit_paths(
            root, [lock_file], PIN_MESSAGE.format(name=plan.name)
        )
    except (gitrepo.GitError, lockfile.LockWriteError) as exc:
        lock_file.write_bytes(old_bytes)
        raise BayError(
            f"the move commit {move_commit[:12]} is made, but the lock commit failed: {exc}",
            hint=f"Check `git status` in the fleet. Undo the move with `git revert "
            f"{move_commit[:12]}`, or pin the lock by hand.",
        ) from None
    result = _result(plan, check=False)
    result.update(
        {
            "fleet_commit": move_commit,
            "lock_commit": lock_commit,
            "lock_diff": _lock_diff(plan.lock_before, final),
        }
    )
    return result


def _result(plan: MovePlan, *, check: bool) -> dict[str, Any]:
    from bay_cli.adopt import _lock_diff, plan_expectation

    env = plan.primary_env if plan.primary_env in plan.envs else (
        plan.envs[0] if plan.envs else plan.primary_env
    )
    shown = _with_commit(plan.lock_after, _PENDING)
    return {
        "check": check,
        "to": "fleet",
        "project": plan.name,
        "fleet": plan.fleet_name,
        "repo": plan.repo,
        "toml_path": plan.toml_path,
        "from_app_commit": plan.from_app_commit,
        "files": [{"from": c.source, "to": c.dest} for c in plan.copies],
        "kept": list(plan.kept),
        "delete": list(plan.delete),
        "keep_in_repo": list(plan.keep_in_repo),
        "envs": list(plan.envs),
        "lock_diff": _lock_diff(plan.lock_before, shown),
        "fleet_commit": None,
        "lock_commit": None,
        "notes": [
            *plan.notes,
            plan_expectation(f"bay plan {env} --project {plan.name}"),
        ],
        "next": [
            f"bay plan {env} --project {plan.name}",
            f"bay up {env} --project {plan.name}",
            f"git rm -r {' '.join(plan.delete)}",
            "git commit",
            "git push",
        ],
    }
