"""``bay adopt <name>``: move a project's bay.toml from the fleet into its app repo.

Run it inside a checkout of the app repo. The project lives in the fleet
today (``projects/<name>/bay.toml``). After the adopt:

* the app repo holds ``bay.toml`` (at ``--toml-path``) and every file the
  project folder held, beside it, in one local commit
  (``chore: add bay.toml (adopted from fleet <fleet>)``);
* files a mount read from the fleet's ``files/`` tree (the old place
  ``files/<name>/<from>``, or an adopted path) move beside the toml too;
  a ``fleet:`` mount stays in the fleet;
* the lock is in repo form: ``repo``, ``toml_path``, ``commit`` (the app
  commit) and, per environment, ``adopted.from_fleet_commit`` (the fleet
  HEAD before the adopt). Every other adopted name stays, so no container,
  volume, database or config path is renamed. ``previous`` is cleared: the
  previous pin is a fleet commit, which an app repo pin cannot roll back to;
* the fleet drops the folder contents except ``bay.lock`` in one commit
  (``bay: adopt <name> into <repo>``).

Nothing is pushed. The order is adopt, ``bay up``, then ``git push``. The lock
records the adopt commit as ``adopted.app_commit``; ``bay up`` accepts that
one commit before it is pushed, and moves no code for it: the box only gets
the new ``rebuild.sh`` with the hold-guard keys (``bay_toml_hash`` and
friends). The push that follows then changes only the bay.toml and the files
beside it, which that script sees as a config-only push: no build, no
recreate. Pushed first, the old script (no ``BAY_TOML_PATH``) would build and
recreate the app. The compiled output is the same before and after apart from
those keys: the box paths are ``config/<target>`` either way
(:mod:`bay_cli.compiler`), so the next ``bay plan`` shows zero steps.

Bay refuses before it changes anything when a precondition fails, so a
refusal never leaves half an adopt behind. ``check`` reports the files and
the lock change and writes nothing.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli import bay_toml, gitrepo, layout, lockfile, reposource
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import FILES_DIR, FLEET_PREFIX, LOCK_FILE, PROJECTS_DIR

APP_MESSAGE = "chore: add bay.toml (adopted from fleet {fleet})"
FLEET_MESSAGE = "bay: adopt {name} into {repo}"
#: The refusal of ``bay rollback`` straight after an adopt (docs/plan.md).
ROLLBACK_AFTER_ADOPT = "the previous pin is a fleet commit; use `bay up --at <commit>`"
#: Shown in a ``--check`` lock diff where the app commit will go.
_PENDING = "<the adopt commit>"


@dataclass
class _Copy:
    """One file that moves: fleet path -> app repo path (both relative)."""

    source: str
    dest: str
    #: The text to write instead of the file's bytes (a bay.toml without ``[build] repo``).
    text: str | None = None

    def content(self, fleet_root: Path) -> bytes:
        return self.text.encode() if self.text is not None else (fleet_root / self.source).read_bytes()


@dataclass
class AdoptPlan:
    """Everything an adopt does, worked out before anything is written."""

    name: str
    fleet_name: str
    fleet_head: str
    repo_root: Path
    repo: str
    toml_path: str
    copies: list[_Copy] = field(default_factory=list)
    #: Fleet paths ``git rm`` takes (the folder contents, the moved files/ copies).
    removals: list[str] = field(default_factory=list)
    #: Fleet files a mount read that stay, because another fleet entry names them.
    kept: list[str] = field(default_factory=list)
    lock_before: dict[str, Any] = field(default_factory=dict)
    lock_after: dict[str, Any] = field(default_factory=dict)
    envs: list[str] = field(default_factory=list)


def adopt(
    cx: Context,
    cwd: Path,
    name: str,
    *,
    toml_path: str | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Adopt project ``name`` of the fleet at ``cx`` into the app repo at ``cwd``.

    Returns a result document. With ``check`` nothing is written. Raises
    BayError on any refused precondition, before anything changes.
    """
    if check:
        if layout.pending(cx.fleet_root).changed:
            raise BayError(
                "the fleet still keeps its locks in the old place",
                code=ErrorCode.CONFLICT,
                hint="Run `bay compile` or `bay up` once (it moves them), then "
                "`bay adopt --check` again.",
            )
    else:
        layout.ensure_for_write(cx.fleet_root)
    plan = _prepare(cx, cwd, name, toml_path)
    if check:
        return _result(plan, check=True)
    return _apply(cx, plan)


# ── preconditions and the plan ──────────────────────────────────────────────


def _prepare(cx: Context, cwd: Path, name: str, toml_path: str | None) -> AdoptPlan:
    from bay_cli.plan import _mount_sources, load_fleet_doc, scope_spec
    from bay_cli.project_init import _toml_rel

    fleet_root = cx.fleet_root
    fleet_doc = load_fleet_doc(cx)
    fleet_name = str(fleet_doc.get("name") or fleet_root.name)
    fleet_head = gitrepo.head(fleet_root)
    if not gitrepo.is_repo(fleet_root) or fleet_head is None:
        raise BayError(f"the fleet {fleet_root} is not a git repo with a commit")

    folder = f"{PROJECTS_DIR}/{name}"
    toml_rel_fleet = f"{folder}/bay.toml"
    lock_file = lockfile.lock_path(fleet_root, name)
    try:
        raw = lockfile.read(lock_file)
    except ValueError as exc:
        raise BayError(f"{folder}/{LOCK_FILE} is not valid JSON: {exc}") from None
    if not (fleet_root / toml_rel_fleet).is_file():
        where = (
            f"; it already lives in its repo {raw['repo']}"
            if raw is not None and raw.get("repo")
            else ""
        )
        raise BayError(
            f"fleet {fleet_name} has no {toml_rel_fleet}{where}",
            code=ErrorCode.NOT_FOUND,
            hint="bay adopt moves a project that lives in the fleet into its app repo.",
        )

    if not gitrepo.is_repo(cwd):
        raise BayError(
            f"{cwd} is not a git repo",
            hint="Run bay adopt inside a checkout of the app repo.",
        )
    root = gitrepo.toplevel(cwd) or cwd
    origin = gitrepo.remote_url(root)
    if origin is None:
        raise BayError(
            f"{root} has no origin remote",
            hint="Bay finds the repo by its origin URL. Give the repo an origin remote, "
            "then run bay adopt again.",
        )
    if gitrepo.head(root) is None:
        raise BayError(
            f"{root} has no commit yet", hint="Commit and push the app first, then adopt."
        )
    rel = _toml_rel(toml_path)
    if (root / rel).exists():
        raise BayError(
            f"{root / rel} already exists",
            code=ErrorCode.CONFLICT,
            hint="bay adopt writes a new bay.toml. Remove or move the file, or pass "
            "--toml-path.",
        )
    if gitrepo.dirty(root):
        raise BayError(
            f"the app repo {root} has uncommitted changes",
            code=ErrorCode.CONFLICT,
            hint="Commit or drop them first, so the adopt commit holds only the move.",
        )
    if gitrepo.path_dirty(fleet_root, folder):
        raise BayError(
            f"{folder} has uncommitted changes in the fleet",
            code=ErrorCode.CONFLICT,
            hint="Commit or drop them first; bay adopt moves what the fleet committed.",
        )

    try:
        doc = bay_toml.load(fleet_root / toml_rel_fleet)
    except bay_toml.BayTomlError as exc:
        raise BayError(f"{toml_rel_fleet}: {exc}") from None
    if doc.get("name") != name:
        raise BayError(
            f"{toml_rel_fleet}: name is {doc.get('name')!r}, not {name!r}",
            code=ErrorCode.CONFLICT,
            hint="The folder name and the name in bay.toml must agree before a move.",
        )
    problems = bay_toml.validate(doc)
    if problems:
        raise BayError(
            f"{toml_rel_fleet} is not valid:\n  " + "\n  ".join(str(p) for p in problems)
        )

    if raw is None:
        raw = lockfile.new_lock(name, repo=None)
    # The compile builds from [build] repo, then from the lock repo. Either
    # must be the repo of this checkout.
    toml_repo = _build_repo(doc)
    if toml_repo and not reposource.same_repo(toml_repo, origin):
        raise BayError(
            f"{toml_rel_fleet} names [build] repo {toml_repo}, but this checkout's origin is "
            f"{origin}",
            code=ErrorCode.CONFLICT,
            hint="Run bay adopt in a checkout of the repo that [build] names.",
        )
    if (
        toml_repo
        and raw.get("repo")
        and not reposource.same_repo(str(raw["repo"]), str(toml_repo))
    ):
        # The adopt would overwrite the lock repo with [build] repo. Refuse, so
        # no one repoints a build by accident.
        raise BayError(
            f"{toml_rel_fleet} names [build] repo {toml_repo}, but {folder}/{LOCK_FILE} names "
            f"repo {raw['repo']}",
            code=ErrorCode.CONFLICT,
            hint="Make the two name the same repo (fix [build] repo or the lock), then adopt.",
        )
    if not toml_repo and raw.get("repo") and not reposource.same_repo(str(raw["repo"]), origin):
        raise BayError(
            f"{folder}/{LOCK_FILE} names repo {raw['repo']}, but this checkout's origin is "
            f"{origin}",
            code=ErrorCode.CONFLICT,
            hint="Run bay adopt in a checkout of the repo the lock names.",
        )
    # The repo string is what the box clones from; keep the one the compile
    # used ([build] repo, then the lock), so the compiled build entry does not change.
    repo = str(toml_repo or raw.get("repo") or origin)

    frozen = sorted(e for e, r in (raw.get("envs") or {}).items() if (r or {}).get("frozen"))
    if frozen:
        raise BayError(
            f"{name} is frozen by bay rollback in {', '.join(frozen)}",
            code=ErrorCode.CONFLICT,
            hint="The freeze names a fleet commit. Clear it with a bay up to a newer "
            "commit first, then adopt.",
        )

    # WANTED must be PINNED: the adopt moves what runs, nothing newer.
    wanted = gitrepo.last_change(fleet_root, scope_spec(name), fleet_head)
    pins = {"the project": raw.get("commit")}
    for env, record in sorted((raw.get("envs") or {}).items()):
        pins[env] = (record or {}).get("commit")
    for label, pin in pins.items():
        if not pin:
            continue
        full = gitrepo.resolve_commit(fleet_root, str(pin))
        if full != wanted:
            raise BayError(
                f"{folder} changed after bay up pinned it ({label}: {str(pin)[:12]}, the "
                f"fleet has {str(wanted)[:12]})",
                code=ErrorCode.CONFLICT,
                hint="Run `bay plan` and `bay up` first, so the adopt moves exactly what runs.",
            )

    toml_dir = str(Path(rel).parent)
    plan = AdoptPlan(
        name=name,
        fleet_name=fleet_name,
        fleet_head=fleet_head,
        repo_root=root,
        repo=repo,
        toml_path=rel,
        envs=sorted(doc.get("deploy") or {}),
    )
    dests: dict[str, str] = {}

    def add_copy(source: str, dest: str) -> None:
        seen = dests.get(dest)
        if seen is not None and seen != source:
            if (fleet_root / seen).read_bytes() != (fleet_root / source).read_bytes():
                raise BayError(
                    f"{seen} and {source} both land at {dest} in the app repo, and they differ",
                    code=ErrorCode.CONFLICT,
                    hint="Keep one copy in the fleet, commit, then adopt.",
                )
            return
        if seen is None:
            dests[dest] = source
            plan.copies.append(_Copy(source, dest))

    # 1. The mounts. The compiler reads files/<target> before the file beside
    #    the toml when the target is adopted, and only as a fallback otherwise.
    #    Whatever it reads is what runs, so that copy moves.
    targets = _targets(raw)
    shared = _shared_fleet_files(fleet_root, name)
    beside_root = fleet_root / folder
    for src in _mount_sources(doc):
        if Path(src).is_absolute() or ".." in Path(src).parts:
            raise BayError(f"{toml_rel_fleet}: from = {src!r} must stay inside the folder")
        chosen: str | None = None
        adopted = targets.get(src)
        candidates = sorted(adopted) if adopted else [f"{name}/{src}"]
        beside = beside_root / src
        for target in candidates:
            old = fleet_root / FILES_DIR / target
            if old.exists() and (adopted or not beside.exists()):
                chosen = f"{FILES_DIR}/{target}"
                break
        if chosen is None:
            if not beside.exists():
                raise BayError(
                    f"{toml_rel_fleet}: from = {src!r} is neither {folder}/{src} nor "
                    f"{FILES_DIR}/{candidates[0]} in the fleet",
                    hint="Fix the mount (bay compile shows the same problem), then adopt.",
                )
            continue  # beside the toml: the folder move below takes it
        if gitrepo.path_dirty(fleet_root, chosen):
            raise BayError(
                f"{chosen} has uncommitted changes in the fleet",
                code=ErrorCode.CONFLICT,
                hint="Commit or drop them first; bay adopt moves what the fleet committed.",
            )
        tracked = gitrepo.tracked_files(fleet_root, chosen)
        if not tracked:
            raise BayError(f"{chosen} is not committed in the fleet", hint="Commit it first.")
        for path in tracked:
            sub = Path(path).relative_to(chosen).as_posix() if path != chosen else ""
            add_copy(path, _join(toml_dir, src if not sub else f"{src}/{sub}"))
        if any(_overlaps(chosen, s) for s in shared):
            plan.kept.append(chosen)
        else:
            plan.removals.extend(tracked)

    # 2. The folder: bay.toml goes to --toml-path, every other file beside it.
    for path in gitrepo.tracked_files(fleet_root, folder):
        sub = Path(path).relative_to(folder).as_posix()
        if sub == LOCK_FILE:
            continue
        add_copy(path, rel if sub == "bay.toml" else _join(toml_dir, sub))
        plan.removals.append(path)

    for item in plan.copies:
        there = root / item.dest
        if there.exists() and there.read_bytes() != item.content(fleet_root):
            raise BayError(
                f"{there} already exists in the app repo and differs from {item.source}",
                code=ErrorCode.CONFLICT,
                hint="Remove or rename the file in the app repo, commit, then adopt.",
            )
    plan.removals = sorted(dict.fromkeys(plan.removals))
    if toml_repo:
        # The repo moves into the lock, so the app repo's bay.toml does not name it.
        stripped = strip_build_repo((fleet_root / toml_rel_fleet).read_text())
        for item in plan.copies:
            if item.source == toml_rel_fleet:
                item.text = stripped

    plan.lock_before = copy.deepcopy(raw)
    plan.lock_after = _new_lock(raw, repo, rel, fleet_head, plan.envs)
    found = lockfile.problems(_with_commit(plan.lock_after, "0" * 40))
    if found:
        raise BayError("the adopted lock would not be valid: " + "; ".join(found))
    return plan


def _build_repo(doc: dict[str, Any]) -> str | None:
    """``[build] repo`` of a bay.toml; None when unset. Refuses several different repos."""
    repos = bay_toml.build_repos(doc)
    if len(repos) > 1:
        raise BayError(
            f"the builds of this bay.toml name {len(repos)} repos: {', '.join(repos)}",
            hint="An app repo builds from one repo. Keep one [build] repo.",
        )
    return repos[0] if repos else None


_TABLE_RE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_REPO_RE = re.compile(r"^\s*repo\s*=")


def strip_build_repo(text: str) -> str:
    """``text`` without the ``repo`` line of every ``[build]`` table (a line edit, so comments stay)."""
    out: list[str] = []
    in_build = False
    for line in text.splitlines(keepends=True):
        header = _TABLE_RE.match(line)
        if header:
            parts = [p.strip() for p in header.group(1).split(".")]
            in_build = parts[-1] == "build"
        elif in_build and _REPO_RE.match(line):
            continue
        out.append(line)
    stripped = "".join(out)
    try:
        doc = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError as exc:
        raise BayError(f"cannot remove [build] repo from bay.toml: {exc}") from None
    if bay_toml.build_repos(doc):
        raise BayError(
            "[build] repo is written in a form bay adopt cannot remove",
            hint="Write it as a `repo = \"...\"` line under [build], commit, then adopt.",
        )
    return stripped


def _targets(raw: dict[str, Any]) -> dict[str, set[str]]:
    """``{from: {adopted targets}}`` over every environment of the lock."""
    out: dict[str, set[str]] = {}
    for record in (raw.get("envs") or {}).values():
        files = ((record or {}).get("adopted") or {}).get("files") or {}
        for key, target in files.items():
            out.setdefault(str(key), set()).add(str(target))
    return out


def _shared_fleet_files(fleet_root: Path, name: str) -> set[str]:
    """``files/...`` paths another fleet entry reads, so they must stay.

    Another in-fleet project's ``fleet:`` mount or old place, another lock's
    adopted file, and a ``[resources.*]`` file list.
    """
    import tomllib

    from bay_cli.plan import _mount_sources

    out: set[str] = set()
    projects = fleet_root / PROJECTS_DIR
    for toml in sorted(projects.glob("*/bay.toml")) if projects.is_dir() else []:
        if toml.parent.name == name:
            continue
        try:
            doc = tomllib.loads(toml.read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        levels = [doc, *(v for v in (doc.get("services") or {}).values() if isinstance(v, dict))]
        for level in levels:
            for mount in level.get("mounts") or []:
                src = mount.get("from") if isinstance(mount, dict) else None
                if isinstance(src, str) and src.startswith(FLEET_PREFIX):
                    out.add(f"{FILES_DIR}/{src[len(FLEET_PREFIX):].strip('/')}")
        # Its own mounts may still read the old place files/<other>/<from>.
        out.update(f"{FILES_DIR}/{toml.parent.name}/{m}" for m in _mount_sources(doc))
    for lock in sorted(projects.glob(f"*/{LOCK_FILE}")) if projects.is_dir() else []:
        if lock.parent.name == name:
            continue
        try:
            raw = lockfile.read(lock) or {}
        except (OSError, ValueError):
            continue
        for targets in _targets(raw).values():
            out.update(f"{FILES_DIR}/{t}" for t in targets)
    try:
        fleet = tomllib.loads((fleet_root / "bay.fleet.toml").read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        fleet = {}
    for res in (fleet.get("resources") or {}).values():
        if isinstance(res, dict):
            out.update(f"{FILES_DIR}/{f}" for f in res.get("files") or [] if isinstance(f, str))
    return out


def _overlaps(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _join(directory: str, rel: str) -> str:
    return rel if directory in ("", ".") else f"{directory}/{rel}"


def _new_lock(
    raw: dict[str, Any], repo: str, toml_path: str, fleet_head: str, envs: list[str]
) -> dict[str, Any]:
    """The lock in repo form; ``commit`` fields are filled in once the app commit exists."""
    lock = copy.deepcopy(raw)
    lock["repo"] = repo
    lock["toml_path"] = toml_path
    records = lock.setdefault("envs", {})
    for env in sorted(set(records) | set(envs)):
        record = dict(records.get(env) or {})
        record.pop("previous", None)
        adopted = dict(record.get("adopted") or {})
        adopted["from_fleet_commit"] = fleet_head
        record["adopted"] = adopted
        records[env] = record
    return lock


def _with_commit(lock: dict[str, Any], commit: str) -> dict[str, Any]:
    """``lock`` with the project pin, and every environment that had a pin, at ``commit``.

    Every environment also records ``adopted.app_commit``: the adopt commit,
    which ``bay up`` accepts unpushed (:func:`is_adopt_commit`).
    """
    out = copy.deepcopy(lock)
    out["commit"] = commit
    for record in (out.get("envs") or {}).values():
        if record.get("commit"):
            record["commit"] = commit
        record.setdefault("adopted", {})["app_commit"] = commit
    return out


def is_adopt_commit(lock: dict[str, Any], env: str, commit: str | None) -> bool:
    """True when ``commit`` is the app commit ``bay adopt`` made for this project.

    ``bay up`` then takes it before it is pushed and moves no code for it.
    """
    record = ((lock.get("envs") or {}).get(env) or {}) if isinstance(lock, dict) else {}
    adopted = record.get("adopted") or {}
    app = adopted.get("app_commit")
    return bool(commit and app and adopted.get("from_fleet_commit") and str(app) == commit)


def _lock_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    a = json.dumps(before, indent=2).splitlines()
    b = json.dumps(after, indent=2).splitlines()
    return list(difflib.unified_diff(a, b, "bay.lock (before)", "bay.lock (after)", lineterm=""))


# ── applying ────────────────────────────────────────────────────────────────


def _apply(cx: Context, plan: AdoptPlan) -> dict[str, Any]:
    fleet_root, root = cx.fleet_root, plan.repo_root
    written: list[Path] = []
    for item in plan.copies:
        dest = root / item.dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        if item.text is not None:
            dest.write_text(item.text)
        else:
            shutil.copy2(fleet_root / item.source, dest)
        written.append(dest)
    try:
        app_commit = gitrepo.commit_paths(
            root, written, APP_MESSAGE.format(fleet=plan.fleet_name)
        )
    except gitrepo.GitError as exc:
        raise BayError(
            f"cannot commit the app repo: {exc}",
            hint=f"The copied files are in {root}, uncommitted. Remove them, fix the "
            "problem, then run bay adopt again.",
        ) from None

    lock_file = lockfile.lock_path(fleet_root, plan.name)
    old_bytes = lock_file.read_bytes() if lock_file.is_file() else None
    final = _with_commit(plan.lock_after, app_commit)
    lock_rel = f"{PROJECTS_DIR}/{plan.name}/{LOCK_FILE}"
    try:
        lockfile.write(lock_file, final)
        gitrepo.remove(fleet_root, plan.removals)
        gitrepo.add(fleet_root, [lock_rel])
        fleet_commit = gitrepo.commit_staged(
            fleet_root,
            [*plan.removals, lock_rel],
            FLEET_MESSAGE.format(name=plan.name, repo=plan.repo),
        )
    except (gitrepo.GitError, lockfile.LockWriteError) as exc:
        if old_bytes is not None:
            lock_file.write_bytes(old_bytes)
        raise BayError(
            f"the app commit {app_commit[:12]} is made, but the fleet commit failed: {exc}",
            hint="Check `git status` in the fleet. The app commit is local: undo it with "
            "`git reset --soft HEAD~1` and remove the copied files, or finish the fleet "
            "side by hand.",
        ) from None
    result = _result(plan, check=False)
    result.update(
        {
            "app_commit": app_commit,
            "fleet_commit": fleet_commit,
            "lock_diff": _lock_diff(plan.lock_before, final),
        }
    )
    return result


def _result(plan: AdoptPlan, *, check: bool) -> dict[str, Any]:
    env = plan.envs[0] if plan.envs else "production"
    shown = _with_commit(plan.lock_after, _PENDING) if check else plan.lock_after
    return {
        "check": check,
        "project": plan.name,
        "fleet": plan.fleet_name,
        "repo": plan.repo,
        "repo_root": str(plan.repo_root),
        "toml_path": plan.toml_path,
        "from_fleet_commit": plan.fleet_head,
        "files": [{"from": c.source, "to": c.dest} for c in plan.copies],
        "removed": list(plan.removals),
        "kept": list(plan.kept),
        "envs": list(plan.envs),
        "lock_diff": _lock_diff(plan.lock_before, shown),
        "app_commit": None,
        "fleet_commit": None,
        "next": [
            f"bay plan {env}",
            f"bay up {env}",
            "git push",
        ],
    }
