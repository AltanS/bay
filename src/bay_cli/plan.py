"""``bay plan``: WANTED against PINNED and RUNNING, steps with a risk, one verdict.

The three truths:

WANTED
    ``bay.toml`` at the project's current commit (``git show <commit>:bay.toml``
    in the checkout the lock names). Uncommitted edits are not part of it.
PINNED
    ``projects/<name>.lock`` in the fleet, and the services file the fleet last
    compiled from it.
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
   the fleet's HEAD when it has none. ``bay compile`` uses the same copy.
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bay_cli import gitrepo, lockfile
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import FILES_DIR, FLEET_FILE, GENERATED_SERVICES, PROJECTS_DIR

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
    checkout: Path
    toml_path: str
    #: True for a project with no repo: its bay.toml is projects/<name>/bay.toml
    #: in the fleet, and ``checkout`` is the fleet root.
    in_fleet: bool = False

    @property
    def primary_env(self) -> str:
        return str(self.fleet.get("primary_env", "production"))

    @property
    def scope(self) -> str:
        """The fleet path whose last commit is an in-fleet project's commit."""
        return f"{PROJECTS_DIR}/{self.name}"


def load_fleet_doc(cx: Context) -> dict[str, Any]:
    from bay_cli.fleet import FleetError, load_fleet_file

    try:
        return load_fleet_file(cx.fleet_root)
    except FleetError as exc:
        raise BayError(
            "the fleet file is not valid:\n  " + "\n  ".join(exc.lines),
            code=ErrorCode.CONFIG_ERROR,
        ) from None


def load_project(cx: Context, name: str, *, expect_fleet: str | None = None) -> ProjectRef:
    fleet_doc = load_fleet_doc(cx)
    if expect_fleet is not None and fleet_doc.get("name") != expect_fleet:
        raise BayError(
            f"bay.toml names fleet {expect_fleet}, but {cx.fleet_root} is fleet "
            f"{fleet_doc.get('name')}",
            hint="Pass --fleet with the right fleet directory.",
        )
    lock_file = lockfile.lock_path(cx.fleet_root, name)
    try:
        raw = lockfile.read(lock_file)
    except ValueError as exc:
        raise BayError(f"projects/{name}.lock is not valid JSON: {exc}") from None
    in_dir = cx.fleet_root / PROJECTS_DIR / name / "bay.toml"
    if raw is None:
        if not in_dir.is_file():
            raise BayError(
                f"project {name} is not in fleet {fleet_doc.get('name')}",
                code=ErrorCode.NOT_FOUND,
                hint="Run `bay init` in the app repo first.",
            )
        raw = lockfile.new_lock(name, repo=None, local_path=None)
    checkout = lockfile.local_path(raw, cx.fleet_root)
    if checkout is None:
        if not in_dir.is_file():
            raise BayError(
                f"projects/{name}.lock names no local checkout, and there is no "
                f"projects/{name}/bay.toml in the fleet"
            )
        return ProjectRef(
            cx=cx,
            name=name,
            fleet=fleet_doc,
            lock_file=lock_file,
            lock=raw,
            checkout=cx.fleet_root,
            toml_path=f"{PROJECTS_DIR}/{name}/bay.toml",
            in_fleet=True,
        )
    return ProjectRef(
        cx=cx,
        name=name,
        fleet=fleet_doc,
        lock_file=lock_file,
        lock=raw,
        checkout=checkout,
        toml_path=str(raw.get("toml_path", "bay.toml")),
    )


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
    """
    record = (proj.lock.get("envs") or {}).get(env, {})
    deploy = ((doc or {}).get("deploy") or {}).get(env, {})
    box = record.get("box") or deploy.get("box") or proj.fleet.get("default_box")
    boxes = proj.fleet.get("boxes") or {}
    if not box or box not in boxes:
        return (str(box) if box else None), None
    return str(box), str(boxes[box]["env"])


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
    dirty = gitrepo.path_dirty(checkout, proj.scope) if proj.in_fleet else gitrepo.dirty(checkout)
    ref = at or "HEAD"
    commit = gitrepo.resolve_commit(checkout, ref)
    if commit is None:
        what = f"commit {at}" if at else "HEAD"
        return Wanted(None, None, None, dirty, [f"{what} is not reachable in {checkout}"])
    if proj.in_fleet:
        # WANTED is the fleet's committed file; its commit is the last one that
        # touched projects/<name>/ (the bay.toml or a file it mounts).
        changed = gitrepo.last_change(checkout, proj.scope, commit)
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
    full = gitrepo.resolve_commit(proj.checkout, commit)
    data = gitrepo.show_file(proj.checkout, full, proj.toml_path) if full else None
    if data is None:
        return None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


# ── Compiling a copy of the fleet ───────────────────────────────────────────


@dataclass
class Compiled:
    result: Any  # compiler.CompileResult | None
    errors: list[str]
    workdir: Path
    notes: list[str] = field(default_factory=list)

    def services_file(self) -> Path | None:
        if self.result is None:
            return None
        path = self.workdir / "compiled-services.yml"
        if not path.exists():
            path.write_text(self.result.text())
        return path


def _mount_sources(doc: Mapping[str, Any]) -> list[str]:
    levels = [doc, *(v for v in (doc.get("services") or {}).values() if isinstance(v, dict))]
    out: list[str] = []
    for level in levels:
        for mount in level.get("mounts") or []:
            if isinstance(mount, dict) and isinstance(mount.get("from"), str):
                out.append(mount["from"].rstrip("/"))
    return out


@dataclass
class _Copy:
    root: Path
    problems: list[str]
    notes: list[str]
    labels: dict[str, str]


def _materialize(cx: Context, tmp: Path, pins: Mapping[str, str]) -> _Copy:
    """Copy the fleet inputs to ``tmp/fleet`` with every project read at its pin.

    * A project with a checkout is read at its lock ``commit`` (or its pin in
      ``pins``). With no commit yet it is left out, with a note.
    * A project in the fleet (``projects/<name>/``) is read from the fleet
      repo at its lock ``commit``; with no lock or no commit, at the fleet's
      HEAD, with a note. The working tree is never read.
    * The fleet's ``files/`` tree (the config files a directory mount expands
      into) is read at the fleet's HEAD. Leave it out and the compiler cannot
      list a mounted directory.

    Every path the compiler and ``fleet.load_inputs`` read from the fleet root
    is copied here: ``bay.fleet.toml``, ``group_vars/all/*.y*ml``,
    ``projects/`` (``bay.toml`` and ``*.lock``) and ``files/``.
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
    for lock in sorted(projects_src.glob("*.lock")) if projects_src.is_dir() else []:
        try:
            raw = lockfile.read(lock)
        except (OSError, ValueError) as exc:
            out.problems.append(f"projects/{lock.name}: {exc}")
            continue
        if raw is not None:
            locks[lock.name[: -len(".lock")]] = raw

    fleet_head = gitrepo.head(src)
    _copy_files_tree(src, fleet_head, out)
    for child in sorted(projects_src.iterdir()) if projects_src.is_dir() else []:
        if child.is_dir() and (child / "bay.toml").is_file():
            _copy_in_fleet(src, fleet_head, child.name, pins, locks.get(child.name), out)

    for stem, raw in locks.items():
        name = str(raw.get("name") or stem)
        checkout = lockfile.local_path(raw, src)
        if checkout is None:
            shutil.copy2(projects_src / f"{stem}.lock", projects_dst / f"{stem}.lock")
            continue
        commit = pins.get(name) or raw.get("commit")
        if not commit:
            out.notes.append(f"{name} has no pinned commit yet, so it is left out")
            continue
        if not gitrepo.is_repo(checkout):
            out.problems.append(f"{name}: the checkout {checkout} is not a git repo")
            continue
        full = gitrepo.resolve_commit(checkout, str(commit))
        if full is None:
            out.problems.append(
                f"{name}: commit {str(commit)[:12]} is not in the checkout {checkout}"
            )
            continue
        toml_rel = str(raw.get("toml_path", "bay.toml"))
        data = gitrepo.show_file(checkout, full, toml_rel)
        if data is None:
            out.problems.append(f"{name}: {toml_rel} is not in commit {full[:12]}")
            continue
        try:
            doc = tomllib.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            doc = {}
        rels = [toml_rel, *_mount_sources(doc)]
        rels = [r for r in dict.fromkeys(rels) if gitrepo.has_path(checkout, full, r)]
        dest = tmp / "checkouts" / name
        try:
            gitrepo.extract(checkout, full, rels, dest)
        except gitrepo.GitError as exc:
            out.problems.append(f"{name}: cannot read commit {full[:12]}: {exc}")
            continue
        out.labels[str(dest)] = f"{name}@{full[:12]}"
        pinned = copy.deepcopy(raw)
        pinned["local_path"] = str(dest)
        pinned["commit"] = full
        (projects_dst / f"{stem}.lock").write_text(json.dumps(pinned, indent=2) + "\n")
    return out


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
    if lock is not None and lockfile.local_path(dict(lock), src) is not None:
        return  # fleet.load_inputs reports the double definition
    if fleet_head is None:
        out.notes.append(f"{name}: the fleet is not a git repo, so its files are read as they are")
        shutil.copytree(src / scope, out.root / scope, symlinks=True)
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


@contextmanager
def compiled_fleet(cx: Context, pins: Mapping[str, str] | None = None) -> Iterator[Compiled]:
    """Compile the fleet with every project at its pin (``pins`` overrides some)."""
    from bay_cli import compiler
    from bay_cli.fleet import FleetError, load_inputs

    with tempfile.TemporaryDirectory(prefix="bay-plan-") as tmp:
        work = Path(tmp)
        made = _materialize(cx, work, pins or {})
        errors = list(made.problems)
        result = None
        if not errors:
            try:
                result = compiler.compile_fleet(load_inputs(made.root))
            except (FleetError, compiler.CompileError) as exc:
                errors.extend(exc.lines)
        cleaned = []
        for line in errors:
            for path, label in made.labels.items():
                line = line.replace(path, label)
            cleaned.append(line.replace(str(made.root) + "/", ""))
        yield Compiled(result=result, errors=cleaned, workdir=work, notes=made.notes)


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


def _container_steps(
    name: str,
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    *,
    project: str,
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
    project: str,
    mine: set[str],
    resources: set[str],
    running: set[str] | None,
) -> list[dict[str, Any]]:
    """Steps that turn the fleet's services file into the wanted one."""
    old_all, new_all = _entries(current), _entries(wanted)
    steps: list[dict[str, Any]] = []
    for name in sorted(set(old_all) | set(new_all)):
        old, new = old_all.get(name), new_all.get(name)
        if old == new:
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
        else:
            if new is None:
                steps.append(
                    _step(
                        "container",
                        "remove",
                        "destructive",
                        f"container of another project removed; bay up for "
                        f"{project} would write that too",
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
                        f"container of another project changes; bay up for "
                        f"{project} would write that too",
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
    """A change of the fleet's tailnet allowlist since the fleet's last commit."""
    data = gitrepo.show_file(cx.fleet_root, "HEAD", f"./{FLEET_FILE}")
    if data is None:
        return None
    try:
        before = tomllib.loads(data.decode("utf-8")).get("tailnet")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    if before == fleet_doc.get("tailnet"):
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
                    row = {k: c.get(k) for k in ("name", "image", "config_hash")}
                    box["containers"].append(row)
                    rows.append([entry.get("box"), row["name"], row["image"], row["config_hash"]])
        boxes.append(box)
    digest = None
    if seen_receipt:
        canon = json.dumps(sorted(rows, key=lambda r: [str(x) for x in r]), sort_keys=True)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return {"checked": True, "receipt_sha256": digest, "boxes": boxes}


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
    cx: Context, box_env: str, services_file: Path
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
                "--check",
            ],
        )
        return read_box_predictions(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(real_reports, ignore_errors=True)


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
    project: str,
    mine: set[str],
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
                project=project if c["name"] in mine else None,
                source="box",
            )
        )
    return steps


# ── Plan ────────────────────────────────────────────────────────────────────


@dataclass
class PlanOptions:
    env: str | None = None
    at: str | None = None
    read_running: bool = True
    box_check: bool = False
    allow_unsupported: bool = False
    cwd_repo: Path | None = None


_UNHASHED = ("plan_id", "plan_sha256", "created_at", "verdict", "exit_code", "approval", "stale")


def body_sha256(plan: Mapping[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in _UNHASHED}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_plan(
    proj: ProjectRef,
    opts: PlanOptions,
    *,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """Build the plan body, then its id, approval and verdict. Does not save it."""
    from bay_cli import secrets_check

    cx = proj.cx
    env = opts.env or proj.primary_env
    blockers: list[str] = []
    notes: list[str] = []

    # Fleet
    fleet_is_git = gitrepo.is_repo(cx.fleet_root)
    fleet_head = gitrepo.head(cx.fleet_root) if fleet_is_git else None
    fleet_dirty = gitrepo.dirty(cx.fleet_root, exclude=(PLANS_DIR,)) if fleet_is_git else None
    behind: bool | None = None
    if not fleet_is_git or fleet_head is None:
        blockers.append(f"the fleet {cx.fleet_root} is not a git repo with a commit")
    else:
        behind, problem = gitrepo.behind_remote(cx.fleet_root)
        if problem:
            blockers.append(problem)
        elif behind:
            blockers.append("the fleet repo is behind its remote; pull it first")

    # WANTED
    wanted = read_wanted(proj, opts.at)
    blockers.extend(wanted.problems)
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
        notes.append(
            f"this command ran in {opts.cwd_repo}, but the fleet reads the project from "
            f"{proj.checkout}"
        )
    if wanted.doc is not None:
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

    box, box_env = resolve_box(proj, env, wanted.doc)
    if box_env is None:
        blockers.append(f"box {box} is not in bay.fleet.toml [boxes]")
    wanted_box = ((wanted.doc or {}).get("deploy") or {}).get(env, {}).get("box")
    if wanted_box and box and wanted_box != box:
        notes.append(
            f"bay.toml asks for box {wanted_box}, but the fleet pins box {box}; the fleet decides"
        )

    # PINNED
    lock_sha = lockfile.sha256_of(proj.lock_file)
    pinned_commit = lockfile.env_pin(proj.lock, env)
    pinned_doc = doc_at(proj, proj.lock.get("commit"))
    current, state = current_services(cx)
    if state == "foreign":
        blockers.append("the fleet's services file was not written by bay; run `bay import` first")
    elif state == "edited":
        blockers.append(
            "the fleet's services file was edited by hand since the last compile; "
            "move the change into bay.toml or bay.fleet.toml"
        )

    names_by_env: dict[str, dict[str, str]] = {}
    for doc in (pinned_doc, wanted.doc):
        if doc:
            for e, names in project_containers(proj.name, doc, proj.lock, proj.primary_env).items():
                names_by_env.setdefault(e, {}).update(names)
    mine = {n for names in names_by_env.values() for n in names.values()}
    env_names = set(names_by_env.get(env, {}).values())

    # RUNNING
    running: dict[str, Any] = {"checked": False, "receipt_sha256": None, "boxes": []}
    if opts.read_running and box_env is not None:
        reader = read_receipts or default_receipt_reader
        running = running_slice(reader(cx, box_env), env_names)
        for b in running["boxes"]:
            if b.get("error"):
                notes.append(f"box {b.get('box') or box_env}: {b['error']}")
    elif not opts.read_running:
        notes.append("the box receipt was not read (--no-remote)")

    # Compile WANTED and diff
    steps: list[dict[str, Any]] = []
    unsupported: list[str] = []
    missing: list[dict[str, Any]] = []
    box_checked = False
    prediction: dict[str, Any] = {"checked": False, "containers": [], "errors": []}
    pins = {proj.name: wanted.commit} if wanted.commit else {}
    if wanted.doc is not None and not wanted.problems:
        with compiled_fleet(cx, pins) as comp:
            blockers.extend(comp.errors)
            notes.extend(comp.notes)
            if comp.result is not None:
                unsupported = [str(u) for u in comp.result.unsupported]
                if unsupported and not opts.allow_unsupported:
                    blockers.append(
                        f"{len(unsupported)} bay.toml feature(s) cannot be deployed yet "
                        "(see unsupported); remove them or pass --allow-unsupported"
                    )
                wanted_data = comp.result.data()
                steps = diff_steps(
                    current,
                    wanted_data,
                    project=proj.name,
                    mine=mine,
                    resources=set((proj.fleet.get("resources") or {}).keys()),
                    running=_running_names(running),
                )
                if box_env is not None:
                    missing, problem = _missing_secrets(
                        cx, box_env, wanted_data, env_names, secrets_check
                    )
                    if problem:
                        blockers.append(problem)
                    for m in missing:
                        blockers.append(
                            f"secret {m['name']} is missing in the fleet for "
                            f"{box_env} (used by {', '.join(m['used_by'])})"
                        )
                if opts.box_check and box_env is not None and not blockers:
                    services_file = comp.services_file()
                    assert services_file is not None
                    try:
                        entries = (check_box or default_box_check)(cx, box_env, services_file)
                        box_checked = True
                    except (BayError, OSError, SystemExit) as exc:
                        blockers.append(f"the check on the box failed: {exc}")
                    else:
                        prediction = box_prediction(entries)
                        blockers.extend(
                            f"the check on the box gave no prediction: {e}"
                            for e in prediction["errors"]
                        )
                        if not prediction["containers"] and not prediction["errors"]:
                            blockers.append("the check on the box gave no prediction")
                        explained = {str(s["container"]) for s in steps if s["container"]}
                        steps.extend(box_steps(prediction, explained, project=proj.name, mine=mine))
    if fleet_is_git:
        t_step = tailnet_step(cx, proj.fleet)
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
    if fleet_dirty and any(s["risk"] == "destructive" for s in steps):
        blockers.append(
            "the fleet repo has uncommitted changes and a step is destructive; "
            "commit or drop the changes first"
        )

    plan: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "plan_id": "",
        "plan_sha256": "",
        "created_at": _now(),
        "project": proj.name,
        "env": env,
        "box": box,
        "box_env": box_env,
        "fleet": {
            "name": proj.fleet.get("name"),
            "commit": fleet_head,
            "dirty": fleet_dirty,
            "behind": behind,
        },
        "wanted": {
            "commit": wanted.commit,
            "toml_sha256": wanted.toml_sha256,
            "dirty": wanted.dirty,
        },
        "pinned": {"commit": pinned_commit, "lock_sha256": lock_sha},
        "running": running,
        "box_checked": box_checked,
        "box_prediction": prediction,
        "steps": steps,
        "unsupported": unsupported,
        "missing_secrets": missing,
        "blockers": blockers,
        "notes": notes,
        "verdict": "",
        "exit_code": 0,
        "approval": None,
        "stale": [],
    }
    sha = body_sha256(plan)
    plan["plan_sha256"] = sha
    plan["plan_id"] = sha[:12]
    plan["approval"] = find_approval(cx, plan)
    decide(plan)
    return plan


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


def stale_reasons(saved: Mapping[str, Any], fresh: Mapping[str, Any]) -> list[str]:
    """Why a saved plan no longer holds. Empty when it still does."""
    reasons: list[str] = []
    if saved["pinned"]["lock_sha256"] != fresh["pinned"]["lock_sha256"]:
        reasons.append(f"the pin moved: projects/{saved['project']}.lock changed since the plan")
    s_run, f_run = saved["running"], fresh["running"]
    if (
        s_run.get("checked")
        and f_run.get("checked")
        and (s_run.get("receipt_sha256") != f_run.get("receipt_sha256"))
    ):
        reasons.append("the box changed: its receipt for this project differs from the plan")
    if not reasons and saved["plan_id"] != fresh["plan_id"]:
        changed = [
            k
            for k in ("fleet", "wanted", "steps", "running", "blockers")
            if saved.get(k) != fresh.get(k)
        ]
        reasons.append("the plan inputs changed since the plan: " + ", ".join(changed or ["?"]))
    return reasons


def recheck(
    proj: ProjectRef,
    saved: Mapping[str, Any],
    opts: PlanOptions,
    *,
    read_receipts: ReceiptReader | None = None,
    check_box: BoxCheck | None = None,
) -> dict[str, Any]:
    """Plan again with the saved plan's env and commit. Mark it stale when it moved."""
    if saved.get("project") != proj.name:
        raise BayError(
            f"plan {saved.get('plan_id')} is for project {saved.get('project')}, not {proj.name}"
        )
    again = PlanOptions(
        env=str(saved["env"]),
        at=saved["wanted"]["commit"],
        read_running=opts.read_running or bool(saved["running"].get("checked")),
        box_check=opts.box_check,
        allow_unsupported=opts.allow_unsupported,
        cwd_repo=opts.cwd_repo,
    )
    fresh = make_plan(proj, again, read_receipts=read_receipts, check_box=check_box)
    fresh["stale"] = stale_reasons(saved, fresh)
    decide(fresh)
    return fresh


# ── Human output ────────────────────────────────────────────────────────────


def render(plan: Mapping[str, Any]) -> str:
    def short(sha: Any) -> str:
        return str(sha)[:12] if sha else "none"

    w, p, r = plan["wanted"], plan["pinned"], plan["running"]
    lines = [
        f"plan {plan['plan_id']}  {plan['project']} {plan['env']} -> box {plan['box']} "
        f"({plan['box_env']})",
        f"  WANTED  {short(w['commit'])}{' (uncommitted changes ignored)' if w['dirty'] else ''}",
        f"  PINNED  {short(p['commit'])}",
        "  RUNNING "
        + (
            f"receipt {short(r['receipt_sha256'])}"
            if r["checked"] and r["receipt_sha256"]
            else "no receipt"
            if r["checked"]
            else "not read"
        ),
        "",
    ]
    if plan["steps"]:
        rows = [("STEP", "FROM", "RISK", "ACTION", "WHAT", "WHY")]
        for s in plan["steps"]:
            what = s["kind"] + " " + (s["resource"] or s["container"] or "")
            if s["kind"] in ("volume", "database", "database_user", "secret") and s["container"]:
                what += f" ({s['container']})"
            rows.append(
                (s["id"], s.get("source", "compile"), s["risk"], s["action"], what, s["reason"])
            )
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        for row in rows:
            lines.append(
                "  " + "  ".join(row[i].ljust(widths[i]) for i in range(5)) + "  " + row[5]
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
