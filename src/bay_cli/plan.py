"""``bay plan``: WANTED against PINNED and RUNNING, steps with a risk, one verdict.

The three truths:

WANTED
    ``bay.toml`` at the project's current commit (``git show <commit>:bay.toml``
    in the checkout you stand in, else in the fleet's repo cache, see
    :mod:`bay_cli.reposource`). Uncommitted edits are not part of it.
PINNED
    ``projects/<name>/bay.lock`` in the fleet, and the services file the fleet
    last compiled from it.
RUNNING
    The box receipt (``docs/deploy-receipt.md``), only the part that names
    this project's containers.

"The fleet decides. Main suggests. The box reports."

How a plan is made:

1. Copy the fleet inputs to a temp dir. Every project with a pinned commit
   is read at that commit, the planned project at the WANTED commit. A
   project whose lock pins no commit yet (just after ``bay init``) is left
   out: nothing of it is deployed. A project with no repo lives in the fleet
   (``projects/<name>/``); it is read from the fleet repo at its pin, or at
   the fleet's HEAD when it has none. The files a bay.toml mounts are mapped
   to ``files/<target>`` in the copy. ``bay compile`` uses the same copy.
2. Compile that copy, so the whole fleet is compiled as ``bay up`` would.
3. Diff the result against the services file in the fleet, entry by entry.
   Each difference becomes one or more steps; the risk of a step is set by
   the data it touches (the risk table is in ``docs/plan.md``).
4. Read the box receipt (skip with ``--no-remote``).
5. Only with ``--remote``: run today's deploy in check mode with the plan-only
   switch against the compiled file. Each box renders the env files it would
   write into a scratch directory (never the live ones), hashes those, and
   hands back its prediction (one entry per container: action and reasons,
   ``env_file`` when the env file bytes would change). A predicted change the
   diff does not explain becomes a step with ``source: box``
   (:func:`box_steps`). Without it, the steps come from the diff alone and
   ``box_checked`` is false.

The plan JSON is ``schemas/plan.schema.json``. A plan is saved to
``<fleet>/plans/<plan_id>.json``. ``plan_id`` is the first 12 hex digits of
the SHA-256 of the plan body: the same inputs give the same id, so
``bay approve <id>`` followed by ``bay up`` finds the approval again.

No secret value is ever read into a plan: secrets are compared by name
(:mod:`bay_cli.secrets_check`), and compiled entries only hold
``{{ secrets.NAME }}`` references, which the plan never copies.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import tomllib
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bay_cli import gitrepo, layout, lockfile, reposource
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import (
    FILES_DIR,
    FLEET_FILE,
    FLEET_PREFIX,
    GENERATED_SERVICES,
    LOCK_FILE,
    LOCK_SUFFIX,
    PROJECTS_DIR,
)

PLAN_VERSION = 1
PLANS_DIR = "plans"
APPROVED_SUFFIX = ".approved"

VERDICT_EXIT = {"auto": 0, "approve": 10, "blocked": 20, "stale": 30}

UNPLANNED_NOTE = (
    "bay up also refreshes the shared proxy, the gateway and the update watcher on the "
    "boxes of {box_env}. Those changes come from the fleet, not from bay.toml, and are "
    "not listed as steps."
)

#: ``(cx, box_env) -> [{"env", "box", "receipt", "error"}]``, as receipts.fetch_receipts.
ReceiptReader = Callable[[Context, str], list[dict[str, Any]]]
#: ``(cx, box_env, compiled_services_file) -> [{"box", "report", "error"}]``,
#: one entry per box of the env; ``report`` is the plan-only JSON the box
#: printed (``python -m bay_reconcile --plan-only``). Raises on failure.
BoxCheck = Callable[[Context, str, Path], list[dict[str, Any]] | None]


# ── Project ─────────────────────────────────────────────────────────────────


@dataclass
class ProjectRef:
    """One project of one fleet, as plan, up and show see it."""

    cx: Context
    name: str
    fleet: dict[str, Any]
    lock_file: Path
    lock: dict[str, Any]
    #: The git repo WANTED is read from: the fleet root for a project in the
    #: fleet, else the checkout you stand in or the fleet's repo cache.
    checkout: Path
    toml_path: str
    #: True for a project whose bay.toml is projects/<name>/bay.toml in the
    #: fleet; ``checkout`` is then the fleet root.
    in_fleet: bool = False
    #: ``fleet``, ``checkout`` or ``cache``: where ``checkout`` came from.
    source: str = "fleet"
    #: The directory the command ran in, when it was given (a matching
    #: checkout there is read before the cache).
    cwd: Path | None = None
    #: Lines for the reader: a lock move, a cache that could not be fetched.
    notes: list[str] = field(default_factory=list)

    @property
    def primary_env(self) -> str:
        return str(self.fleet.get("primary_env", "production"))

    @property
    def repo(self) -> str | None:
        repo = self.lock.get("repo")
        return str(repo) if repo else None

    @property
    def scope(self) -> str:
        """The fleet folder of the project: ``projects/<name>``."""
        return f"{PROJECTS_DIR}/{self.name}"

    @property
    def scope_spec(self) -> list[str]:
        """Pathspecs whose last commit is an in-fleet project's commit.

        The folder minus its ``bay.lock``: ``bay up`` commits the lock, and
        that commit must not change the project's WANTED commit.
        """
        return scope_spec(self.name)


def scope_spec(name: str) -> list[str]:
    folder = f"{PROJECTS_DIR}/{name}"
    return [folder, f":(exclude){folder}/{LOCK_FILE}"]


def load_fleet_doc(cx: Context) -> dict[str, Any]:
    from bay_cli.fleet import FleetError, load_fleet_file

    try:
        return load_fleet_file(cx.fleet_root)
    except FleetError as exc:
        raise BayError(
            "the fleet file is not valid:\n  " + "\n  ".join(exc.lines),
            code=ErrorCode.CONFIG_ERROR,
        ) from None


def load_project(
    cx: Context,
    name: str,
    *,
    expect_fleet: str | None = None,
    cwd: Path | None = None,
    fetch: bool = True,
) -> ProjectRef:
    """The project ``name`` of the fleet at ``cx``.

    A project with ``projects/<name>/bay.toml`` lives in the fleet. Any other
    project is read from its ``repo``: the checkout at ``cwd`` when its
    origin is that repo, else the fleet's repo cache (cloned when missing,
    fetched first with ``fetch``). The flat locks of a format 1 fleet are
    moved into project folders first (:func:`bay_cli.layout.ensure`).
    """
    moved = layout.ensure(cx.fleet_root)
    fleet_doc = load_fleet_doc(cx)
    if expect_fleet is not None and fleet_doc.get("name") != expect_fleet:
        raise BayError(
            f"bay.toml names fleet {expect_fleet}, but {cx.fleet_root} is fleet "
            f"{fleet_doc.get('name')}",
            hint="Pass --fleet with the right fleet directory.",
        )
    lock_file = lockfile.lock_path(cx.fleet_root, name)
    lock_rel = f"{PROJECTS_DIR}/{name}/{LOCK_FILE}"
    try:
        raw = lockfile.read(lock_file)
    except ValueError as exc:
        raise BayError(f"{lock_rel} is not valid JSON: {exc}") from None
    in_dir = cx.fleet_root / PROJECTS_DIR / name / "bay.toml"
    if raw is None:
        if not in_dir.is_file():
            raise BayError(
                f"project {name} is not in fleet {fleet_doc.get('name')}",
                code=ErrorCode.NOT_FOUND,
                hint="Run `bay init` in the app repo first.",
            )
        raw = lockfile.new_lock(name, repo=None)
    base = {
        "cx": cx,
        "name": name,
        "fleet": fleet_doc,
        "lock_file": lock_file,
        "lock": raw,
        "cwd": cwd,
        "notes": [f"fleet layout: {line}" for line in moved],
    }
    if in_dir.is_file():
        return ProjectRef(
            **base,
            checkout=cx.fleet_root,
            toml_path=f"{PROJECTS_DIR}/{name}/bay.toml",
            in_fleet=True,
            source="fleet",
        )
    repo = raw.get("repo")
    if not repo:
        raise BayError(
            f"{lock_rel} names no repo, and there is no {PROJECTS_DIR}/{name}/bay.toml "
            "in the fleet",
            hint="Set repo in the lock (bay init does), or move the bay.toml into the fleet.",
        )
    toml_path = str(raw.get("toml_path", "bay.toml"))
    checkout = reposource.checkout_for(cwd, str(repo))
    if checkout is not None:
        return ProjectRef(**base, checkout=checkout, toml_path=toml_path, source="checkout")
    cache, problem = reposource.ensure_cache(cx.fleet_root, str(repo), fetch=fetch)
    if cache is None:
        raise BayError(
            f"project {name}: {problem}",
            hint="Check the repo URL in the lock and your access to it, or run the "
            "command inside a checkout of the repo.",
        )
    ref = ProjectRef(**base, checkout=cache, toml_path=toml_path, source="cache")
    if problem:
        ref.notes.append(f"{problem}; the plan reads what the cache already holds")
    return ref


def project_containers(
    name: str, doc: Mapping[str, Any], lock_raw: Mapping[str, Any], primary_env: str
) -> dict[str, dict[str, str]]:
    """``{env: {service: container name}}`` for a project.

    Mirrors ``compiler._Compiler._container_name``: the lockfile's adopted
    name wins, else ``<name>`` / ``<name>-<env>`` plus ``-<service>``.
    """
    out: dict[str, dict[str, str]] = {}
    services = doc.get("services") or {}
    for env in doc.get("deploy") or {}:
        adopted = (lock_raw.get("envs") or {}).get(env, {}).get("adopted", {}).get("containers", {})
        base = name if env == primary_env else f"{name}-{env}"
        names: dict[str, str] = {}
        for service in ["web", *sorted(services)]:
            names[service] = adopted.get(service) or (
                base if service == "web" else f"{base}-{service}"
            )
        out[str(env)] = names
    return out


def resolve_box(
    proj: ProjectRef, env: str, doc: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    """``(box, box_env)``: the lock's box wins, then bay.toml, then the fleet default.

    ``box_env`` is the box's ``env`` in bay.fleet.toml: the group a deploy targets.
    The compiler uses the same order, so an edit of ``deploy.<env>.box`` never
    moves a workload by itself: the plan shows it as a ``move`` step
    (:func:`move_record`) and ``bay up`` writes the new box into the lock.
    """
    record = (proj.lock.get("envs") or {}).get(env, {})
    deploy = ((doc or {}).get("deploy") or {}).get(env, {})
    box = record.get("box") or deploy.get("box") or proj.fleet.get("default_box")
    return box_and_env(proj.fleet, box)


def box_and_env(fleet: Mapping[str, Any], box: Any) -> tuple[str | None, str | None]:
    """``(box, box_env)``; ``box_env`` is None when the fleet has no such box."""
    boxes = fleet.get("boxes") or {}
    if not box or box not in boxes:
        return (str(box) if box else None), None
    return str(box), str(boxes[box]["env"])


def wanted_box(fleet: Mapping[str, Any], doc: Mapping[str, Any] | None, env: str) -> str | None:
    """WANTED box: ``deploy.<env>.box`` in bay.toml, else the fleet's default box."""
    deploy = ((doc or {}).get("deploy") or {}).get(env) or {}
    box = deploy.get("box") or fleet.get("default_box")
    return str(box) if box else None


def pinned_box(lock: Mapping[str, Any], env: str) -> str | None:
    """PINNED box: the box the lock records for ``env``, or None before the first ``bay up``."""
    box = ((lock.get("envs") or {}).get(env) or {}).get("box")
    return str(box) if box else None


# ── Reading WANTED ──────────────────────────────────────────────────────────


@dataclass
class Wanted:
    commit: str | None
    toml: bytes | None
    doc: dict[str, Any] | None
    dirty: bool | None
    problems: list[str] = field(default_factory=list)

    @property
    def toml_sha256(self) -> str | None:
        return hashlib.sha256(self.toml).hexdigest() if self.toml is not None else None


def read_wanted(proj: ProjectRef, at: str | None) -> Wanted:
    checkout = proj.checkout
    if not gitrepo.is_repo(checkout):
        return Wanted(None, None, None, None, [f"the checkout {checkout} is not a git repo"])
    if proj.in_fleet:
        dirty = gitrepo.path_dirty(checkout, proj.scope_spec)
    elif proj.source == "cache":
        dirty = None  # a mirror has no work tree
    else:
        dirty = gitrepo.dirty(checkout)
    ref = at or "HEAD"
    commit = gitrepo.resolve_commit(checkout, ref)
    if commit is None:
        what = f"commit {at}" if at else "HEAD"
        return Wanted(None, None, None, dirty, [f"{what} is not reachable in {checkout}"])
    if proj.in_fleet:
        # WANTED is the fleet's committed file; its commit is the last one that
        # touched projects/<name>/ (the bay.toml or a file it mounts), never
        # counting projects/<name>/bay.lock, which bay up commits itself.
        changed = gitrepo.last_change(checkout, proj.scope_spec, commit)
        if changed is None:
            return Wanted(
                None,
                None,
                None,
                dirty,
                [f"{proj.scope} is not committed in the fleet; commit it first"],
            )
        commit = changed
    data = gitrepo.show_file(checkout, commit, proj.toml_path)
    if data is None:
        return Wanted(
            commit,
            None,
            None,
            dirty,
            [f"{proj.toml_path} is not committed at {commit[:12]}; commit it first"],
        )
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return Wanted(commit, data, None, dirty, [f"{proj.toml_path} at {commit[:12]}: {exc}"])
    return Wanted(commit, data, doc, dirty)


def doc_at(proj: ProjectRef, commit: str | None) -> dict[str, Any] | None:
    if not commit:
        return None
    repo, full = proj.checkout, gitrepo.resolve_commit(proj.checkout, commit)
    if full is None and not proj.in_fleet and proj.repo:
        found = reposource.find_commit(proj.cx.fleet_root, proj.repo, commit, cwd=proj.cwd)
        repo, full = found.path or repo, found.commit
    data = gitrepo.show_file(repo, full, proj.toml_path) if full else None
    if data is None:
        return None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def commit_on_remote(proj: ProjectRef, commit: str) -> bool | None:
    """Is ``commit`` of a repo project on a branch of its remote? True for a fleet project.

    Read in the checkout (its remote-tracking branches) and in the fleet's
    repo cache (fetched before the plan). None when neither can tell.
    """
    if proj.in_fleet or not proj.repo:
        return True
    answers: list[bool | None] = []
    if proj.source == "checkout":
        answer = gitrepo.on_remote(proj.checkout, commit)
        if answer:
            return True
        answers.append(answer)
    # The cache was fetched when the project was loaded from it; a checkout
    # may know an older remote, so the cache is fetched then.
    cache, problem = reposource.ensure_cache(
        proj.cx.fleet_root, proj.repo, fetch=proj.source != "cache"
    )
    if cache is not None and gitrepo.resolve_commit(cache, commit) is not None:
        answers.append(gitrepo.on_remote(cache, commit))
    elif cache is not None and problem is None:
        answers.append(False)
    if any(a is True for a in answers):
        return True
    if any(a is False for a in answers):
        return False
    return None


# ── Compiling a copy of the fleet ───────────────────────────────────────────


@dataclass
class Compiled:
    result: Any  # compiler.CompileResult | None
    errors: list[str]
    workdir: Path
    notes: list[str] = field(default_factory=list)
    #: ``{project: commit}`` for every project the compile read: a repo
    #: project's pinned commit, or for a project in the fleet the last fleet
    #: commit that touched ``projects/<name>/`` at or before the commit read
    #: (the same form as its WANTED commit). ``bay up`` pins these.
    commits: dict[str, str] = field(default_factory=dict)
    #: Repo projects left out because their lock pins no commit yet.
    unpinned: list[str] = field(default_factory=list)
    #: Uncommitted fleet files that are not deployed (see :func:`uncommitted_notes`).
    uncommitted: list[str] = field(default_factory=list)
    #: ``{container: project}`` for every container a project of the compile owns.
    owners: dict[str, str] = field(default_factory=dict)

    @property
    def files_root(self) -> Path:
        """``files/`` of the scratch fleet: what the deploy copies config files from.

        The fleet's ``files/`` at its commit plus every file a bay.toml mounts
        from beside it (:func:`_map_files`). Passed to the deploy as
        ``bay_config_files_root``. It lives as long as this compile.
        """
        path = self.workdir / "fleet" / FILES_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    def services_file(self) -> Path | None:
        if self.result is None:
            return None
        path = self.workdir / "compiled-services.yml"
        if not path.exists():
            path.write_text(self.result.text())
        return path


def _mount_sources(doc: Mapping[str, Any]) -> list[str]:
    """Every ``from`` of the file's mounts, without the ``fleet:`` ones."""
    levels = [doc, *(v for v in (doc.get("services") or {}).values() if isinstance(v, dict))]
    out: list[str] = []
    for level in levels:
        for mount in level.get("mounts") or []:
            if isinstance(mount, dict) and isinstance(mount.get("from"), str):
                src = mount["from"].rstrip("/")
                if not src.startswith(FLEET_PREFIX):
                    out.append(src)
    return list(dict.fromkeys(out))


def _join(directory: str, rel: str) -> str:
    return rel if directory in ("", ".") else f"{directory}/{rel}"


@dataclass
class _Copy:
    root: Path
    problems: list[str]
    notes: list[str]
    labels: dict[str, str]
    commits: dict[str, str] = field(default_factory=dict)
    unpinned: list[str] = field(default_factory=list)
    #: Repo project -> the directory its files were extracted into.
    checkouts: dict[str, Path] = field(default_factory=dict)


def _materialize(
    cx: Context, tmp: Path, pins: Mapping[str, str], *, cwd: Path | None = None
) -> _Copy:
    """Copy the fleet inputs to ``tmp/fleet`` with every project read at its pin.

    * A repo project (no ``projects/<name>/bay.toml``) is read at its lock
      ``commit`` (or its pin in ``pins``) from the checkout at ``cwd`` when
      its origin is the lock's repo, else from the fleet's repo cache
      (:mod:`bay_cli.reposource`). Its bay.toml and the files it mounts land
      in ``tmp/checkouts/<name>``. With no commit yet it is left out, with a
      note. A commit found in neither place is a problem, never a skip.
    * A project in the fleet (``projects/<name>/``) is read from the fleet
      repo at its lock ``commit``; with no lock or no commit, at the fleet's
      HEAD, with a note. The working tree is never read. Its lock is the one
      in the working tree (the pin, not the pinned commit's copy).
    * The fleet's ``files/`` tree (rig files, resource files, shared files)
      is read at the fleet's HEAD.
    * Every file a project mounts from beside its bay.toml is copied to
      ``files/<target>`` in the copy, where ``<target>`` is the adopted path
      from the lock or ``<name>/<from>``: the place the deploy reads.

    Every path the compiler and ``fleet.load_inputs`` read from the fleet root
    is copied here: ``bay.fleet.toml``, ``group_vars/all/*.y*ml``,
    ``projects/`` (``bay.toml``, its files and ``bay.lock``) and ``files/``.
    """
    root = tmp / "fleet"
    root.mkdir(parents=True)
    out = _Copy(root=root, problems=[], notes=[], labels={})
    src = cx.fleet_root
    if (src / FLEET_FILE).is_file():
        shutil.copy2(src / FLEET_FILE, root / FLEET_FILE)
    projects_src = src / PROJECTS_DIR
    projects_dst = root / PROJECTS_DIR
    projects_dst.mkdir()
    all_src = cx.env_dir("all")
    all_dst = root / "group_vars" / "all"
    all_dst.mkdir(parents=True)
    generated = (src / GENERATED_SERVICES).resolve()
    for path in (
        sorted([*all_src.glob("*.yml"), *all_src.glob("*.yaml")]) if all_src.is_dir() else []
    ):
        if path.resolve() == generated or path.name.startswith(("secrets", "vault")):
            continue
        shutil.copy2(path, all_dst / path.name)

    locks: dict[str, dict[str, Any]] = {}
    for lock in sorted(projects_src.glob(f"*/{LOCK_FILE}")) if projects_src.is_dir() else []:
        try:
            raw = lockfile.read(lock)
        except (OSError, ValueError) as exc:
            out.problems.append(f"{PROJECTS_DIR}/{lock.parent.name}/{LOCK_FILE}: {exc}")
            continue
        if raw is not None:
            locks[lock.parent.name] = raw

    fleet_head = gitrepo.head(src)
    _copy_files_tree(src, fleet_head, out)
    in_fleet: set[str] = set()
    for child in sorted(projects_src.iterdir()) if projects_src.is_dir() else []:
        if child.is_dir() and (child / "bay.toml").is_file():
            in_fleet.add(child.name)
            _copy_in_fleet(src, fleet_head, child.name, pins, locks.get(child.name), out)

    for stem, raw in locks.items():
        if stem in in_fleet:
            continue
        _copy_repo_project(cx, tmp, stem, raw, pins, out, cwd=cwd)
    return out


def _write_lock(out: _Copy, name: str, raw: Mapping[str, Any]) -> None:
    path = out.root / PROJECTS_DIR / name / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, indent=2) + "\n")


def _copy_repo_project(
    cx: Context,
    tmp: Path,
    stem: str,
    raw: Mapping[str, Any],
    pins: Mapping[str, str],
    out: _Copy,
    *,
    cwd: Path | None,
) -> None:
    """Extract a repo project at its pin into ``tmp/checkouts/<name>``."""
    name = str(raw.get("name") or stem)
    commit = pins.get(name) or raw.get("commit")
    if not commit:
        out.notes.append(f"{name} has no pinned commit yet, so it is left out")
        out.unpinned.append(name)
        return
    repo = raw.get("repo")
    if not repo:
        out.problems.append(
            f"{name}: {PROJECTS_DIR}/{name}/{LOCK_FILE} names no repo, and there is no "
            f"{PROJECTS_DIR}/{name}/bay.toml in the fleet"
        )
        return
    found = reposource.find_commit(cx.fleet_root, str(repo), str(commit), cwd=cwd)
    if found.path is None or found.commit is None:
        out.problems.append(f"{name}: {found.problem}; push it, or fix the lock")
        return
    checkout, full = found.path, found.commit
    toml_rel = str(raw.get("toml_path", "bay.toml"))
    data = gitrepo.show_file(checkout, full, toml_rel)
    if data is None:
        out.problems.append(f"{name}: {toml_rel} is not in commit {full[:12]}")
        return
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        doc = {}
    toml_dir = str(Path(toml_rel).parent)
    rels = [toml_rel, *(_join(toml_dir, m) for m in _mount_sources(doc))]
    rels = [r for r in dict.fromkeys(rels) if gitrepo.has_path(checkout, full, r)]
    dest = tmp / "checkouts" / name
    try:
        gitrepo.extract(checkout, full, rels, dest)
    except gitrepo.GitError as exc:
        out.problems.append(f"{name}: cannot read commit {full[:12]}: {exc}")
        return
    out.labels[str(dest)] = f"{name}@{full[:12]}"
    pinned = copy.deepcopy(dict(raw))
    pinned["commit"] = full
    _write_lock(out, stem, pinned)
    out.checkouts[name] = dest
    out.commits[name] = full
    _map_files(name, doc, (dest / toml_rel).parent, pinned, out)


def _map_files(
    name: str, doc: Mapping[str, Any], toml_dir: Path, lock: Mapping[str, Any] | None, out: _Copy
) -> None:
    """Copy each file mounted from beside the bay.toml to ``files/<target>`` in the copy.

    ``<target>`` is the adopted path of every env in the lock, else
    ``<name>/<from>``. A ``from`` that is not beside the toml is left alone:
    the compiler then reads the old place ``files/<target>``.
    """
    adopted: dict[str, set[str]] = {}
    for record in ((lock or {}).get("envs") or {}).values():
        files = ((record or {}).get("adopted") or {}).get("files") or {}
        for key, target in files.items():
            adopted.setdefault(str(key), set()).add(str(target))
    for rel in _mount_sources(doc):
        source = toml_dir / rel
        if not source.exists() or ".." in Path(rel).parts:
            continue
        for target in sorted(adopted.get(rel) or {f"{name}/{rel}"}):
            dest = out.root / FILES_DIR / target
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            elif dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, symlinks=True)
            else:
                shutil.copy2(source, dest)


def _copy_files_tree(src: Path, fleet_head: str | None, out: _Copy) -> None:
    """Copy the fleet's ``files/`` tree: at its HEAD, or as it is when not a git repo."""
    if fleet_head is None:
        if (src / FILES_DIR).is_dir():
            shutil.copytree(src / FILES_DIR, out.root / FILES_DIR, symlinks=True)
        return
    if not gitrepo.has_path(src, fleet_head, FILES_DIR):
        return
    try:
        gitrepo.extract(src, fleet_head, [FILES_DIR], out.root)
    except gitrepo.GitError as exc:
        out.problems.append(f"{FILES_DIR}: cannot read fleet commit {fleet_head[:12]}: {exc}")


def _copy_in_fleet(
    src: Path,
    fleet_head: str | None,
    name: str,
    pins: Mapping[str, str],
    lock: Mapping[str, Any] | None,
    out: _Copy,
) -> None:
    """Copy ``projects/<name>/`` as the fleet repo holds it at the project's pin."""
    scope = f"{PROJECTS_DIR}/{name}"
    if fleet_head is None:
        out.notes.append(f"{name}: the fleet is not a git repo, so its files are read as they are")
        shutil.copytree(src / scope, out.root / scope, symlinks=True)
        _map_in_fleet(name, lock, out)
        return
    commit = pins.get(name) or (lock or {}).get("commit")
    if not commit:
        commit = fleet_head
        out.notes.append(
            f"{name} lives in the fleet and has no pinned commit; read at the fleet's HEAD"
        )
    full = gitrepo.resolve_commit(src, str(commit))
    if full is None:
        out.problems.append(f"{name}: commit {str(commit)[:12]} is not in the fleet repo")
        return
    if not gitrepo.has_path(src, full, f"{scope}/bay.toml"):
        if (lock or {}).get("commit") or name in pins:
            out.problems.append(f"{name}: {scope}/bay.toml is not in fleet commit {full[:12]}")
        else:
            out.notes.append(f"{name}: {scope}/bay.toml is not committed, so it is left out")
        return
    try:
        gitrepo.extract(src, full, [scope], out.root)
    except gitrepo.GitError as exc:
        out.problems.append(f"{name}: cannot read fleet commit {full[:12]}: {exc}")
        return
    # The lock is the pin as it is now, not the copy the pinned commit held.
    stale = out.root / scope / LOCK_FILE
    if stale.exists():
        stale.unlink()
    if lock is not None:
        _write_lock(out, name, lock)
    out.commits[name] = gitrepo.last_change(src, scope_spec(name), full) or full
    _map_in_fleet(name, lock, out)


def _map_in_fleet(name: str, lock: Mapping[str, Any] | None, out: _Copy) -> None:
    toml = out.root / PROJECTS_DIR / name / "bay.toml"
    try:
        doc = tomllib.loads(toml.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return  # fleet.load_inputs reports it
    _map_files(name, doc, toml.parent, lock, out)


def uncommitted_notes(fleet_root: Path) -> list[str]:
    """One note per uncommitted fleet file that a mount could read. Never a blocker.

    The deploy copies config files from the scratch copy at the fleet's
    commit (``bay_config_files_root``), so an untracked or changed file under
    ``files/`` or in a project folder (its ``bay.toml`` and ``bay.lock`` aside:
    the plan reads those on its own) is not deployed.
    """
    specs = [
        FILES_DIR,
        PROJECTS_DIR,
        f":(exclude){PROJECTS_DIR}/*/{LOCK_FILE}",
        f":(exclude){PROJECTS_DIR}/*/bay.toml",
        f":(exclude){PROJECTS_DIR}/*{LOCK_SUFFIX}",
    ]
    return [
        f"uncommitted file {rel} is not deployed; commit it first"
        for rel in gitrepo.uncommitted_paths(fleet_root, specs)
    ]


def _override_boxes(root: Path, boxes: Mapping[str, Mapping[str, str]]) -> None:
    """Set the box of some ``{project: {env: box}}`` in the scratch locks.

    A plan with a ``move`` step compiles the project on its WANTED box; the
    fleet's own lock is never touched.
    """
    for name, envs in boxes.items():
        path = root / PROJECTS_DIR / name / LOCK_FILE
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for env, box in envs.items():
            raw.setdefault("envs", {}).setdefault(env, {})["box"] = box
        path.write_text(json.dumps(raw, indent=2) + "\n")


def _drop_projects(made: _Copy, drop: Mapping[str, Collection[str] | None]) -> None:
    """Leave projects, or some envs of them, out of the scratch copy (``bay remove``).

    ``{project: None}`` drops the whole project folder; ``{project: {env, ...}}``
    drops those env records from its scratch lock (the bay.toml read must
    have no ``[deploy.<env>]`` left, or the compile keeps the env). The
    fleet's own files are never touched.
    """
    for name, envs in drop.items():
        folder = made.root / PROJECTS_DIR / name
        if envs is None:
            shutil.rmtree(folder, ignore_errors=True)
            made.checkouts.pop(name, None)
            made.commits.pop(name, None)
            if name in made.unpinned:
                made.unpinned.remove(name)
            continue
        path = folder / LOCK_FILE
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for env in envs:
            (raw.get("envs") or {}).pop(env, None)
        path.write_text(json.dumps(raw, indent=2) + "\n")


def _owners(inputs: Any) -> dict[str, str]:
    """``{container: project}`` for every project the compile read."""
    primary = str(inputs.fleet.get("primary_env", "production"))
    out: dict[str, str] = {}
    for name, project in sorted(inputs.projects.items()):
        raw = {
            "envs": {
                env: {"adopted": {"containers": dict(project.lock_env(env).containers)}}
                for env in project.doc.get("deploy") or {}
            }
        }
        for names in project_containers(name, project.doc, raw, primary).values():
            for container in names.values():
                out.setdefault(container, name)
    return out


@contextmanager
def compiled_fleet(
    cx: Context,
    pins: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    boxes: Mapping[str, Mapping[str, str]] | None = None,
    drop: Mapping[str, Collection[str] | None] | None = None,
) -> Iterator[Compiled]:
    """Compile the fleet with every project at its pin (``pins`` overrides some).

    ``cwd`` is where the command ran: a repo project whose checkout is there
    is read from it, the others from the fleet's repo cache. ``boxes``
    (``{project: {env: box}}``) places a moving project on its new box.
    ``drop`` leaves projects or some of their envs out (:func:`_drop_projects`).
    """
    from bay_cli import compiler
    from bay_cli.fleet import FleetError, load_inputs

    moved = layout.ensure(cx.fleet_root)
    with tempfile.TemporaryDirectory(prefix="bay-plan-") as tmp:
        work = Path(tmp)
        made = _materialize(cx, work, pins or {}, cwd=cwd)
        if boxes:
            _override_boxes(made.root, boxes)
        if drop:
            _drop_projects(made, drop)
        errors = list(made.problems)
        notes = [f"fleet layout: {line}" for line in moved] + list(made.notes)
        result = None
        owners: dict[str, str] = {}
        if not errors:
            try:
                inputs = load_inputs(made.root, checkouts=made.checkouts)
                result = compiler.compile_fleet(inputs)
                owners = _owners(inputs)
            except (FleetError, compiler.CompileError) as exc:
                errors.extend(exc.lines)
        if result is not None:
            notes.extend(result.notes)
        cleaned = []
        for line in errors:
            for path, label in made.labels.items():
                line = line.replace(path, label)
            cleaned.append(line.replace(str(made.root) + "/", ""))
        yield Compiled(
            result=result,
            errors=cleaned,
            workdir=work,
            notes=notes,
            commits=dict(made.commits),
            unpinned=list(made.unpinned),
            uncommitted=uncommitted_notes(cx.fleet_root) if gitrepo.head(cx.fleet_root) else [],
            owners=owners,
        )


# ── PINNED on disk ──────────────────────────────────────────────────────────


def current_services(cx: Context) -> tuple[dict[str, Any], str]:
    """The fleet's services file and its state.

    State: ``missing`` (no file), ``generated`` (bay wrote it, hash matches),
    ``edited`` (bay wrote it, then a hand edit), ``foreign`` (bay never wrote it).
    """
    import yaml

    from bay_cli import compiler

    path = cx.fleet_root / GENERATED_SERVICES
    if not path.is_file():
        return {}, "missing"
    text = path.read_text()
    recorded, body = compiler.split_header(text)
    data = yaml.safe_load(body) or {}
    if not isinstance(data, dict):
        data = {}
    if recorded is None:
        return data, "foreign"
    if recorded != compiler.body_digest(body):
        return data, "edited"
    return data, "generated"


# ── Steps ───────────────────────────────────────────────────────────────────


def _entries(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for section in ("accessories", "services"):
        block = data.get(section) or {}
        if isinstance(block, Mapping):
            for key, value in block.items():
                if isinstance(value, Mapping):
                    out[str(key)] = dict(value)
    return out


def _named_volumes(entry: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in entry.get("volumes") or []:
        if not isinstance(spec, str):
            continue
        src, _, rest = spec.partition(":")
        if not src or src.startswith(("/", ".", "{", "~")):
            continue
        out[src] = rest
    return out


def _secret_names(name: str, entry: Mapping[str, Any]) -> set[str]:
    env = entry.get("env")
    secret = env.get("secret") if isinstance(env, Mapping) else None
    prefix = name.upper().replace("-", "_")
    if isinstance(secret, list):
        return {f"{prefix}_{k}" for k in secret}
    if isinstance(secret, Mapping):
        return {str(v) for v in secret.values()}
    return set()


def _step(
    kind: str,
    action: str,
    risk: str,
    reason: str,
    *,
    container: str | None = None,
    resource: str | None = None,
    project: str | None = None,
    source: str = "compile",
) -> dict[str, Any]:
    return {
        "id": "",
        "kind": kind,
        "container": container,
        "resource": resource,
        "project": project,
        "action": action,
        "risk": risk,
        "reason": reason,
        "source": source,
    }


def _changed_keys(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


#: Build keys that only the build side reads (the hold guard, docs/plan.md).
#: The container hash leaves the whole ``build`` table out, so a change of
#: these alone recreates nothing; ``bay up`` still writes them to the box.
_HOLD_KEYS = ("bay_toml_hash", "bay_toml_path", "bay_toml_files")


def _hold_only(old: Mapping[str, Any], new: Mapping[str, Any]) -> bool:
    """True when two entries differ only in the hold-guard keys of ``build``.

    That is the case right after ``bay adopt``: the bay.toml moved into the
    app repo, so the build gains the hash of the file, and nothing else
    changes.
    """

    def strip(entry: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(entry)
        build = out.get("build")
        if isinstance(build, Mapping):
            out["build"] = {k: v for k, v in build.items() if k not in _HOLD_KEYS}
        return out

    return strip(old) == strip(new)


def _container_steps(
    name: str,
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    *,
    project: str | None,
    running: set[str] | None,
) -> list[dict[str, Any]]:
    if old is None and new is not None:
        return [
            _step("container", "create", "safe", "new container", container=name, project=project)
        ]
    if new is None:
        return [
            _step(
                "container",
                "remove",
                "destructive",
                "container removed; its volumes stay on the box",
                container=name,
                project=project,
            )
        ]
    assert old is not None
    steps = [
        _step(
            "container",
            "update",
            "safe",
            "changed: " + ", ".join(_changed_keys(old, new)),
            container=name,
            project=project,
        )
    ]

    old_vols, new_vols = _named_volumes(old), _named_volumes(new)
    gone = sorted(set(old_vols) - set(new_vols))
    added = sorted(set(new_vols) - set(old_vols))
    if gone and len(gone) == len(added):
        for before, after in zip(gone, added, strict=True):
            steps.append(
                _step(
                    "volume",
                    "rename",
                    "destructive",
                    f"volume {before} becomes {after}; a rename is a delete in disguise: "
                    f"the data stays in {before} and {after} starts empty",
                    container=name,
                    resource=before,
                    project=project,
                )
            )
    else:
        for vol in gone:
            steps.append(
                _step(
                    "volume",
                    "remove",
                    "destructive",
                    f"volume {vol} is no longer mounted; its data is left behind",
                    container=name,
                    resource=vol,
                    project=project,
                )
            )
        for vol in added:
            steps.append(
                _step(
                    "volume",
                    "create",
                    "safe",
                    f"new volume {vol}",
                    container=name,
                    resource=vol,
                    project=project,
                )
            )
    for vol in sorted(set(old_vols) & set(new_vols)):
        if old_vols[vol] != new_vols[vol]:
            steps.append(
                _step(
                    "volume",
                    "update",
                    "safe",
                    f"volume {vol} is mounted at a new path; the data stays",
                    container=name,
                    resource=vol,
                    project=project,
                )
            )

    old_db = old.get("database") if isinstance(old.get("database"), Mapping) else None
    new_db = new.get("database") if isinstance(new.get("database"), Mapping) else None
    if old_db and not new_db:
        steps.append(
            _step(
                "database",
                "remove",
                "destructive",
                f"the container loses database {old_db.get('name')}; the data stays in it",
                container=name,
                resource=str(old_db.get("name")),
                project=project,
            )
        )
    elif new_db and not old_db:
        steps.append(
            _step(
                "database",
                "create",
                "safe",
                f"new database {new_db.get('name')} on {new_db.get('accessory')}",
                container=name,
                resource=str(new_db.get("name")),
                project=project,
            )
        )
    elif old_db and new_db:
        if old_db.get("accessory") != new_db.get("accessory"):
            steps.append(
                _step(
                    "database",
                    "move",
                    "destructive",
                    f"database {old_db.get('name')} moves from {old_db.get('accessory')} to "
                    f"{new_db.get('accessory')}; the data does not move with it",
                    container=name,
                    resource=str(old_db.get("name")),
                    project=project,
                )
            )
        if old_db.get("name") != new_db.get("name"):
            steps.append(
                _step(
                    "database",
                    "rename",
                    "destructive",
                    f"database {old_db.get('name')} becomes {new_db.get('name')}; the new one "
                    "starts empty",
                    container=name,
                    resource=str(old_db.get("name")),
                    project=project,
                )
            )
        if old_db.get("user") != new_db.get("user"):
            steps.append(
                _step(
                    "database_user",
                    "rename",
                    "destructive",
                    f"database user {old_db.get('user')} becomes {new_db.get('user')}",
                    container=name,
                    resource=str(old_db.get("user")),
                    project=project,
                )
            )

    for secret in sorted(_secret_names(name, old) - _secret_names(name, new)):
        in_use = running is None or name in running
        steps.append(
            _step(
                "secret",
                "remove",
                "destructive" if in_use else "safe",
                f"secret {secret} is no longer given to the container"
                + (" while it runs" if running is not None and in_use else "")
                + (" (the box was not read, so it may be running)" if running is None else ""),
                container=name,
                resource=secret,
                project=project,
            )
        )
    return steps


def diff_steps(
    current: Mapping[str, Any],
    wanted: Mapping[str, Any],
    *,
    project: str | None,
    mine: set[str],
    resources: set[str],
    running: set[str] | None,
    owners: Mapping[str, str] | None = None,
    running_scope: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Steps that turn the fleet's services file into the wanted one.

    ``mine`` are the containers of ``project``. A container in ``owners``
    (``{container: project}``) belongs to that project: its steps carry that
    project and keep their own risk, so a safe change of another project is
    safe here too. ``running`` names the containers that run, and is only
    known for the names in ``running_scope`` (the planned projects); for any
    other container it counts as not read. Only a container that no project
    owns is ``shared``: a change to it cannot be traced to one bay.toml.
    """
    owners = owners or {}
    old_all, new_all = _entries(current), _entries(wanted)
    steps: list[dict[str, Any]] = []
    for name in sorted(set(old_all) | set(new_all)):
        old, new = old_all.get(name), new_all.get(name)
        if old == new or (old is not None and new is not None and _hold_only(old, new)):
            continue
        if name in resources:
            if new is None:
                steps.append(
                    _step(
                        "resource",
                        "remove",
                        "destructive",
                        f"shared resource {name} removed",
                        resource=name,
                    )
                )
            else:
                action = "create" if old is None else "update"
                what = (
                    "new" if old is None else "changed: " + ", ".join(_changed_keys(old or {}, new))
                )
                steps.append(
                    _step(
                        "resource",
                        action,
                        "shared",
                        f"shared resource {name}: {what}",
                        resource=name,
                    )
                )
        elif name in mine:
            steps.extend(_container_steps(name, old, new, project=project, running=running))
        elif name in owners:
            seen = running if running_scope is not None and name in running_scope else None
            steps.extend(_container_steps(name, old, new, project=owners[name], running=seen))
        else:
            if new is None:
                steps.append(
                    _step(
                        "container",
                        "remove",
                        "destructive",
                        "container that no project owns is removed; bay up writes that too",
                        container=name,
                    )
                )
            else:
                action = "create" if old is None else "update"
                steps.append(
                    _step(
                        "container",
                        action,
                        "shared",
                        "container that no project owns changes; bay up writes that too",
                        container=name,
                    )
                )
    if current.get("webhook") != wanted.get("webhook"):
        steps.append(
            _step("fleet", "update", "shared", "the deploy webhook changes", resource="webhook")
        )
    for i, step in enumerate(steps, start=1):
        step["id"] = f"s{i}"
    return steps


def tailnet_step(cx: Context, fleet_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """A change of the fleet's tailnet allowlist since the fleet's last commit.

    Only ``allowlist``: a route change is a ``route`` step of its own.
    """
    data = gitrepo.show_file(cx.fleet_root, "HEAD", f"./{FLEET_FILE}")
    if data is None:
        return None
    try:
        before = (tomllib.loads(data.decode("utf-8")).get("tailnet") or {}).get("allowlist")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if before == (fleet_doc.get("tailnet") or {}).get("allowlist"):
        return None
    return _step(
        "tailnet",
        "update",
        "shared",
        "the tailnet allowlist in bay.fleet.toml changed since the last fleet commit",
        resource="allowlist",
    )


# ── RUNNING ─────────────────────────────────────────────────────────────────


def running_slice(entries: list[dict[str, Any]], names: set[str]) -> dict[str, Any]:
    """This project's part of the receipts of one box env.

    ``receipt_sha256`` hashes only (box, name, image, config_hash) of this
    project's containers. Another project's deploy rewrites the receipt file
    but leaves this hash alone, so it does not make this project's plan stale.

    ``image`` is the reference the deploy asked for (the receipt's
    ``image_ref``; older receipts only have ``image``). The receipt's own
    ``image`` and ``commit`` move when a webhook build recreates a container
    (``python -m bay_reconcile.receipt stamp``); new code on a branch is not
    drift, so neither is read here. :func:`running_commits` reads them.
    """
    boxes: list[dict[str, Any]] = []
    rows: list[list[Any]] = []
    seen_receipt = False
    for entry in entries:
        receipt = entry.get("receipt")
        box: dict[str, Any] = {
            "box": entry.get("box"),
            "error": entry.get("error"),
            "containers": [],
        }
        if isinstance(receipt, Mapping):
            seen_receipt = True
            for c in receipt.get("containers") or []:
                if isinstance(c, Mapping) and c.get("name") in names:
                    row = {
                        "name": c.get("name"),
                        "image": c.get("image_ref") or c.get("image"),
                        "config_hash": c.get("config_hash"),
                    }
                    box["containers"].append(row)
                    rows.append([entry.get("box"), row["name"], row["image"], row["config_hash"]])
        boxes.append(box)
    digest = None
    if seen_receipt:
        canon = json.dumps(sorted(rows, key=lambda r: [str(x) for x in r]), sort_keys=True)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return {"checked": True, "receipt_sha256": digest, "boxes": boxes}


def running_commits(entries: list[dict[str, Any]], names: set[str]) -> dict[str, str | None]:
    """``{container: commit12 or None}`` for this project's containers, from the receipts."""
    out: dict[str, str | None] = {}
    for entry in entries:
        receipt = entry.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        for c in receipt.get("containers") or []:
            if isinstance(c, Mapping) and c.get("name") in names:
                commit = c.get("commit")
                out[str(c["name"])] = str(commit)[:12] if commit else None
    return out


#: ``(pin, running) -> "same" | "newer" | "older" | "unknown"``: where the
#: running commit stands against the pin. See :func:`code_order`.
CodeOrder = Callable[[str, str], str]


def code_order(repos: Sequence[Path], pin: str, running: str) -> str:
    """Where ``running`` stands against ``pin``, read in ``repos`` in order.

    ``repos`` is the checkout, then the fleet's repo cache (fetched before the
    plan, so it knows what a push deployed after the checkout was pulled).

    ``newer``: ``running`` descends from ``pin`` (a push deployed it after the
    pin). ``older``: ``pin`` descends from ``running`` (a held build, or the
    first deploy of new code). ``unknown``: no repo holds both commits, or the
    two are on different branches.
    """
    if pin.lower().startswith(running.lower()) or running.lower().startswith(pin.lower()):
        return "same"
    for repo in repos:
        forward = gitrepo.is_ancestor(repo, pin, running)
        if forward:
            return "newer"
        back = gitrepo.is_ancestor(repo, running, pin)
        if back:
            return "older"
        if forward is False and back is False:
            return "unknown"  # both known, on different branches
    return "unknown"


def _code_orderer(proj: ProjectRef) -> CodeOrder:
    """:func:`code_order` for ``proj``: the checkout first, then the repo cache.

    A checkout may be stale: a push deployed a commit it never fetched. So
    when the checkout cannot order the two, the fleet's repo cache is fetched
    (once per plan) and asked.
    """
    cache: list[Path] | None = None

    def order(pin: str, running: str) -> str:
        nonlocal cache
        where = code_order([proj.checkout], pin, running)
        if where != "unknown" or proj.in_fleet or not proj.repo:
            return where
        if cache is None:
            path, _ = reposource.ensure_cache(
                proj.cx.fleet_root, proj.repo, fetch=proj.source != "cache"
            )
            same = path is not None and path.resolve() == proj.checkout.resolve()
            cache = [path] if path is not None and not same else []
        return code_order(cache, pin, running) if cache else where

    return order


@dataclass
class CodeStatus:
    """What :func:`code_status` found: plan steps, notes, blockers and kept containers."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    #: Branch mode: build containers whose running code is newer than the pin.
    #: ``bay up`` applies the config and leaves their ``:latest`` alone.
    keep: list[str] = field(default_factory=list)


def code_status(
    commits: Mapping[str, str | None],
    *,
    project: str,
    track: str,
    in_fleet: bool,
    pinned_commit: str | None,
    wanted_commit: str | None,
    frozen: Mapping[str, Any] | None = None,
    order: CodeOrder | None = None,
    force_code: bool = False,
) -> CodeStatus:
    """Code against config.

    * ``track = "branch"``: a push deploys new code under the pinned config,
      so a receipt commit that differs from the pin is expected, not drift.
      One info line: ``code at <commit>, config pinned at <commit>``.
      ``bay up`` moves code only forward (``order`` given, app-repo project):

      - running newer than the commit ``bay up`` pins: keep it (``keep``);
        ``bay up`` applies the config only;
      - running older or the same: ``bay up`` moves ``:latest`` to the pin;
      - order unknown: a blocker, ``cannot order <pin> and <running>``, or
        with ``force_code`` a step ``image`` of risk destructive.
    * ``track = "pin"`` (bay.toml in the app repo): the code must be the
      commit ``bay up`` pins, backwards included. A container that runs
      another commit is a step ``image``, risk safe.
    * ``frozen`` (the env record, when ``bay rollback`` froze it): one info
      line that says pushes do not deploy.
    """
    out = CodeStatus()
    steps, info = out.steps, out.info
    if frozen and frozen.get("frozen"):
        at = str(frozen.get("frozen_commit") or "")[:12] or "unknown"
        info.append(
            f"frozen by bay rollback at {at}: a push builds but does not deploy, until a "
            "bay up to a newer commit"
        )
    known = {name: c for name, c in commits.items() if c}
    if track == "pin" and not in_fleet and wanted_commit:
        target = wanted_commit[:12]
        for name in sorted(known):
            if known[name] != target:
                steps.append(
                    _step(
                        "image",
                        "update",
                        "safe",
                        f"the box runs code {known[name]}; bay up deploys {target} "
                        '(track = "pin")',
                        container=name,
                        project=project,
                        source="box",
                    )
                )
        return out
    if order is not None and not in_fleet and wanted_commit:
        target = wanted_commit[:12]
        for name in sorted(known):
            where = order(wanted_commit, known[name])
            if where == "newer":
                out.keep.append(name)
            elif where == "unknown":
                problem = f"cannot order {target} and {known[name]}"
                if not force_code:
                    out.blockers.append(f"{problem}; fetch the repo or pass --force-code")
                    continue
                steps.append(
                    _step(
                        "image",
                        "update",
                        "destructive",
                        f"{problem}; --force-code moves the code to {target}, which may be "
                        "older than what runs",
                        container=name,
                        project=project,
                        source="box",
                    )
                )
    if out.keep:
        # bay up keeps this code and pins the config at WANTED.
        kept = sorted({known[name] for name in out.keep})
        info.append(f"code at {', '.join(kept)}, config pinned at {str(wanted_commit)[:12]}")
        return out
    pinned = (pinned_commit or "")[:12]
    running = sorted(set(known.values()))
    if pinned and running and running != [pinned]:
        info.append(f"code at {', '.join(running)}, config pinned at {pinned}")
    return out


def running_detail(entries: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    """For ``bay show``: per box the receipt result, time and this project's containers."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        receipt = entry.get("receipt") if isinstance(entry.get("receipt"), Mapping) else None
        out.append(
            {
                "box": entry.get("box"),
                "error": entry.get("error"),
                "result": receipt.get("result") if receipt else None,
                "deployed_at": receipt.get("deployed_at") if receipt else None,
                "fleet_commit": receipt.get("fleet_commit") if receipt else None,
                "containers": [
                    {k: c.get(k) for k in ("name", "image", "action", "healthy")}
                    for c in (receipt.get("containers") or [] if receipt else [])
                    if isinstance(c, Mapping) and c.get("name") in names
                ],
            }
        )
    return out


def _running_names(slice_: Mapping[str, Any]) -> set[str] | None:
    if not slice_.get("checked") or slice_.get("receipt_sha256") is None:
        return None
    if any(b.get("error") for b in slice_.get("boxes", [])):
        return None
    return {c["name"] for b in slice_["boxes"] for c in b["containers"]}


def default_receipt_reader(cx: Context, box_env: str) -> list[dict[str, Any]]:
    from bay_cli.receipts import fetch_receipts

    return fetch_receipts(cx, box_env)


def default_box_check(
    cx: Context, box_env: str, services_file: Path, *, config_files_root: Path | None = None
) -> list[dict[str, Any]] | None:
    """Today's deploy, in check mode, with the plan-only switch, against the compiled file.

    ``-e @file`` puts the compiled services, accessories and webhook above
    the fleet's own file, so the box is asked about WANTED, not PINNED.

    Check mode writes nothing live. Each box renders its env files into a
    scratch directory and plans against those
    (``roles/deploy_stack/tasks/env_scratch.yml``), so a container whose env
    file bytes change is predicted as a recreate, reason ``env_file``.

    Each box writes its plan-only JSON into a temporary directory on this
    machine (``bay_reconciler_plan_report_dir``, see
    ``roles/container_lifecycle/tasks/reconcile.yml``). The directory comes
    from ``tempfile.mkdtemp``, so it is outside every working tree, and it is
    removed when the reports are read. The real-run hand-off goes to its own
    temp dir too (``bay_reconciler_report_dir``), never to
    ``<framework>/.reconcile-report/``.

    ``config_files_root`` is the ``files/`` of the plan's scratch fleet; the
    check mode run copies config files from there, as ``bay up`` will.
    """
    from bay_cli.commands.ops import _run_playbook
    from bay_cli.healthcheck import new_report_dir, report_dir_vars

    tmp = tempfile.mkdtemp(prefix="bay-box-plan-")
    real_reports = new_report_dir()
    try:
        _run_playbook(
            cx,
            "deploy",
            box_env,
            "deploy_stack",
            [
                "-e",
                f"@{services_file}",
                "-e",
                "bay_reconciler_plan_only=true",
                "-e",
                json.dumps({"bay_reconciler_plan_report_dir": tmp}),
                "-e",
                "_rig_mode=true",
                "-e",
                "_rig_write=false",
                *report_dir_vars(real_reports),
                *config_files_vars(config_files_root),
                "--check",
            ],
        )
        return read_box_predictions(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(real_reports, ignore_errors=True)


def config_files_vars(root: Path | None) -> list[str]:
    """``-e`` arguments that point the deploy's config file copy at ``root``."""
    if root is None:
        return []
    return ["-e", json.dumps({"bay_config_files_root": str(root)})]


def read_box_predictions(directory: Path) -> list[dict[str, Any]]:
    """``[{"box", "report", "error"}]`` from the per-box plan-only files in ``directory``."""
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            out.append({"box": path.stem, "report": None, "error": f"unreadable: {exc}"})
            continue
        if not isinstance(report, dict):
            out.append({"box": path.stem, "report": None, "error": "not a JSON object"})
            continue
        out.append({"box": path.stem, "report": report, "error": None})
    return out


#: Box actions that change a container. ``noop`` is left out of the steps.
_BOX_RISK = {"create": "safe", "start": "safe", "recreate": "safe", "remove": "destructive"}


def box_prediction(entries: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The plan's ``box_prediction``: every container each box reported."""
    containers: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in entries or []:
        box = str(entry.get("box"))
        report = entry.get("report")
        if entry.get("error") or not isinstance(report, Mapping):
            errors.append(f"box {box}: {entry.get('error') or 'no report'}")
            continue
        listed = report.get("containers")
        if not isinstance(listed, list):
            errors.append(
                f"box {box}: the report lists no containers (the box runs an older Bay)"
            )
            continue
        for c in listed:
            if not isinstance(c, Mapping) or not c.get("name"):
                continue
            containers.append(
                {
                    "box": box,
                    "name": str(c["name"]),
                    "action": str(c.get("action") or "noop"),
                    "reasons": [str(r) for r in c.get("reasons") or []],
                }
            )
    containers.sort(key=lambda c: (c["box"], c["name"]))
    return {"checked": bool(containers) and not errors, "containers": containers, "errors": errors}


def box_steps(
    prediction: Mapping[str, Any],
    explained: set[str],
    *,
    project: str | None,
    mine: set[str],
    owners: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Steps for what the box will change that the compile diff does not explain.

    A container the compile diff already has a step for is left alone: the
    diff names the cause. Everything else the box predicts (a recreate after
    an env file or image change, a missing container, an orphan removal) is a
    step with ``source: box``. Risk: remove is destructive, the rest is safe.
    """
    steps: list[dict[str, Any]] = []
    for c in prediction.get("containers") or []:
        risk = _BOX_RISK.get(c["action"])
        if risk is None or c["name"] in explained:
            continue
        why = "; ".join(c["reasons"]) or "no reason given"
        steps.append(
            _step(
                "container",
                c["action"],
                risk,
                f"box {c['box']} predicts {c['action']}: {why}",
                container=c["name"],
                project=project if c["name"] in mine else (owners or {}).get(c["name"]),
                source="box",
            )
        )
    return steps


# ── Box move ────────────────────────────────────────────────────────────────


#: ``--data`` values. ``move`` is named so it can be refused with a reason.
DATA_KEEP = "keep"
DATA_MOVE = "move"
DATA_MOVE_REFUSED = (
    "--data move is deferred: Bay does not copy volumes or databases between boxes yet; "
    "see docs/plan.md (Box move)"
)


def check_data_mode(data: str | None) -> None:
    """Refuse ``--data move`` (deferred) and any value but ``keep``."""
    if data is None or data == DATA_KEEP:
        return
    if data == DATA_MOVE:
        raise BayError(DATA_MOVE_REFUSED, hint="Pass --data keep, or keep the box.")
    raise BayError(f"--data {data} is not known", hint="The only value today is --data keep.")


def _data_of(names: set[str], *datas: Mapping[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
    """Named volumes and databases of the containers ``names`` in the services files."""
    volumes: set[str] = set()
    databases: dict[str, str] = {}
    for data in datas:
        entries = _entries(data)
        for name in names:
            entry = entries.get(name)
            if not entry:
                continue
            volumes |= set(_named_volumes(entry))
            db = entry.get("database")
            if isinstance(db, Mapping) and db.get("name"):
                databases[str(db["name"])] = str(db.get("accessory") or "")
    return sorted(volumes), [{"name": k, "resource": v} for k, v in sorted(databases.items())]


def move_record(
    fleet: Mapping[str, Any],
    project: str,
    env: str,
    pinned: str | None,
    wanted: str | None,
    names: set[str],
    *datas: Mapping[str, Any],
    data: str | None = None,
) -> dict[str, Any] | None:
    """The plan's record of a box move, or None when WANTED and PINNED box agree.

    PINNED is the lock's box (None before the first ``bay up``: then there is
    nothing to move). The risk is ``destructive`` when the project's
    containers mount a named volume or use a database, else ``shared``.
    """
    if not pinned or not wanted or pinned == wanted:
        return None
    volumes, databases = _data_of(names, *datas)
    return {
        "project": project,
        "env": env,
        "from": pinned,
        "to": wanted,
        "from_box_env": box_and_env(fleet, pinned)[1],
        "to_box_env": box_and_env(fleet, wanted)[1],
        "containers": sorted(n for n in names if any(n in _entries(d) for d in datas)),
        "volumes": volumes,
        "databases": databases,
        "risk": "destructive" if volumes or databases else "shared",
        "data": data,
    }


def _left_behind(move: Mapping[str, Any]) -> str:
    parts = []
    if move["volumes"]:
        parts.append("volumes " + ", ".join(move["volumes"]))
    if move["databases"]:
        parts.append("database " + ", ".join(d["name"] for d in move["databases"]))
    return " and ".join(parts)


def move_steps(move: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``move`` step, then per container a remove on the old box and a create on the new."""
    a, b, who, risk = move["from"], move["to"], move["project"], move["risk"]
    left = _left_behind(move)
    reason = f"{who} moves from box {a} to box {b}"
    reason += (
        f"; {left} stay on box {a}, and the new box starts empty"
        if left
        else "; it has no volume and no database"
    )
    steps = [_step("move", "move", risk, reason, resource=f"{a} -> {b}", project=who)]
    for name in move["containers"]:
        steps.append(
            _step(
                "container",
                "remove",
                risk,
                f"moves to box {b}; the container on box {a} is removed"
                + (f"; {left} stay on box {a}" if left else ""),
                container=name,
                project=who,
            )
        )
        steps.append(
            _step(
                "container",
                "create",
                "safe",
                f"starts on box {b}" + ("; its volumes and database start empty" if left else ""),
                container=name,
                project=who,
            )
        )
    return steps


def move_blocker(move: Mapping[str, Any]) -> str | None:
    """A destructive move is blocked until ``--data keep``."""
    if move["risk"] != "destructive" or move.get("data") == DATA_KEEP:
        return None
    return (
        f"{move['project']} moves from box {move['from']} to box {move['to']}, and its data "
        f"does not move with it: {_left_behind(move)}. Pass --data keep to start empty on "
        f"box {move['to']} and leave the data untouched on box {move['from']}, or set "
        f"deploy.{move['env']}.box back to {move['from']}"
    )


def move_notes(move: Mapping[str, Any]) -> list[str]:
    """With ``--data keep``: what stays on the old box, and how to remove it later by hand."""
    if move.get("data") != DATA_KEEP or move["risk"] != "destructive":
        return []
    out = [
        f"after the move, {_left_behind(move)} of {move['project']} stay on box "
        f"{move['from']}; Bay never removes them"
    ]
    if move["volumes"]:
        out.append(
            f"to remove them later on box {move['from']}: docker volume rm "
            + " ".join(move["volumes"])
        )
    for db in move["databases"]:
        out.append(
            f"to remove it later on box {move['from']}: DROP DATABASE {db['name']}; "
            f"(in resource {db['resource']})"
        )
    return out


# ── Plan ────────────────────────────────────────────────────────────────────


@dataclass
class PlanOptions:
    env: str | None = None
    at: str | None = None
    read_running: bool = True
    box_check: bool = False
    allow_unsupported: bool = False
    cwd_repo: Path | None = None
    #: ``--data``: ``keep`` lets a box move with data start empty on the new
    #: box and leave the data on the old one (:func:`move_blocker`).
    data: str | None = None
    #: Branch mode: allow ``bay up`` to move code it cannot order against the
    #: pin (a destructive ``image`` step). See :func:`code_status`.
    force_code: bool = False
    #: Branch mode: order running code against the pin. ``bay rollback`` turns
    #: it off: it moves code backwards on purpose.
    code_order: bool = True


# ``notes`` is text for the reader (a missing --remote hint, the directory the
# command ran in). It never changes what a deploy does, so it is not hashed.
_UNHASHED = (
    "plan_id",
    "plan_sha256",
    "created_at",
    "verdict",
    "exit_code",
    "approval",
    "stale",
    "notes",
)


def body_sha256(plan: Mapping[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in _UNHASHED}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _FleetState:
    is_git: bool
    head: str | None
    dirty: bool | None
    behind: bool | None
    blockers: list[str]


def _fleet_state(cx: Context) -> _FleetState:
    is_git = gitrepo.is_repo(cx.fleet_root)
    head = gitrepo.head(cx.fleet_root) if is_git else None
    dirty = gitrepo.dirty(cx.fleet_root, exclude=(PLANS_DIR,)) if is_git else None
    behind: bool | None = None
    blockers: list[str] = []
    if not is_git or head is None:
        blockers.append(f"the fleet {cx.fleet_root} is not a git repo with a commit")
    else:
        behind, problem = gitrepo.behind_remote(cx.fleet_root)
        if problem:
            blockers.append(problem)
        elif behind:
            blockers.append("the fleet repo is behind its remote; pull it first")
    return _FleetState(is_git, head, dirty, behind, blockers)


def _env_names(proj: ProjectRef, docs: list[dict[str, Any] | None]) -> dict[str, dict[str, str]]:
    names_by_env: dict[str, dict[str, str]] = {}
    for doc in docs:
        if doc:
            for e, names in project_containers(proj.name, doc, proj.lock, proj.primary_env).items():
                names_by_env.setdefault(e, {}).update(names)
    return names_by_env


def _check_wanted_doc(proj: ProjectRef, wanted: Wanted, env: str, blockers: list[str]) -> None:
    if wanted.doc is None:
        return
    from bay_cli import bay_toml

    for v in bay_toml.validate(wanted.doc):
        blockers.append(f"{proj.toml_path}: {v}")
    if wanted.doc.get("name") != proj.name:
        blockers.append(
            f"{proj.toml_path}: name is {wanted.doc.get('name')}, "
            f"but the fleet knows the project as {proj.name}"
        )
    if env not in (wanted.doc.get("deploy") or {}):
        blockers.append(f"{proj.toml_path} has no [deploy.{env}]")


@dataclass
class _Placement:
    """Where one project of a plan deploys, and whether that is a box move."""

    box: str | None
    box_env: str | None
    #: ``(from, to)`` when the WANTED box differs from the PINNED box.
    move: tuple[str, str] | None = None


def _placement(
    proj: ProjectRef, env: str, doc: Mapping[str, Any] | None, blockers: list[str], label: str = ""
) -> _Placement:
    """The box a plan deploys ``proj`` to: the WANTED box when it differs from the pin."""
    box, box_env = resolve_box(proj, env, doc)
    move = None
    if doc is not None and env in (doc.get("deploy") or {}):
        to_box, from_box = wanted_box(proj.fleet, doc, env), pinned_box(proj.lock, env)
        if from_box and to_box and from_box != to_box:
            if box_and_env(proj.fleet, to_box)[1] is None:
                blockers.append(f"{label}box {to_box} is not in bay.fleet.toml [boxes]")
            else:
                box, box_env = box_and_env(proj.fleet, to_box)
                move = (from_box, to_box)
    if box_env is None:
        blockers.append(f"{label}box {box} is not in bay.fleet.toml [boxes]")
    return _Placement(box, box_env, move)


@dataclass
class _Diff:
    steps: list[dict[str, Any]] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    box_checked: bool = False
    prediction: dict[str, Any] = field(
        default_factory=lambda: {"checked": False, "containers": [], "errors": []}
    )
    wanted_data: dict[str, Any] = field(default_factory=dict)


def _compile_and_diff(
    cx: Context,
    opts: PlanOptions,
    *,
    pins: Mapping[str, str],
    boxes: Mapping[str, Mapping[str, str]],
    cwd: Path | None,
    current: Mapping[str, Any],
    project: str | None,
    mine: set[str],
    names: set[str],
    resources: set[str],
    box_env: str | None,
    running: Mapping[str, Any],
    blockers: list[str],
    notes: list[str],
    check_box: BoxCheck | None,
    fleet_doc: Mapping[str, Any],
) -> _Diff:
    """Compile the fleet copy, diff it against PINNED, check secrets and (``--remote``) the box.

    ``names`` are the containers of the planned project(s) in the env.
    """
    from bay_cli import secrets_check

    out = _Diff()
    with compiled_fleet(cx, pins, cwd=cwd, boxes=boxes) as comp:
        blockers.extend(comp.errors)
        notes.extend(comp.notes)
        notes.extend(comp.uncommitted)
        if comp.result is None:
            return out
        out.unsupported = [str(u) for u in comp.result.unsupported]
        if out.unsupported and not opts.allow_unsupported:
            blockers.append(
                f"{len(out.unsupported)} bay.toml feature(s) cannot be deployed yet "
                "(see unsupported); remove them or pass --allow-unsupported"
            )
        out.wanted_data = comp.result.data()
        out.steps = diff_steps(
            current,
            out.wanted_data,
            project=project,
            mine=mine,
            resources=resources,
            running=_running_names(running),
            owners=comp.owners,
            running_scope=names,
        )
        # Tailnet routes (spec M117/07): steps, and a blocker off the ingress env.
        from bay_cli import routes

        route_steps, route_blockers = routes.plan_routes(
            current, out.wanted_data, fleet_doc, box_env
        )
        out.steps.extend(route_steps)
        blockers.extend(route_blockers)
        if box_env is not None:
            out.missing, problem = _missing_secrets(
                cx, box_env, out.wanted_data, names, secrets_check
            )
            if problem:
                blockers.append(problem)
            for m in out.missing:
                blockers.append(
                    f"secret {m['name']} is missing in the fleet for "
                    f"{box_env} (used by {', '.join(m['used_by'])})"
                )
        if opts.box_check and box_env is not None and not blockers:
            services_file = comp.services_file()
            assert services_file is not None
            checker = check_box or partial(default_box_check, config_files_root=comp.files_root)
            try:
                entries = checker(cx, box_env, services_file)
                out.box_checked = True
            except (BayError, OSError, SystemExit) as exc:
                blockers.append(f"the check on the box failed: {exc}")
            else:
                out.prediction = box_prediction(entries)
                blockers.extend(
                    f"the check on the box gave no prediction: {e}"
                    for e in out.prediction["errors"]
                )
                if not out.prediction["containers"] and not out.prediction["errors"]:
                    blockers.append("the check on the box gave no prediction")
                explained = {str(s["container"]) for s in out.steps if s["container"]}
                out.steps.extend(
                    box_steps(
                        out.prediction, explained, project=project, mine=mine, owners=comp.owners
                    )
                )
    return out


def _finish(
    cx: Context,
    plan: dict[str, Any],
    state: _FleetState,
    fleet_doc: Mapping[str, Any],
    box_checked: bool,
    box_env: str | None,
) -> dict[str, Any]:
    """The tail both plan kinds share: tailnet step, ids, notes, dirty gate, id, verdict."""
    steps, blockers, notes = plan["steps"], plan["blockers"], plan["notes"]
    if state.is_git:
        t_step = tailnet_step(cx, fleet_doc)
        if t_step is not None:
            steps.append(t_step)
    for i, step in enumerate(steps, start=1):
        step["id"] = f"s{i}"
    if not box_checked:
        notes.append(
            "the steps come from the compiled files alone; pass --remote to check them on the box"
        )
    if box_env is not None:
        notes.append(UNPLANNED_NOTE.format(box_env=box_env))
    if state.dirty and any(s["risk"] == "destructive" for s in steps):
        blockers.append(
            "the fleet repo has uncommitted changes and a step is destructive; "
            "commit or drop the changes first"
        )
    sha = body_sha256(plan)
    plan["plan_sha256"] = sha
    plan["plan_id"] = sha[:12]
    plan["approval"] = find_approval(cx, plan)
    decide(plan)
    return plan


def _apply_moves(
    plan_moves: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    blockers: list[str],
    notes: list[str],
) -> list[dict[str, Any]]:
    """Put each move's steps first; a destructive move without ``--data keep`` blocks."""
    lead: list[dict[str, Any]] = []
    for move in plan_moves:
        lead.extend(move_steps(move))
        blocker = move_blocker(move)
        if blocker:
            blockers.append(blocker)
        notes.extend(move_notes(move))
    return lead + steps


def make_plan(
    proj: ProjectRef,
    opts: PlanOptions,
    *,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """Build the plan body, then its id, approval and verdict. Does not save it."""
    check_data_mode(opts.data)
    cx = proj.cx
    env = opts.env or proj.primary_env
    state = _fleet_state(cx)
    blockers: list[str] = list(state.blockers)
    notes: list[str] = list(proj.notes)

    # WANTED
    wanted = read_wanted(proj, opts.at)
    blockers.extend(wanted.problems)
    if wanted.commit and not wanted.problems and commit_on_remote(proj, wanted.commit) is not True:
        notes.append(
            f"commit {wanted.commit[:12]} is not on a branch of {proj.repo}; "
            "bay up refuses it: push first"
        )
    if wanted.dirty:
        notes.append(
            f"the project has uncommitted changes; the plan uses commit "
            f"{(wanted.commit or '')[:12]} and ignores them"
        )
    if (
        not proj.in_fleet
        and opts.cwd_repo is not None
        and opts.cwd_repo.resolve() != proj.checkout.resolve()
    ):
        where = "its repo cache" if proj.source == "cache" else str(proj.checkout)
        notes.append(
            f"this command ran in {opts.cwd_repo}, but the fleet reads the project from {where}"
        )
    _check_wanted_doc(proj, wanted, env, blockers)
    place = _placement(proj, env, wanted.doc, blockers)

    # PINNED
    lock_sha = lockfile.sha256_of(proj.lock_file)
    pinned_commit = lockfile.env_pin(proj.lock, env)
    pinned_doc = doc_at(proj, proj.lock.get("commit"))
    current, services_state = current_services(cx)
    _services_blockers(services_state, blockers)

    names_by_env = _env_names(proj, [pinned_doc, wanted.doc])
    mine = {n for names in names_by_env.values() for n in names.values()}
    env_names = set(names_by_env.get(env, {}).values())

    # RUNNING
    running, receipt_entries = _read_running(
        cx, opts, place.box_env, env_names, notes, read_receipts
    )

    # Compile WANTED and diff
    diff = _Diff()
    if wanted.doc is not None and not wanted.problems:
        diff = _compile_and_diff(
            cx,
            opts,
            pins={proj.name: wanted.commit} if wanted.commit else {},
            boxes={proj.name: {env: place.move[1]}} if place.move else {},
            cwd=proj.cwd,
            current=current,
            project=proj.name,
            mine=mine,
            names=env_names,
            resources=set((proj.fleet.get("resources") or {}).keys()),
            box_env=place.box_env,
            running=running,
            blockers=blockers,
            notes=notes,
            check_box=check_box,
            fleet_doc=proj.fleet,
        )
    built = {
        n
        for n in env_names
        if "build" in ((diff.wanted_data.get("services") or {}).get(n) or {})
        or "build" in ((diff.wanted_data.get("accessories") or {}).get(n) or {})
    }
    keep: list[str] = []
    if wanted.doc is not None and receipt_entries:
        from bay_cli import bay_toml

        # Only build containers carry this project's code; a pulled image's
        # revision label names someone else's repo.
        commits = {
            name: c
            for name, c in running_commits(receipt_entries, env_names).items()
            if name in built
        }
        code = code_status(
            commits,
            project=proj.name,
            track=bay_toml.track(wanted.doc, env),
            in_fleet=proj.in_fleet,
            pinned_commit=pinned_commit,
            wanted_commit=wanted.commit,
            frozen=(proj.lock.get("envs") or {}).get(env),
            order=_code_orderer(proj) if opts.code_order else None,
            force_code=opts.force_code,
        )
        explained = {str(s["container"]) for s in diff.steps if s["container"]}
        diff.steps.extend(
            s for s in code.steps if s["risk"] == "destructive" or s["container"] not in explained
        )
        notes.extend(code.info)
        blockers.extend(code.blockers)
        keep = code.keep
    moves: list[dict[str, Any]] = []
    if place.move:
        move = move_record(
            proj.fleet,
            proj.name,
            env,
            place.move[0],
            place.move[1],
            env_names,
            current,
            diff.wanted_data,
            data=opts.data,
        )
        if move is not None:
            moves.append(move)
    steps = _apply_moves(moves, diff.steps, blockers, notes)

    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "plan_id": "",
        "plan_sha256": "",
        "created_at": _now(),
        "project": proj.name,
        "env": env,
        "box": place.box,
        "box_env": place.box_env,
        "fleet": {
            "name": proj.fleet.get("name"),
            "commit": state.head,
            "dirty": state.dirty,
            "behind": state.behind,
        },
        "wanted": {
            "commit": wanted.commit,
            "toml_sha256": wanted.toml_sha256,
            "dirty": wanted.dirty,
        },
        "pinned": {"commit": pinned_commit, "lock_sha256": lock_sha},
        "running": running,
        "box_checked": diff.box_checked,
        "box_prediction": diff.prediction,
        "steps": steps,
        "unsupported": diff.unsupported,
        "missing_secrets": diff.missing,
        "blockers": blockers,
        "notes": notes,
        "verdict": "",
        "exit_code": 0,
        "approval": None,
        "stale": [],
    }
    # Only a plan with a box move carries ``moves``: an older plan without the
    # key keeps its id.
    if moves:
        plan["moves"] = moves
    if keep:
        # Hashed: it changes what bay up does with the code.
        plan["code"] = {"keep": keep}
    return _finish(cx, plan, state, proj.fleet, diff.box_checked, place.box_env)


def _services_blockers(state: str, blockers: list[str]) -> None:
    if state == "foreign":
        blockers.append("the fleet's services file was not written by bay; run `bay import` first")
    elif state == "edited":
        blockers.append(
            "the fleet's services file was edited by hand since the last compile; "
            "move the change into bay.toml or bay.fleet.toml"
        )


def _read_running(
    cx: Context,
    opts: PlanOptions,
    box_env: str | None,
    names: set[str],
    notes: list[str],
    read_receipts: ReceiptReader | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(running slice, raw receipt entries)``; the entries feed :func:`code_status`."""
    running: dict[str, Any] = {"checked": False, "receipt_sha256": None, "boxes": []}
    entries: list[dict[str, Any]] = []
    if opts.read_running and box_env is not None:
        reader = read_receipts or default_receipt_reader
        entries = reader(cx, box_env)
        running = running_slice(entries, names)
        for b in running["boxes"]:
            if b.get("error"):
                notes.append(f"box {b.get('box') or box_env}: {b['error']}")
    elif not opts.read_running:
        notes.append("the box receipt was not read (--no-remote)")
    return running, entries


# ── The whole environment ───────────────────────────────────────────────────


def fleet_projects(cx: Context) -> list[str]:
    """Every project folder of the fleet: ``projects/<name>/`` with a lock or a bay.toml."""
    root = cx.fleet_root / PROJECTS_DIR
    if not root.is_dir():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and ((p / LOCK_FILE).is_file() or (p / "bay.toml").is_file())
    )


def make_env_plan(
    cx: Context,
    opts: PlanOptions,
    *,
    ats: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """One plan for every project that has ``[deploy.<env>]``: ``bay plan <env>`` in a fleet.

    Each project is read at its WANTED commit (``ats`` overrides some, for a
    recheck), the fleet is compiled once, and every step carries the project
    it belongs to. The record has ``project: null`` and lists the projects in
    ``projects``. Verdict and exit code as for one project.
    """
    check_data_mode(opts.data)
    moved = layout.ensure(cx.fleet_root)
    fleet_doc = load_fleet_doc(cx)
    env = opts.env or str(fleet_doc.get("primary_env", "production"))
    state = _fleet_state(cx)
    blockers: list[str] = list(state.blockers)
    notes: list[str] = [f"fleet layout: {line}" for line in moved]

    members: list[tuple[ProjectRef, Wanted]] = []
    for name in fleet_projects(cx):
        try:
            proj = load_project(cx, name, cwd=cwd)
        except BayError as exc:
            blockers.append(f"{name}: {exc}")
            continue
        notes.extend(f"{name}: {n}" for n in proj.notes if not n.startswith("fleet layout"))
        wanted = read_wanted(proj, (ats or {}).get(name))
        if wanted.doc is None:
            blockers.extend(f"{name}: {p}" for p in wanted.problems)
            continue
        if env not in (wanted.doc.get("deploy") or {}):
            continue
        blockers.extend(f"{name}: {p}" for p in wanted.problems)
        _check_wanted_doc(proj, wanted, env, blockers)
        if wanted.commit and commit_on_remote(proj, wanted.commit) is not True:
            notes.append(
                f"{name}: commit {wanted.commit[:12]} is not on a branch of {proj.repo}; "
                "bay up refuses it: push first"
            )
        if wanted.dirty:
            notes.append(
                f"{name} has uncommitted changes; the plan uses commit "
                f"{(wanted.commit or '')[:12]} and ignores them"
            )
        members.append((proj, wanted))

    current, services_state = current_services(cx)
    _services_blockers(services_state, blockers)

    projects: list[dict[str, Any]] = []
    placements: dict[str, _Placement] = {}
    env_names: dict[str, set[str]] = {}
    by_box_env: dict[str, list[str]] = {}
    for proj, wanted in members:
        place = _placement(proj, env, wanted.doc, blockers, label=f"{proj.name}: ")
        placements[proj.name] = place
        if place.box_env is not None:
            by_box_env.setdefault(place.box_env, []).append(proj.name)
        pinned_doc = doc_at(proj, proj.lock.get("commit"))
        env_names[proj.name] = set(
            _env_names(proj, [pinned_doc, wanted.doc]).get(env, {}).values()
        )
        projects.append(
            {
                "name": proj.name,
                "box": place.box,
                "box_env": place.box_env,
                "wanted": {
                    "commit": wanted.commit,
                    "toml_sha256": wanted.toml_sha256,
                    "dirty": wanted.dirty,
                },
                "pinned": {
                    "commit": lockfile.env_pin(proj.lock, env),
                    "lock_sha256": lockfile.sha256_of(proj.lock_file),
                },
            }
        )
    if not members:
        notes.append(f"no project of fleet {fleet_doc.get('name')} has [deploy.{env}]")
    if len(by_box_env) > 1:
        blockers.append(
            f"the projects of {env} sit on boxes of more than one box environment ("
            + "; ".join(f"{e}: {', '.join(n)}" for e, n in sorted(by_box_env.items()))
            + "); plan them one at a time with --project"
        )
    box_env = next(iter(by_box_env)) if len(by_box_env) == 1 else None
    boxes_used = {p["box"] for p in projects}
    box = next(iter(boxes_used)) if len(boxes_used) == 1 else None
    all_names = set().union(*env_names.values()) if env_names else set()

    running, _ = _read_running(cx, opts, box_env, all_names, notes, read_receipts)

    diff = _Diff()
    if members and not any(w.problems for _, w in members):
        diff = _compile_and_diff(
            cx,
            opts,
            pins={p.name: w.commit for p, w in members if w.commit},
            boxes={n: {env: pl.move[1]} for n, pl in placements.items() if pl.move},
            cwd=cwd,
            current=current,
            project=None,
            mine=set(),
            names=all_names,
            resources=set((fleet_doc.get("resources") or {}).keys()),
            box_env=box_env,
            running=running,
            blockers=blockers,
            notes=notes,
            check_box=check_box,
            fleet_doc=fleet_doc,
        )
    moves: list[dict[str, Any]] = []
    for proj, _ in members:
        place = placements[proj.name]
        if not place.move:
            continue
        move = move_record(
            fleet_doc,
            proj.name,
            env,
            place.move[0],
            place.move[1],
            env_names[proj.name],
            current,
            diff.wanted_data,
            data=opts.data,
        )
        if move is not None:
            moves.append(move)
    steps = _apply_moves(moves, diff.steps, blockers, notes)

    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "plan_id": "",
        "plan_sha256": "",
        "created_at": _now(),
        "project": None,
        "projects": projects,
        "env": env,
        "box": box,
        "box_env": box_env,
        "fleet": {
            "name": fleet_doc.get("name"),
            "commit": state.head,
            "dirty": state.dirty,
            "behind": state.behind,
        },
        "wanted": {
            "commit": None,
            "toml_sha256": None,
            "dirty": any(bool(w.dirty) for _, w in members),
        },
        "pinned": {"commit": None, "lock_sha256": None},
        "running": running,
        "box_checked": diff.box_checked,
        "box_prediction": diff.prediction,
        "steps": steps,
        "unsupported": diff.unsupported,
        "missing_secrets": diff.missing,
        "blockers": blockers,
        "notes": notes,
        "verdict": "",
        "exit_code": 0,
        "approval": None,
        "stale": [],
    }
    if moves:
        plan["moves"] = moves
    return _finish(cx, plan, state, fleet_doc, diff.box_checked, box_env)


def _missing_secrets(
    cx: Context,
    box_env: str,
    wanted_data: Mapping[str, Any],
    names: set[str],
    secrets_check: Any,
) -> tuple[list[dict[str, Any]], str | None]:
    services = {k: v for k, v in (wanted_data.get("services") or {}).items() if k in names}
    accessories = {k: v for k, v in (wanted_data.get("accessories") or {}).items() if k in names}
    required = secrets_check.required_secrets(services, accessories)
    if not required:
        return [], None
    try:
        held = secrets_check.vault_names(cx, box_env)
    except secrets_check.SecretsUncheckable as exc:
        return [], f"cannot check the secrets for {box_env}: {exc}"
    return [m.to_dict() for m in secrets_check.compare(required, held)], None


def decide(plan: dict[str, Any]) -> None:
    """Set ``verdict`` and ``exit_code``: blocked, then stale, then approve, else auto."""
    if plan["blockers"]:
        verdict = "blocked"
    elif plan.get("stale"):
        verdict = "stale"
    elif any(s["risk"] in ("destructive", "shared") for s in plan["steps"]) and not plan.get(
        "approval"
    ):
        verdict = "approve"
    else:
        verdict = "auto"
    plan["verdict"] = verdict
    plan["exit_code"] = VERDICT_EXIT[verdict]


def needs_approval(plan: Mapping[str, Any]) -> bool:
    return any(s["risk"] in ("destructive", "shared") for s in plan["steps"])


# ── Saved plans and approvals ───────────────────────────────────────────────


def plans_dir(cx: Context) -> Path:
    return cx.fleet_root / PLANS_DIR


def plan_file(cx: Context, plan_id: str) -> Path:
    return plans_dir(cx) / f"{plan_id}.json"


def approval_file(cx: Context, plan_id: str) -> Path:
    return plans_dir(cx) / f"{plan_id}{APPROVED_SUFFIX}"


def save(cx: Context, plan: Mapping[str, Any]) -> Path:
    path = plan_file(cx, str(plan["plan_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2) + "\n")
    tmp.replace(path)
    return path


def load_saved(cx: Context, plan_id: str) -> dict[str, Any]:
    if not plan_id or not all(c in "0123456789abcdef" for c in plan_id):
        raise BayError(f"not a plan id: {plan_id}")
    path = plan_file(cx, plan_id)
    if not path.is_file():
        raise BayError(
            f"no saved plan {plan_id} in {plans_dir(cx)}",
            code=ErrorCode.NOT_FOUND,
            hint="Run `bay plan` to make one.",
        )
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise BayError(f"{path} is not a plan")
    return data


def find_approval(cx: Context, plan: Mapping[str, Any]) -> dict[str, Any] | None:
    path = approval_file(cx, str(plan["plan_id"]))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("plan_sha256") != plan["plan_sha256"]:
        return None
    return {"reason": data.get("reason"), "approved_at": data.get("approved_at")}


def approve(cx: Context, plan_id: str, reason: str) -> Path:
    if not reason.strip():
        raise BayError("an approval needs a reason", hint='Pass --reason "<why>".')
    saved = load_saved(cx, plan_id)
    if saved.get("verdict") in ("blocked", "stale"):
        raise BayError(
            f"plan {plan_id} is {saved.get('verdict')}; an approval cannot fix that",
            hint="Fix the blockers and run `bay plan` again.",
        )
    if body_sha256(saved) != saved.get("plan_sha256"):
        raise BayError(f"plan {plan_id} was changed after it was saved")
    path = approval_file(cx, plan_id)
    path.write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "plan_sha256": saved["plan_sha256"],
                "reason": reason.strip(),
                "approved_at": _now(),
            },
            indent=2,
        )
        + "\n"
    )
    return path


def _lock_shas(plan: Mapping[str, Any]) -> dict[str, Any]:
    """``{project: lock sha256}`` of every project a plan covers."""
    if plan.get("projects") is not None:
        return {p["name"]: p["pinned"]["lock_sha256"] for p in plan["projects"]}
    return {plan["project"]: plan["pinned"]["lock_sha256"]}


def stale_reasons(saved: Mapping[str, Any], fresh: Mapping[str, Any]) -> list[str]:
    """Why a saved plan no longer holds. Empty when it still does."""
    reasons: list[str] = []
    before, after = _lock_shas(saved), _lock_shas(fresh)
    for name in sorted(set(before) & set(after)):
        if before[name] != after[name]:
            reasons.append(
                f"the pin moved: {PROJECTS_DIR}/{name}/{LOCK_FILE} changed since the plan"
            )
    s_run, f_run = saved["running"], fresh["running"]
    if (
        s_run.get("checked")
        and f_run.get("checked")
        and (s_run.get("receipt_sha256") != f_run.get("receipt_sha256"))
    ):
        reasons.append("the box changed: its receipt for this project differs from the plan")
    if not reasons and saved["plan_id"] != fresh["plan_id"]:
        keys = sorted((set(saved) | set(fresh)) - set(_UNHASHED))
        changed = [k for k in keys if saved.get(k) != fresh.get(k)]
        if changed:
            reasons.append("the plan inputs changed since the plan: " + ", ".join(changed))
        else:
            reasons.append("plan id differs but no input changed")
    return reasons


def saved_data_mode(saved: Mapping[str, Any]) -> str | None:
    """The ``--data`` a saved plan was made with (read from its moves)."""
    modes = {m.get("data") for m in saved.get("moves") or []}
    return DATA_KEEP if DATA_KEEP in modes else None


def _refuse_remove_plan(saved: Mapping[str, Any]) -> None:
    """A ``bay remove`` plan is checked again by :mod:`bay_cli.remove`, never here."""
    if saved.get("remove"):
        raise BayError(
            f"plan {saved.get('plan_id')} removes {saved.get('project')}; it is not a deploy plan",
            hint=f"Apply it with `bay up {saved.get('env')} --plan-id {saved.get('plan_id')}`.",
        )


def recheck(
    proj: ProjectRef,
    saved: Mapping[str, Any],
    opts: PlanOptions,
    *,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """Plan again with the saved plan's env, commit and checks. Mark it stale when it moved.

    A plan made with a box check is checked again on the box, so the two bodies
    can match. Without that the ``box_checked`` and ``box_prediction`` fields
    would always differ and the plan would look stale.
    """
    _refuse_remove_plan(saved)
    if saved.get("project") != proj.name:
        raise BayError(
            f"plan {saved.get('plan_id')} is for project {saved.get('project')}, not {proj.name}"
        )
    again = PlanOptions(
        env=str(saved["env"]),
        at=saved["wanted"]["commit"],
        read_running=opts.read_running or bool(saved["running"].get("checked")),
        box_check=opts.box_check or bool(saved.get("box_checked")),
        allow_unsupported=opts.allow_unsupported,
        cwd_repo=opts.cwd_repo,
        data=opts.data or saved_data_mode(saved),
        # A plan made with --force-code carries its destructive image step;
        # re-plan with the flag so the two bodies match.
        force_code=opts.force_code
        or any(
            s.get("kind") == "image" and s.get("risk") == "destructive"
            for s in saved.get("steps") or []
        ),
        code_order=opts.code_order,
    )
    fresh = make_plan(proj, again, read_receipts=read_receipts, check_box=check_box)
    fresh["stale"] = stale_reasons(saved, fresh)
    decide(fresh)
    return fresh


def recheck_env(
    cx: Context,
    saved: Mapping[str, Any],
    opts: PlanOptions,
    *,
    cwd: Path | None = None,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """:func:`recheck` for a whole-environment plan: each project at its saved commit."""
    _refuse_remove_plan(saved)
    if saved.get("project") is not None or saved.get("projects") is None:
        raise BayError(
            f"plan {saved.get('plan_id')} is for project {saved.get('project')}, "
            "not for a whole environment",
            hint="Pass --project with that name.",
        )
    again = PlanOptions(
        env=str(saved["env"]),
        read_running=opts.read_running or bool(saved["running"].get("checked")),
        box_check=opts.box_check or bool(saved.get("box_checked")),
        allow_unsupported=opts.allow_unsupported,
        data=opts.data or saved_data_mode(saved),
    )
    ats = {p["name"]: p["wanted"]["commit"] for p in saved["projects"] if p["wanted"]["commit"]}
    fresh = make_env_plan(
        cx, again, ats=ats, cwd=cwd, read_receipts=read_receipts, check_box=check_box
    )
    fresh["stale"] = stale_reasons(saved, fresh)
    decide(fresh)
    return fresh


# ── Human output ────────────────────────────────────────────────────────────


def render(plan: Mapping[str, Any]) -> str:
    def short(sha: Any) -> str:
        return str(sha)[:12] if sha else "none"

    w, p, r = plan["wanted"], plan["pinned"], plan["running"]
    running = "  RUNNING " + (
        f"receipt {short(r['receipt_sha256'])}"
        if r["checked"] and r["receipt_sha256"]
        else "no receipt"
        if r["checked"]
        else "not read"
    )
    if plan.get("projects") is not None:
        lines = [
            f"plan {plan['plan_id']}  {plan['env']} (whole environment, "
            f"{len(plan['projects'])} projects) -> box environment {plan['box_env']}",
        ]
        for proj in plan["projects"]:
            pw = proj["wanted"]
            lines.append(
                f"  {proj['name']}: WANTED {short(pw['commit'])}"
                f"{' (uncommitted changes ignored)' if pw['dirty'] else ''}, "
                f"PINNED {short(proj['pinned']['commit'])}, box {proj['box']}"
            )
        lines += [running, ""]
    else:
        lines = [
            f"plan {plan['plan_id']}  {plan['project']} {plan['env']} -> box {plan['box']} "
            f"({plan['box_env']})",
            f"  WANTED  {short(w['commit'])}"
            f"{' (uncommitted changes ignored)' if w['dirty'] else ''}",
            f"  PINNED  {short(p['commit'])}",
            running,
            "",
        ]
    if plan["steps"]:
        rows = [("STEP", "FROM", "RISK", "ACTION", "PROJECT", "WHAT", "WHY")]
        for s in plan["steps"]:
            what = s["kind"] + " " + (s["resource"] or s["container"] or "")
            if s["kind"] in ("volume", "database", "database_user", "secret") and s["container"]:
                what += f" ({s['container']})"
            rows.append(
                (
                    s["id"],
                    s.get("source", "compile"),
                    s["risk"],
                    s["action"],
                    s.get("project") or "-",
                    what,
                    s["reason"],
                )
            )
        widths = [max(len(row[i]) for row in rows) for i in range(6)]
        for row in rows:
            lines.append(
                "  " + "  ".join(row[i].ljust(widths[i]) for i in range(6)) + "  " + row[6]
            )
    else:
        lines.append("  no changes")
    prediction = plan.get("box_prediction") or {}
    if plan.get("box_checked") and prediction.get("containers"):
        counts: dict[str, int] = {}
        for c in prediction["containers"]:
            counts[c["action"]] = counts.get(c["action"], 0) + 1
        lines.append(
            "box prediction: "
            + ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
        )
    for b in plan["blockers"]:
        lines.append(f"blocked: {b}")
    for s in plan.get("stale") or []:
        lines.append(f"stale: {s}")
    for n in plan["notes"]:
        lines.append(f"note: {n}")
    if plan.get("approval"):
        lines.append(f"approved: {plan['approval'].get('reason')}")
    verdict = plan["verdict"]
    tail = {
        "auto": "bay up may apply it",
        "approve": f'run `bay approve {plan["plan_id"]} --reason "<why>"`, then bay up',
        "blocked": "fix the blockers above",
        "stale": "run bay plan again",
    }[verdict]
    lines.append(f"verdict: {verdict} (exit {plan['exit_code']}); {tail}")
    return "\n".join(lines)
