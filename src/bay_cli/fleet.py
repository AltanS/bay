"""Fleet inputs for ``bay compile``: the fleet file, lockfiles and project files.

A fleet repo holds:

``bay.fleet.toml``
    Hand-edited by the operator: fleet name, defaults, boxes, shared resources.
``projects/<name>.lock``
    Owned by the CLI (written by ``bay up`` later). Pins the repo, the commit,
    the box per environment and the adopted data names. JSON, version 1.
``projects/<name>/bay.toml``
    Apps that have no repo of their own (gatus, beszel) live here.

This module only reads. It never opens anything under ``group_vars/<env>/``
(the encrypted secrets), and it reads ``group_vars/all/*.yml`` only to learn
which ``services:``/``accessories:`` keys an operator maintains by hand.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli import bay_toml
from bay_cli.bay_toml import Violation

FLEET_FILE = "bay.fleet.toml"
PROJECTS_DIR = "projects"
LOCK_SUFFIX = ".lock"
LOCK_VERSION = 1

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


@dataclass(frozen=True)
class Lock:
    """``projects/<name>.lock``.

    ``local_path`` is a transition aid: until fetching by ``repo`` and
    ``commit`` exists, the compiler reads the pinned bay.toml from a local
    checkout. A relative ``local_path`` is resolved against the fleet root.
    """

    path: Path
    name: str
    repo: str | None
    commit: str | None
    toml_path: str
    local_path: Path | None
    envs: dict[str, LockEnv]

    def env(self, env: str) -> LockEnv:
        return self.envs.get(env, LockEnv())


def load_lock(path: Path, fleet_root: Path) -> Lock:
    """Read and check one lockfile. Raise FleetError on any problem."""
    rel = _rel(path, fleet_root)
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise FleetError([f"{rel}: cannot read the file ({exc.strerror})"]) from exc
    except json.JSONDecodeError as exc:
        raise FleetError([f"{rel}: not valid JSON: {exc}"]) from exc
    schema = json.loads(LOCK_SCHEMA_PATH.read_text())
    problems = sorted(set(bay_toml.schema_violations(raw, schema)))
    if problems:
        raise FleetError([f"{rel}: {v}" for v in problems])
    stem = path.name[: -len(LOCK_SUFFIX)]
    if raw["name"] != stem:
        raise FleetError([f"{rel}: name: is {raw['name']}, but the file is named {stem}.lock"])

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
        )
    local = raw.get("local_path")
    local_path = None
    if local:
        lp = Path(local).expanduser()
        local_path = lp if lp.is_absolute() else (fleet_root / lp)
    return Lock(
        path=path,
        name=raw["name"],
        repo=raw.get("repo"),
        commit=raw.get("commit"),
        toml_path=raw.get("toml_path", "bay.toml"),
        local_path=local_path,
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
    return doc


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


def load_inputs(fleet_root: Path, *, output: Path | None = None) -> FleetInputs:
    """Load the fleet file, every lockfile and every project bay.toml.

    ``output`` is the generated file; it is skipped when scanning
    ``group_vars/all`` for hand-maintained keys. All problems are collected
    and raised together as one FleetError.
    """
    errors: list[str] = []
    fleet: dict[str, Any] = {}
    try:
        fleet = load_fleet_file(fleet_root)
    except FleetError as exc:
        errors.extend(exc.lines)

    projects_dir = fleet_root / PROJECTS_DIR
    locks: dict[str, Lock] = {}
    for path in sorted(projects_dir.glob(f"*{LOCK_SUFFIX}")):
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
        if lock is not None and lock.local_path is not None and in_fleet is not None:
            errors.append(
                f"{_rel(lock.path, fleet_root)}: {name} has a bay.toml in "
                f"{_rel(in_fleet, fleet_root)} and a checkout at {lock.local_path}; keep one"
            )
            continue
        if lock is not None and lock.local_path is not None:
            repo_root = lock.local_path
            toml_file = repo_root / lock.toml_path
        elif in_fleet is not None:
            repo_root = in_fleet.parent
            toml_file = in_fleet
        else:
            assert lock is not None
            errors.append(
                f"{_rel(lock.path, fleet_root)}: no local_path and no "
                f"{PROJECTS_DIR}/{name}/bay.toml, so there is no bay.toml to read "
                "(fetching by repo and commit is not built yet)"
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
