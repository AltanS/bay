"""Fleet inputs for ``bay compile``: the fleet file, lockfiles and project files.

A fleet repo holds:

``bay.fleet.toml``
    Hand-edited by the operator: fleet name, defaults, boxes, shared resources.
    ``format = 2`` says the fleet uses one folder per project. A CLI refuses
    a format newer than it knows (:data:`FLEET_FORMAT`).
``projects/<name>/bay.lock``
    Owned by the CLI (written by ``bay up``). Pins the repo, the commit, the
    box per environment and the adopted data names. JSON, version 2. The old
    flat form ``projects/<name>.lock`` is moved here by
    :func:`bay_cli.layout.migrate`.
``projects/<name>/bay.toml``
    Apps that live in the fleet (gatus, beszel), with the files they mount
    beside them. A repo project has only its ``bay.lock`` here.

This module only reads. It never opens anything under ``group_vars/<env>/``
(the encrypted secrets), and it reads ``group_vars/all/*.yml`` only to learn
which ``services:``/``accessories:`` keys an operator maintains by hand.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli import bay_toml
from bay_cli.bay_toml import Violation

FLEET_FILE = "bay.fleet.toml"
PROJECTS_DIR = "projects"
#: The fleet's config files; a directory mount of an adopted path lists it.
FILES_DIR = "files"
#: The lock of a project: ``projects/<name>/bay.lock``.
LOCK_FILE = "bay.lock"
#: The old flat lock ``projects/<name>.lock`` (format 1).
LOCK_SUFFIX = ".lock"
LOCK_VERSION = 2
#: The newest ``format`` of bay.fleet.toml this CLI knows. No key means 1.
FLEET_FORMAT = 2
#: ``from = "fleet:<path>"`` mounts ``<fleet>/files/<path>``.
FLEET_PREFIX = "fleet:"

_SCHEMAS = Path(__file__).parent / "schemas"
FLEET_SCHEMA_PATH = _SCHEMAS / "bay_fleet.schema.json"
LOCK_SCHEMA_PATH = _SCHEMAS / "bay_lock.schema.json"

#: The file `bay compile` writes, relative to the fleet root.
GENERATED_SERVICES = Path("group_vars") / "all" / "services.yml"


class FleetError(Exception):
    """One or more fleet inputs are invalid. ``lines`` holds one message each."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines


# ── Lockfile ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LockEnv:
    """One environment of a lockfile: the pinned box and the adopted names."""

    box: str | None = None
    database: str | None = None
    role: str | None = None
    volumes: dict[str, str] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    #: ``from`` path in bay.toml -> today's path under config/ and files/.
    files: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Lock:
    """``projects/<name>/bay.lock``.

    It names no path on this machine. A repo project's bay.toml is read from
    the checkout you stand in or from the fleet's repo cache
    (:mod:`bay_cli.reposource`), at ``commit``.
    """

    path: Path
    name: str
    repo: str | None
    commit: str | None
    toml_path: str
    envs: dict[str, LockEnv]

    def env(self, env: str) -> LockEnv:
        return self.envs.get(env, LockEnv())


def lock_file(fleet_root: Path, name: str) -> Path:
    """``<fleet>/projects/<name>/bay.lock``."""
    return fleet_root / PROJECTS_DIR / name / LOCK_FILE


def flat_lock_file(fleet_root: Path, name: str) -> Path:
    """The old form ``<fleet>/projects/<name>.lock``."""
    return fleet_root / PROJECTS_DIR / f"{name}{LOCK_SUFFIX}"


def upgrade_lock(raw: Any) -> Any:
    """A version 1 lock as version 2, in memory: ``local_path`` is dropped.

    Anything else is returned as it is (the schema check reports it).
    """
    if isinstance(raw, dict) and raw.get("lock_version") == 1:
        raw = {k: v for k, v in raw.items() if k != "local_path"}
        raw["lock_version"] = LOCK_VERSION
    return raw


def load_lock(path: Path, fleet_root: Path) -> Lock:
    """Read and check one ``projects/<name>/bay.lock``. Raise FleetError on any problem."""
    rel = _rel(path, fleet_root)
    try:
        raw = upgrade_lock(json.loads(path.read_text()))
    except OSError as exc:
        raise FleetError([f"{rel}: cannot read the file ({exc.strerror})"]) from exc
    except json.JSONDecodeError as exc:
        raise FleetError([f"{rel}: not valid JSON: {exc}"]) from exc
    schema = json.loads(LOCK_SCHEMA_PATH.read_text())
    problems = sorted(set(bay_toml.schema_violations(raw, schema)))
    if problems:
        raise FleetError([f"{rel}: {v}" for v in problems])
    stem = path.parent.name
    if raw["name"] != stem:
        raise FleetError([f"{rel}: name: is {raw['name']}, but the folder is named {stem}"])

    envs: dict[str, LockEnv] = {}
    for env, body in raw.get("envs", {}).items():
        adopted = body.get("adopted", {})
        envs[env] = LockEnv(
            box=body.get("box"),
            database=adopted.get("database"),
            role=adopted.get("role"),
            volumes=dict(adopted.get("volumes", {})),
            containers=dict(adopted.get("containers", {})),
            images=dict(adopted.get("images", {})),
            files=dict(adopted.get("files", {})),
        )
    return Lock(
        path=path,
        name=raw["name"],
        repo=raw.get("repo"),
        commit=raw.get("commit"),
        toml_path=raw.get("toml_path", "bay.toml"),
        envs=envs,
    )


# ── Fleet file ──────────────────────────────────────────────────────────────


def validate_fleet(doc: dict[str, Any]) -> list[Violation]:
    """Every violation in a parsed bay.fleet.toml, sorted."""
    schema = json.loads(FLEET_SCHEMA_PATH.read_text())
    found = set(bay_toml.schema_violations(doc, schema))
    boxes = doc.get("boxes") if isinstance(doc.get("boxes"), dict) else {}
    default_box = doc.get("default_box")
    if isinstance(default_box, str) and boxes and default_box not in boxes:
        found.add(Violation("default_box", f"there is no [boxes.{default_box}]"))
    raw_resources = doc.get("resources")
    resources: dict[str, Any] = raw_resources if isinstance(raw_resources, dict) else {}
    for key, res in resources.items():
        if not isinstance(res, dict):
            continue
        placed = res.get("box", default_box)
        for box in placed if isinstance(placed, list) else [placed]:
            if isinstance(box, str) and boxes and box not in boxes:
                found.add(Violation(f"resources.{key}.box", f"there is no [boxes.{box}]"))
        if "expose" in res and "port" not in res:
            found.add(Violation(f"resources.{key}.port", "is required when expose is set"))
    return sorted(found)


def load_fleet_file(fleet_root: Path) -> dict[str, Any]:
    path = fleet_root / FLEET_FILE
    if not path.is_file():
        raise FleetError([f"{FLEET_FILE}: not found in {fleet_root}"])
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise FleetError([f"{FLEET_FILE}: the file is not valid TOML: {exc}"]) from exc
    problems = validate_fleet(doc)
    if problems:
        raise FleetError([f"{FLEET_FILE}: {v}" for v in problems])
    found = fleet_format(doc)
    if found > FLEET_FORMAT:
        raise FleetError(
            [
                f"{FLEET_FILE}: format: is {found}, but this Bay knows formats up to "
                f"{FLEET_FORMAT}; run `bay self update`"
            ]
        )
    return doc


def fleet_format(doc: Mapping[str, Any]) -> int:
    """The ``format`` of a parsed bay.fleet.toml. No key means 1."""
    value = doc.get("format", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


# ── Projects ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Project:
    """One project as the compiler sees it: its bay.toml plus its lockfile."""

    name: str
    doc: dict[str, Any]
    toml_file: Path
    repo_root: Path
    lock: Lock | None

    @property
    def repo(self) -> str | None:
        return self.lock.repo if self.lock else None

    def lock_env(self, env: str) -> LockEnv:
        return self.lock.env(env) if self.lock else LockEnv()


@dataclass(frozen=True)
class FleetInputs:
    """Everything ``bay compile`` reads, already loaded and checked."""

    root: Path
    fleet: dict[str, Any]
    projects: dict[str, Project]
    #: ``services``/``accessories`` key -> the hand-maintained file that defines it.
    hand_keys: dict[str, str]


def load_inputs(
    fleet_root: Path,
    *,
    output: Path | None = None,
    checkouts: Mapping[str, Path] | None = None,
) -> FleetInputs:
    """Load the fleet file, every lockfile and every project bay.toml.

    ``output`` is the generated file; it is skipped when scanning
    ``group_vars/all`` for hand-maintained keys. ``checkouts`` maps a repo
    project (one with no ``projects/<name>/bay.toml``) to the directory that
    holds its repo files; ``bay plan`` passes the files it extracted at the
    pinned commit. All problems are collected and raised together as one
    FleetError.
    """
    errors: list[str] = []
    fleet: dict[str, Any] = {}
    try:
        fleet = load_fleet_file(fleet_root)
    except FleetError as exc:
        errors.extend(exc.lines)

    projects_dir = fleet_root / PROJECTS_DIR
    for flat in sorted(projects_dir.glob(f"*{LOCK_SUFFIX}")):
        errors.append(
            f"{_rel(flat, fleet_root)}: a lock in the old place; it belongs in "
            f"{PROJECTS_DIR}/{flat.stem}/{LOCK_FILE} (any bay plan or bay compile moves it)"
        )
    locks: dict[str, Lock] = {}
    for path in sorted(projects_dir.glob(f"*/{LOCK_FILE}")):
        try:
            loaded = load_lock(path, fleet_root)
        except FleetError as exc:
            errors.extend(exc.lines)
            continue
        locks[loaded.name] = loaded

    local_dirs = {p.parent.name: p for p in sorted(projects_dir.glob("*/bay.toml"))}
    projects: dict[str, Project] = {}
    for name in sorted(set(locks) | set(local_dirs)):
        lock = locks.get(name)
        in_fleet = local_dirs.get(name)
        checkout = (checkouts or {}).get(name)
        if in_fleet is not None:
            repo_root = in_fleet.parent
            toml_file = in_fleet
        elif checkout is not None:
            assert lock is not None
            repo_root = checkout
            toml_file = repo_root / lock.toml_path
        else:
            assert lock is not None
            what = (
                f"its repo {lock.repo} is not read here (bay compile and bay plan read it "
                "at the pinned commit)"
                if lock.repo
                else "the lock names no repo"
            )
            errors.append(
                f"{_rel(lock.path, fleet_root)}: there is no {PROJECTS_DIR}/{name}/bay.toml "
                f"and {what}"
            )
            continue
        project = _load_project(name, toml_file, repo_root, lock, fleet, fleet_root, errors)
        if project is not None:
            projects[name] = project

    hand_keys = _hand_keys(fleet_root, output, errors)
    if errors:
        raise FleetError(errors)
    return FleetInputs(root=fleet_root, fleet=fleet, projects=projects, hand_keys=hand_keys)


def _load_project(
    name: str,
    toml_file: Path,
    repo_root: Path,
    lock: Lock | None,
    fleet: dict[str, Any],
    fleet_root: Path,
    errors: list[str],
) -> Project | None:
    rel = _rel(toml_file, fleet_root)
    try:
        doc = bay_toml.load(toml_file)
    except bay_toml.BayTomlError as exc:
        errors.append(f"{rel}: {exc}")
        return None
    problems = bay_toml.validate(doc)
    if problems:
        errors.extend(f"{rel}: {v}" for v in problems)
        return None
    if doc["name"] != name:
        errors.append(f"{rel}: name: is {doc['name']}, but the fleet knows it as {name}")
        return None
    if fleet.get("name") and doc["fleet"] != fleet["name"]:
        errors.append(f"{rel}: fleet: is {doc['fleet']}, but this fleet is {fleet['name']}")
        return None
    if lock is not None:
        for env in sorted(set(lock.envs) - set(doc["deploy"])):
            errors.append(
                f"{_rel(lock.path, fleet_root)}: envs.{env}: bay.toml has no "
                f"[deploy.{env}]; remove the environment from the lockfile on purpose"
            )
    return Project(name=name, doc=doc, toml_file=toml_file, repo_root=repo_root, lock=lock)


def _hand_keys(fleet_root: Path, output: Path | None, errors: list[str]) -> dict[str, str]:
    import yaml

    class Loader(yaml.SafeLoader):
        """Reads ``!vault`` and any other tag as None instead of failing."""

    def _ignore(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
        return None

    Loader.add_multi_constructor("!", _ignore)

    keys: dict[str, str] = {}
    all_dir = fleet_root / "group_vars" / "all"
    skip = {(fleet_root / GENERATED_SERVICES).resolve()}
    if output is not None:
        skip.add(output.resolve())
    files = sorted([*all_dir.glob("*.yml"), *all_dir.glob("*.yaml")])
    for path in files:
        if path.resolve() in skip:
            continue
        try:
            data = yaml.load(path.read_text(), Loader=Loader)  # noqa: S506 (safe subclass)
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{_rel(path, fleet_root)}: cannot read the file: {exc}")
            continue
        if not isinstance(data, dict):
            continue
        for top in ("services", "accessories"):
            block = data.get(top)
            if isinstance(block, dict):
                for key in block:
                    keys.setdefault(str(key), _rel(path, fleet_root))
    return keys


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
