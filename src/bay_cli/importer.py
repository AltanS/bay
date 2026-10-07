"""``bay import``: read today's fleet and write bay.fleet.toml, bay.toml files and lockfiles.

The importer is read-only against the fleet it reads. It loads the YAML under
``group_vars/all/`` and the box groups' plain files (never a ``secrets.yml``,
never a file that holds ``$ANSIBLE_VAULT``), plus ``hosts/``. It writes only
into the output directory:

* ``bay.fleet.toml``: boxes, shared resources (today's ``accessories:``), the
  webhook, the repo tokens and the tailnet routes (today's
  ``tailnet_proxies``, names kept; :func:`bay_cli.routes.from_proxies`).
* ``projects/<name>/bay.toml`` for every project, with the config files it
  mounts beside it (``files/<name>/<path>`` today becomes
  ``projects/<name>/<path>``, and ``from = "<path>"``).
* ``projects/<name>/bay.lock``: ``repo``, ``commit: null`` and, per
  environment, the box and every adopted name (containers, images, volumes,
  database, database user, config paths), so the compile reproduces today's
  names exactly.
* ``files/``: a copy of every other config file a container reads today: a
  path outside ``<name>/`` (adopted in the lock) and the resources' files.
* ``format = 2`` in ``bay.fleet.toml``: one folder per project.

Grouping (see :func:`_plan_projects`): a ``services:`` key becomes a project
with that key as ``name``. ``<name>-prod``/``-staging``/``-dev`` keys are an
environment of ``<name>``. ``<name>-<suffix>`` with the same repo (or the same
image) becomes ``[services.<suffix>]`` when that is exact. Every decision, and
every value the importer cannot carry over exactly (a FLAG), lands in the
report. Secret values are never read and never printed.

Environment names: the main environment is named after the group of the box
it runs on (``boxes.<b>.env``), so ``[deploy.<env>]`` matches the group a
deploy targets. With one group in the fleet, that group is also
``primary_env`` in bay.fleet.toml (see :meth:`_Importer._env_names`).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli.bay_toml import sibling_var

#: Environment suffixes on a services.yml key: suffix -> bay.toml environment.
ENV_SUFFIXES = {
    "prod": "production",
    "production": "production",
    "staging": "staging",
    "stage": "staging",
    "dev": "dev",
}
PRIMARY_ENV = "production"

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_SECRET_REF_RE = re.compile(
    r"^\{\{\s*secrets(?:\.([A-Za-z0-9_]+)|\[['\"]([A-Za-z0-9_]+)['\"]\])\s*\}\}$"
)
_CONFIG_BIND_RE = re.compile(r"^\{\{\s*stack_dir\s*\}\}/config/([^:]+):(/[^:]*)(?::(ro|rw))?$")
_URL_RE = re.compile(r"^http://([a-z0-9][a-z0-9.-]*):([0-9]{1,5})$")
_SIZE_RE = re.compile(r"^[1-9][0-9]*[kmg]$")
_VOLUME_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

#: services.yml keys the importer maps. Anything else is reported as dropped.
_SERVICE_KEYS = {
    "access",
    "build",
    "command",
    "config_files",
    "config_files_mode",
    "database",
    "depends_on",
    "domains",
    "env",
    "healthcheck_path",
    "image",
    "log_retention",
    "log_rotation",
    "mem_limit",
    "memswap_limit",
    "middleware",
    "ports",
    "public_routes",
    "regions",
    "replicas",
    "update",
    "volumes",
    "vpn_routes",
    "zero_downtime",
}
_ACCESSORY_KEYS = {
    "backup",
    "command",
    "config_files",
    "config_files_mode",
    "env",
    "expose",
    "healthcheck",
    "image",
    "mem_limit",
    "memswap_limit",
    "network_mode",
    "port",
    "regions",
    "update",
    "volumes",
}
_BUILD_KEYS = {
    "args",
    "branch",
    "context",
    "dockerfile",
    "paths",
    "repo",
    "secrets",
    "strategy",
    "token",
}
_UPDATE = {"auto": "auto", False: "off", "monitor": "notify", None: "notify"}


class ImportError_(Exception):
    """The fleet cannot be read. ``lines`` holds one message each."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__("\n".join(lines))
        self.lines = lines


# ── Reading today's fleet ───────────────────────────────────────────────────


def _yaml_loader() -> type:
    import yaml

    class Loader(yaml.SafeLoader):
        """Reads ``!vault`` and any other tag as None instead of failing."""

    def _ignore(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> None:
        return None

    Loader.add_multi_constructor("!", _ignore)
    return Loader


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith("secrets.") or "vault" in name


def _read_yaml(path: Path) -> Any:
    import yaml

    text = path.read_text()
    if text.lstrip().startswith("$ANSIBLE_VAULT"):
        return None
    return yaml.load(text, Loader=_yaml_loader())  # noqa: S506 (safe subclass)


@dataclass
class Legacy:
    """Today's fleet, as the importer reads it."""

    root: Path
    services: dict[str, dict[str, Any]]
    accessories: dict[str, dict[str, Any]]
    webhook: dict[str, Any] | None
    tailnet_proxies: dict[str, Any] | None
    #: inventory group -> member hosts (direct) and child groups
    hosts: dict[str, list[str]]
    children: dict[str, list[str]]
    #: group -> plain variables from that group's YAML files (``all`` included)
    group_vars: dict[str, dict[str, Any]]
    #: the group whose directory holds the encrypted secrets file
    env_group: str
    #: services.yml text of every file that defines services (for template scans)
    texts: list[str] = field(default_factory=list)

    def var_names(self) -> set[str]:
        out: set[str] = set()
        for text in self.texts:
            out |= set(_VAR_RE.findall(text))
        return out - {"stack_dir", "stack_name"}


def load_legacy(root: Path) -> Legacy:
    """Read a fleet in today's layout. Raise ImportError_ when it is not one."""
    gv = root / "group_vars"
    all_dir = gv / "all"
    if not all_dir.is_dir():
        raise ImportError_(
            [f"{root}: no shared variables directory; this is not a fleet in the YAML layout"]
        )
    services: dict[str, dict[str, Any]] = {}
    accessories: dict[str, dict[str, Any]] = {}
    webhook = None
    proxies = None
    texts: list[str] = []
    group_vars: dict[str, dict[str, Any]] = {}
    for gdir in sorted(p for p in gv.iterdir() if p.is_dir()):
        merged: dict[str, Any] = {}
        for path in sorted([*gdir.glob("*.yml"), *gdir.glob("*.yaml")]):
            if _is_secret_file(path):
                continue
            data = _read_yaml(path)
            if not isinstance(data, dict):
                continue
            if gdir.name == "all" and any(
                k in data for k in ("services", "accessories", "webhook")
            ):
                texts.append(path.read_text())
                for key, value in (data.get("services") or {}).items():
                    services[str(key)] = value or {}
                for key, value in (data.get("accessories") or {}).items():
                    accessories[str(key)] = value or {}
                if data.get("webhook"):
                    webhook = data["webhook"]
            if "tailnet_proxies" in data:
                proxies = data["tailnet_proxies"]
            merged.update(
                {
                    k: v
                    for k, v in data.items()
                    if k not in ("services", "accessories", "webhook", "tailnet_proxies")
                }
            )
        group_vars[gdir.name] = merged

    hosts, children = _read_inventory(root / "hosts")
    env_group = next(
        (
            g
            for g in sorted(hosts)
            if any((gv / g / n).is_file() for n in ("secrets.yml", "secrets.yaml"))
        ),
        next(iter(sorted(hosts)), "production"),
    )
    return Legacy(
        root=root,
        services=services,
        accessories=accessories,
        webhook=webhook,
        tailnet_proxies=proxies,
        hosts=hosts,
        children=children,
        group_vars=group_vars,
        env_group=env_group,
        texts=texts,
    )


def _read_inventory(hosts_dir: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    hosts: dict[str, list[str]] = {}
    children: dict[str, list[str]] = {}
    files = sorted(p for p in hosts_dir.iterdir() if p.is_file()) if hosts_dir.is_dir() else []
    for path in files:
        group, kind = None, "hosts"
        for raw in path.read_text().splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            match = re.match(r"^\[([^\]:]+)(?::(children|vars))?\]$", line)
            if match:
                group, kind = match.group(1), match.group(2) or "hosts"
                if kind == "children":
                    children.setdefault(group, [])
                elif kind == "hosts":
                    hosts.setdefault(group, [])
                continue
            if group is None or kind == "vars":
                continue
            name = line.split()[0]
            (children if kind == "children" else hosts).setdefault(group, []).append(name)
    for group in children:
        hosts.setdefault(group, [])
    return hosts, children


# ── Result ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecretMove:
    """A secret the compiled fleet reads under a new name.

    ``source`` is ``("literal", <yaml path>)`` for a value written in plain text
    today, or ``("secret", <old name>)``. ``value`` holds the literal for the
    round-trip gate only; it is never printed or written.
    """

    name: str
    source: tuple[str, str]
    value: str | None = field(default=None, repr=False, compare=False)


@dataclass
class Box:
    name: str
    env: str
    group: str | None
    tailnet_ip: str | None
    #: inventory groups a box of this kind is in (for the round-trip render)
    groups: list[str]
    #: plain variables in scope on this box (all, then parents, then the group)
    variables: dict[str, Any]


@dataclass
class ImportResult:
    fleet_name: str
    boxes: dict[str, Box]
    files: dict[str, str] = field(default_factory=dict)
    copies: dict[str, Path] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    groupings: list[str] = field(default_factory=list)
    needs_pairs: list[tuple[str, str, str]] = field(default_factory=list)
    fleet_secret_aliases: int = 0
    flags: list[str] = field(default_factory=list)
    secret_moves: list[SecretMove] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def report_lines(self) -> list[str]:
        out = [
            f"fleet {self.fleet_name}: {len(self.projects)} project(s), "
            f"{len(self.resources)} resource(s), {len(self.boxes)} box(es)",
        ]
        out += [f"grouping: {g}" for g in self.groupings]
        out += [f"needs: {c} needs {t} as {v}" for c, t, v in self.needs_pairs]
        out.append(f"fleet_secrets aliases: {self.fleet_secret_aliases}")
        for move in self.secret_moves:
            kind, where = move.source
            origin = f"the plain value at {where}" if kind == "literal" else f"the value of {where}"
            out.append(f"new secret: add {move.name} with {origin}")
        out += [f"note: {n}" for n in self.notes]
        out += [f"FLAG: {f}" for f in self.flags]
        return out

    def write(self, out: Path) -> None:
        out.mkdir(parents=True, exist_ok=True)
        for rel, text in sorted(self.files.items()):
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for rel, src in sorted(self.copies.items()):
            path = out / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, path)


# ── Import ──────────────────────────────────────────────────────────────────


def import_fleet(root: Path, name: str | None = None) -> ImportResult:
    """Read the fleet at ``root`` and build the new layout in memory."""
    legacy = load_legacy(root)
    return _Importer(legacy, name or root.name).run()


@dataclass
class _Member:
    """One services.yml key inside a project."""

    key: str
    svc: dict[str, Any]
    env: str
    service: str  # "web" for the main container


@dataclass
class _Plan:
    name: str
    members: list[_Member]


class _Importer:
    def __init__(self, legacy: Legacy, name: str) -> None:
        self.lg = legacy
        self.name = _slug(name)
        self.flags: list[str] = []
        self.notes: list[str] = []
        self.groupings: list[str] = []
        self.moves: dict[str, SecretMove] = {}
        self.aliases = 0
        self.needs_pairs: list[tuple[str, str, str]] = []
        self.copies: dict[str, Path] = {}
        self.boxes = self._boxes()
        self.default_box = self._default_box()
        #: the box groups a deploy targets (``boxes.<b>.env`` in bay.fleet.toml)
        self.groups = sorted({b.env for b in self.boxes.values()})
        self.res_ports: dict[str, int] = {}

    # ── boxes ───────────────────────────────────────────────────────────
    def _boxes(self) -> dict[str, Box]:
        lg = self.lg
        used: list[str] = []
        for block in (*lg.services.values(), *lg.accessories.values()):
            for g in block.get("regions") or []:
                if g not in used:
                    used.append(g)
        env_group = lg.env_group
        boxes: dict[str, Box] = {}
        if not used:
            boxes[env_group] = Box(
                name=env_group,
                env=env_group,
                group=None,
                tailnet_ip=None,
                groups=[env_group],
                variables=self._vars([env_group]),
            )
            return boxes
        for g in sorted(used):
            if g not in lg.hosts:
                self.flags.append(
                    f"box group {g} is named by a container, but no box belongs to it"
                )
            parents = [p for p, kids in lg.children.items() if g in kids]
            env = parents[0] if parents else env_group
            variables = self._vars([*parents, g])
            ip = variables.get("headscale_server_tailnet_ip")
            boxes[g] = Box(
                name=g,
                env=env,
                group=g,
                tailnet_ip=ip if isinstance(ip, str) and re.match(r"^[0-9.]+$", ip) else None,
                groups=[*parents, g],
                variables=variables,
            )
        return boxes

    def _vars(self, groups: list[str]) -> dict[str, Any]:
        merged: dict[str, Any] = dict(self.lg.group_vars.get("all", {}))
        for g in groups:
            merged.update(self.lg.group_vars.get(g, {}))
        return merged

    def _default_box(self) -> str:
        counts: dict[str, int] = {b: 0 for b in self.boxes}
        for svc in self.lg.services.values():
            for b in self._boxes_of(svc, "x"):
                counts[b] = counts.get(b, 0) + 1
        return max(sorted(counts), key=lambda b: counts[b])

    def _boxes_of(self, block: dict[str, Any], who: str) -> list[str]:
        regions = block.get("regions") or []
        if not regions:
            return [self.default_box_or_only()]
        return [g for g in regions if g in self.boxes]

    def default_box_or_only(self) -> str:
        return getattr(self, "default_box", None) or sorted(self.boxes)[0]

    # ── templating (plain variables only) ───────────────────────────────
    def _resolve(self, value: Any, box: str, where: str) -> Any:
        if isinstance(value, dict):
            return {k: self._resolve(v, box, f"{where}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, box, f"{where}[{i}]") for i, v in enumerate(value)]
        if not isinstance(value, str) or "{{" not in value:
            return value
        variables = self.boxes[box].variables

        def sub(m: re.Match[str]) -> str:
            var = m.group(1)
            if var in ("stack_dir", "stack_name"):
                return m.group(0)
            val = variables.get(var)
            if isinstance(val, (str, int)) and "{{" not in str(val):
                return str(val)
            self.flags.append(
                f"{where}: {{{{ {var} }}}} is not a plain variable on box {box}; kept as text"
            )
            return m.group(0)

        return _VAR_RE.sub(sub, value)

    # ── run ─────────────────────────────────────────────────────────────
    def run(self) -> ImportResult:
        plans = self._plan_projects()
        main_of: dict[str, tuple[str, int | None, str]] = {}
        for plan in plans:
            for m in plan.members:
                if m.service == "web":
                    main_of[m.key] = (plan.name, _port(m.svc), self._box(m.svc, m.key))
        self.main_of = main_of
        self.kinds = self._resource_kinds()

        files: dict[str, str] = {}
        published: set[str] = set()
        project_docs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for plan in plans:
            doc, lock = self._project(plan)
            project_docs[plan.name] = (doc, lock)
            for need in _need_names(doc):
                published.add(need)
        for pname, (doc, lock) in sorted(project_docs.items()):
            if pname in published:
                doc["publish"] = True
            files[f"projects/{pname}/bay.toml"] = to_toml(_order_project(doc))
            files[f"projects/{pname}/bay.lock"] = (
                json.dumps(lock, indent=2, sort_keys=False) + "\n"
            )

        resources = {k: self._resource(k, a) for k, a in sorted(self.lg.accessories.items())}
        fleet_doc = self._fleet_doc(resources)
        files["bay.fleet.toml"] = to_toml(fleet_doc)
        return ImportResult(
            fleet_name=self.name,
            boxes=self.boxes,
            files=files,
            copies=self.copies,
            projects=sorted(project_docs),
            resources=sorted(resources),
            groupings=self.groupings,
            needs_pairs=sorted(self.needs_pairs),
            fleet_secret_aliases=self.aliases,
            flags=list(dict.fromkeys(self.flags)),
            secret_moves=sorted(self.moves.values(), key=lambda m: m.name),
            notes=self.notes,
        )

    def _box(self, svc: dict[str, Any], key: str) -> str:
        boxes = self._boxes_of(svc, key)
        chosen = boxes[0] if boxes else self.default_box
        if len(boxes) != 1:
            self.flags.append(
                f"{key}: runs on {len(boxes)} boxes; "
                f"bay.toml places one box per environment, used {chosen}"
            )
        return chosen

    # ── grouping ────────────────────────────────────────────────────────
    def _plan_projects(self) -> list[_Plan]:
        keys = sorted(self.lg.services)
        taken: set[str] = set()
        plans: dict[str, _Plan] = {}

        # G1: <name>-<env suffix> is an environment of <name>.
        for key in keys:
            m = re.match(r"^(.+)-(" + "|".join(ENV_SUFFIXES) + r")$", key)
            if not m:
                continue
            stem, env = m.group(1), ENV_SUFFIXES[m.group(2)]
            if stem in self.lg.services and env == PRIMARY_ENV:
                self.groupings.append(f"{key}: kept as its own project, {stem} already holds {env}")
                continue
            plan = plans.setdefault(stem, _Plan(stem, []))
            if any(x.env == env for x in plan.members):
                self.groupings.append(f"{key}: kept as its own project, {stem} already has {env}")
                continue
            reason = self._env_group_problem(stem, key, env)
            if reason:
                self.groupings.append(
                    f"{key}: kept as its own project, not environment {env} of {stem}: {reason}"
                )
                if not plan.members:
                    del plans[stem]
                continue
            plan.members.append(_Member(key, self.lg.services[key], env, "web"))
            taken.add(key)
            self.groupings.append(
                f"{key}: environment {env} of project {stem} (container name adopted)"
            )
        for stem, plan in list(plans.items()):
            if stem in self.lg.services:
                plan.members.insert(0, _Member(stem, self.lg.services[stem], PRIMARY_ENV, "web"))
                taken.add(stem)

        # G2: <name>-<suffix> of the same app is [services.<suffix>].
        for key in keys:
            if key in taken:
                continue
            for base in sorted(
                (k for k in keys if key.startswith(k + "-") and k != key), key=len, reverse=True
            ):
                if base in taken and base not in plans:
                    continue
                suffix = key[len(base) + 1 :]
                reason = self._service_group_problem(base, key, suffix)
                if reason is None:
                    plan = plans.setdefault(
                        base,
                        _Plan(base, [_Member(base, self.lg.services[base], PRIMARY_ENV, "web")]),
                    )
                    if len(plan.members) > 1 and any(x.env != PRIMARY_ENV for x in plan.members):
                        self.groupings.append(
                            f"{key}: kept as its own project, {base} has several environments"
                        )
                        break
                    plan.members.append(_Member(key, self.lg.services[key], PRIMARY_ENV, suffix))
                    taken.update({base, key})
                    self.groupings.append(f"{key}: [services.{suffix}] of project {base}")
                else:
                    self.groupings.append(
                        f"{key}: kept as its own project, not [services.{suffix}] of {base}: "
                        f"{reason}"
                    )
                break
        for key in keys:
            if key not in taken:
                plans[key] = _Plan(key, [_Member(key, self.lg.services[key], PRIMARY_ENV, "web")])
                taken.add(key)
        return [plans[k] for k in sorted(plans)]

    def _same_app(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        ra, rb = (a.get("build") or {}).get("repo"), (b.get("build") or {}).get("repo")
        if ra or rb:
            return bool(ra) and ra == rb
        return _image_repo(a.get("image")) == _image_repo(b.get("image")) is not None

    def _env_group_problem(self, stem: str, key: str, env: str) -> str | None:
        if stem not in self.lg.services:
            return None
        a, b = self.lg.services[stem], self.lg.services[key]
        if not self._same_app(a, b):
            return "a different repo or image"
        same = (
            "volumes",
            "config_files",
            "middleware",
            "ports",
            "healthcheck_path",
            "mem_limit",
            "memswap_limit",
            "command",
            "database",
            "update",
            "zero_downtime",
            "replicas",
            "log_rotation",
        )
        for k in same:
            if k in ("volumes", "database"):
                continue  # names differ per environment and are adopted
            if a.get(k) != b.get(k):
                return f"{k} differs between the environments"
        return None

    def _service_group_problem(self, base: str, key: str, suffix: str) -> str | None:
        a, b = self.lg.services[base], self.lg.services[key]
        if not self._same_app(a, b):
            return "a different repo or image"
        if self._box(a, base) != self._box(b, key):
            return "it runs on another box"
        if len(b.get("domains") or []) > 1:
            return "a service has one domain, this container has several"
        port = _port(b)
        clear = (a.get("env") or {}).get("clear") or {}
        if port is not None and clear.get(sibling_var(suffix)) != f"http://{key}:{port}":
            return f"{base} would get {sibling_var(suffix)}, which it does not read today"
        da, db = a.get("database"), b.get("database")
        if da and db and _db(base, da) != _db(key, db):
            return "both containers bind a different database"
        ab, bb = a.get("build") or {}, b.get("build") or {}
        if ab and bb and ab.get("branch", "main") != bb.get("branch", "main"):
            return "the builds follow different branches"
        if (b.get("ports") or {}).get("expose") == "host":
            return "it publishes its port on all addresses"
        return None

    # ── resources ───────────────────────────────────────────────────────
    def _resource_kinds(self) -> dict[str, str]:
        kinds = {k: "container" for k in self.lg.accessories}
        for svc in self.lg.services.values():
            target = (svc.get("database") or {}).get("accessory")
            if target in kinds:
                kinds[target] = "postgres"
        return kinds

    def _resource(self, key: str, acc: dict[str, Any]) -> dict[str, Any]:
        where = f"shared container {key}"
        for k in sorted(set(acc) - _ACCESSORY_KEYS):
            self.flags.append(f"{where} {k}: no bay.fleet.toml key; dropped")
        boxes = self._boxes_of(acc, key)
        box = boxes[0] if boxes else self.default_box
        res: dict[str, Any] = {}
        if self.kinds.get(key) != "container":
            res["kind"] = self.kinds[key]
        if acc.get("regions"):
            res["box"] = boxes if len(boxes) > 1 else boxes[0]
        elif box != self.default_box:
            res["box"] = box
        res["image"] = self._resolve(acc.get("image"), box, f"{where} image")
        if "port" in acc:
            parts = str(acc["port"]).split(":")
            host_ip = parts[0] if len(parts) == 3 else None
            pair = parts[-2:] if len(parts) >= 2 else [parts[0], parts[0]]
            if pair[0] != pair[1]:
                self.flags.append(
                    f"{where} port: box port {pair[0]} differs from container port {pair[1]}; "
                    "bay.fleet.toml binds the same number"
                )
            res["port"] = int(pair[1])
            expose = acc.get("expose", "loopback")
            if host_ip not in (None, "127.0.0.1"):
                self.flags.append(f"{where} port: bound to {host_ip}; written as {expose}")
            if expose in ("gateway",):
                expose = "tailnet"
            if expose not in ("loopback", "tailnet"):
                self.flags.append(
                    f"{where} expose: {expose} is not expressible; written as loopback"
                )
                expose = "loopback"
            res["expose"] = expose
        elif key in self.res_ports:
            res["port"] = self.res_ports[key]
        if "command" in acc:
            res["command"] = acc["command"]
        if "mem_limit" in acc:
            res["memory"] = _size(acc["mem_limit"], f"{where} mem_limit", self.flags)
        _swap_flag(acc, where, self.flags)
        update = _UPDATE.get(acc.get("update"))
        if update is None:
            self.flags.append(
                f"{where} update: {acc.get('update')!r} is not expressible; written as notify"
            )
        elif update != "notify":
            res["update"] = update
        if "network_mode" in acc:
            res["network_mode"] = acc["network_mode"]
        if acc.get("volumes"):
            res["volumes"] = [
                str(v) for v in self._resolve(acc["volumes"], box, f"{where} volumes")
            ]
        env = acc.get("env") or {}
        if env.get("clear"):
            res["env"] = {
                k: _str(v)
                for k, v in self._resolve(env["clear"], box, f"{where} env.clear").items()
            }
        secret = env.get("secret")
        if isinstance(secret, list):
            prefix = key.upper().replace("-", "_")
            res["secrets"] = {n: f"{prefix}_{n}" for n in secret}
        elif isinstance(secret, dict):
            res["secrets"] = dict(secret)
        if "healthcheck" in acc:
            hc = dict(acc["healthcheck"])
            res["health"] = {
                k: hc[k]
                for k in ("test", "interval", "timeout", "retries", "start_period")
                if k in hc
            }
        if "backup" in acc:
            res["backup"] = dict(acc["backup"])
        if acc.get("config_files"):
            res["files"] = list(acc["config_files"])
            self._copy_files(acc["config_files"], where)
        if "config_files_mode" in acc:
            res["files_mode"] = acc["config_files_mode"]
        return res

    def _copy_files(self, paths: list[str], where: str, project: str | None = None) -> None:
        """Copy config files. A project's own file (``<project>/<path>``) goes beside its toml."""
        for rel in paths:
            src = self.lg.root / "files" / rel
            if not src.is_file():
                self.flags.append(f"{where}.config_files: files/{rel} does not exist in the fleet")
                continue
            if project is not None and rel.startswith(project + "/"):
                self.copies[f"projects/{project}/{rel[len(project) + 1 :]}"] = src
            else:
                self.copies[f"files/{rel}"] = src

    # ── projects ────────────────────────────────────────────────────────
    def _project(self, plan: _Plan) -> tuple[dict[str, Any], dict[str, Any]]:
        mains = [m for m in plan.members if m.service == "web"]
        extras = [m for m in plan.members if m.service != "web"]
        primary = next((m for m in mains if m.env == PRIMARY_ENV), mains[0])
        doc: dict[str, Any] = {"name": plan.name, "fleet": self.name}
        lock: dict[str, Any] = {
            "lock_version": 2,
            "name": plan.name,
            "repo": None,
            "commit": None,
            "envs": {},
        }
        repos = sorted(
            {str(r) for m in plan.members if (r := (m.svc.get("build") or {}).get("repo"))}
        )
        if len(repos) > 1:
            self.flags.append(
                f"{plan.name}: builds from {len(repos)} repos; the lockfile holds one"
            )
        if repos:
            lock["repo"] = repos[0]

        # The main container receives <SUFFIX>_URL for every extra service; Bay
        # injects it, so it is not declared in [env].
        injected = {
            sibling_var(m.service): f"http://{m.key}:{_port(m.svc)}"
            for m in extras
            if _port(m.svc) is not None
        }
        names = self._env_names(plan.name, mains)
        per_env: dict[str, dict[str, Any]] = {}
        for m in mains:
            per_env[m.env] = self._level(plan, m, m.key, is_web=True, injected=injected)
        top = per_env[primary.env]
        for k in (
            "image",
            "build",
            "port",
            "expose",
            "command",
            "health",
            "memory",
            "update",
            "zero_downtime",
            "replicas",
            "logs",
            "log_rotation",
            "secrets",
            "needs",
            "fleet_secrets",
            "access",
            "mounts",
        ):
            if k in top["doc"]:
                doc[k] = top["doc"][k]
        doc["env"] = dict(top["doc"].get("env", {}))
        deploy: dict[str, Any] = {}
        for env, lvl in per_env.items():
            d: dict[str, Any] = dict(lvl["deploy"])
            if env != primary.env:
                for k in (
                    "image",
                    "port",
                    "command",
                    "health",
                    "memory",
                    "update",
                    "zero_downtime",
                    "replicas",
                    "logs",
                    "secrets",
                ):
                    if lvl["doc"].get(k) != doc.get(k) and k in lvl["doc"]:
                        d[k] = lvl["doc"][k]
                env_access = lvl["doc"].get("access", {})
                acc_diff = {
                    k: v for k, v in env_access.items() if doc.get("access", {}).get(k) != v
                }
                for k in sorted(set(doc.get("access", {})) - set(env_access)):
                    if k in ("open", "locked"):
                        acc_diff[k] = []
                    else:
                        self.flags.append(
                            f"{lvl['key']}: access.{k} of {primary.key} cannot be removed for one "
                            "environment"
                        )
                if acc_diff:
                    d["access"] = acc_diff
                for k in ("needs", "fleet_secrets", "mounts", "build", "expose", "log_rotation"):
                    if lvl["doc"].get(k) != doc.get(k):
                        self.flags.append(
                            f"{lvl['key']}: {k} differs from {primary.key}; "
                            f"environments share it, so {primary.key}'s value is used"
                        )
            deploy[names[env]] = d
        # [env]: what every environment shares; the rest per environment.
        if len(per_env) > 1:
            common = {
                k: v
                for k, v in doc["env"].items()
                if all(lvl["doc"].get("env", {}).get(k) == v for lvl in per_env.values())
            }
            for env, lvl in per_env.items():
                rest = {k: v for k, v in lvl["doc"].get("env", {}).items() if common.get(k) != v}
                if rest:
                    deploy[names[env]]["env"] = rest
            doc["env"] = common
        if not doc["env"]:
            del doc["env"]
        for env, lvl in per_env.items():
            lock["envs"][names[env]] = {"box": lvl["box"], "adopted": lvl["adopted"]}

        services: dict[str, Any] = {}
        for m in extras:
            lvl = self._level(plan, m, m.key, is_web=False, main_key=primary.key)
            services[m.service] = lvl["doc"]
            adopted = lock["envs"][names[primary.env]]["adopted"]
            for k, v in lvl["adopted"].items():
                if isinstance(v, dict):
                    adopted.setdefault(k, {}).update(v)
                elif k not in adopted:
                    adopted[k] = v
                elif adopted[k] != v:
                    self.flags.append(f"{m.key}: {k} {v} differs from {adopted[k]}")
        for sdoc in services.values():
            # A service inherits update and logs from the project; keep today's own value.
            if "update" not in sdoc and doc.get("update", "notify") != "notify":
                sdoc["update"] = "notify"
            if "logs" not in sdoc and doc.get("logs", "off") != "off":
                sdoc["logs"] = "off"
        if services:
            doc["services"] = services
        doc["deploy"] = deploy
        for env in lock["envs"].values():
            env["adopted"] = {
                k: v for k, v in sorted(env["adopted"].items()) if v not in ({}, None)
            }
        return doc, lock

    def _env_names(self, project: str, mains: list[_Member]) -> dict[str, str]:
        """``{logical env: [deploy.<name>]}`` for a project's main containers.

        The primary environment (``-prod`` or no suffix) is named after the
        group of the box it runs on (``boxes.<b>.env``), so a box in group
        ``testing`` gives ``[deploy.testing]``. ``-staging`` and ``-dev`` keep
        their names. When a name would then repeat, the project keeps the
        plain names and a FLAG says so.
        """
        names: dict[str, str] = {}
        for m in mains:
            if m.env == PRIMARY_ENV:
                boxes = self._boxes_of(m.svc, m.key)
                names[m.env] = self.boxes[boxes[0] if boxes else self.default_box].env
            else:
                names[m.env] = m.env
        if len(set(names.values())) < len(names):
            self.flags.append(
                f"{project}: the box group {names[PRIMARY_ENV]} is also the name of another "
                f"environment; kept the names {', '.join(sorted(names))}"
            )
            return {env: env for env in names}
        return names

    def _level(
        self,
        plan: _Plan,
        m: _Member,
        key: str,
        *,
        is_web: bool,
        main_key: str | None = None,
        injected: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map one container to a bay.toml level, its deploy table and its adopted names."""
        svc = m.svc
        where = f"services.{key}"
        box = self._box(svc, key)
        flags = self.flags
        for k in sorted(set(svc) - _SERVICE_KEYS):
            flags.append(
                f"{where}.{k}: no bay.toml key; dropped (the container spec never reads it)"
                if k == "port"
                else f"{where}.{k}: no bay.toml key; dropped"
            )
        prefix = (key if is_web else main_key or key).upper().replace("-", "_")
        doc: dict[str, Any] = {}
        deploy: dict[str, Any] = {"box": box}
        adopted: dict[str, Any] = {"containers": {m.service: key}}
        if not is_web:
            doc["inherit"] = False

        # image and build
        build = svc.get("build")
        image = self._resolve(svc.get("image"), box, f"{where}.image")
        if build:
            table: dict[str, Any] = {}
            for k in sorted(set(build) - _BUILD_KEYS):
                flags.append(f"{where}.build.{k}: no bay.toml key; dropped")
            for k in ("dockerfile", "context", "strategy"):
                if k in build:
                    table[k] = build[k]
            paths = build.get("paths") or {}
            if paths.get("include"):
                table["watch"] = list(paths["include"])
            if paths.get("exclude"):
                table["ignore"] = list(paths["exclude"])
            if build.get("args"):
                table["args"] = {k: _str(v) for k, v in build["args"].items()}
            if build.get("secrets"):
                table["secrets"] = {}
                for sid, ref in build["secrets"].items():
                    name = _secret_name(ref)
                    if name is None:
                        flags.append(
                            f"{where}.build.secrets.{sid}: not a secret reference; dropped"
                        )
                    else:
                        table["secrets"][sid] = name
            branch = build.get("branch", "main")
            if is_web and branch != "main":
                deploy["branch"] = branch
            if "token" in build and _secret_name(build["token"]) is None:
                flags.append(f"{where}.build.token: not a secret reference; dropped")
            doc["build"] = table
            if image:
                adopted["images"] = {m.service: image}
        elif image:
            doc["image"] = image

        # runtime
        port = _port(svc)
        if port is not None:
            doc["port"] = port
        expose = (svc.get("ports") or {}).get("expose")
        if expose:
            if expose == "gateway":
                expose = "tailnet"
            if expose in ("loopback", "tailnet"):
                doc["expose"] = expose
            else:
                flags.append(f"{where}.ports.expose: {expose} is not expressible; dropped")
        if "command" in svc:
            if isinstance(svc["command"], str):
                doc["command"] = svc["command"]
            else:
                flags.append(f"{where}.command: a list is not expressible in bay.toml; dropped")
        doc["health"] = svc.get("healthcheck_path") or "none"
        if "mem_limit" in svc:
            doc["memory"] = _size(svc["mem_limit"], f"{where}.mem_limit", flags)
        _swap_flag(svc, where, flags)
        update = _UPDATE.get(svc.get("update"))
        if update is None:
            flags.append(
                f"{where}.update: {svc.get('update')!r} is not expressible; written as notify"
            )
        elif update != "notify":
            doc["update"] = update
        if svc.get("zero_downtime"):
            doc["zero_downtime"] = True
        if svc.get("replicas", 1) != 1:
            doc["replicas"] = svc["replicas"]
        retention = svc.get("log_retention")
        if retention:
            if "days" in retention:
                doc["logs"] = f"{retention['days']}d"
            extra = sorted(set(retention) - {"days"})
            if extra:
                flags.append(
                    f"{where}.log_retention: {', '.join(extra)} have no bay.toml key; "
                    "only days is carried over"
                )
        rotation = svc.get("log_rotation")
        if rotation:
            if set(rotation) == {"max_size", "max_file"} and _SIZE_RE.match(
                str(rotation["max_size"])
            ):
                doc["log_rotation"] = {
                    "max_size": str(rotation["max_size"]),
                    "max_file": int(rotation["max_file"]),
                }
            else:
                flags.append(
                    f"{where}.log_rotation: only max_size and max_file are expressible; dropped"
                )

        # access and routing
        mode = "public" if svc.get("access", "public") == "public" else "tailnet"
        access: dict[str, Any] = {"mode": mode}
        if svc.get("vpn_routes"):
            if mode == "public":
                access["locked"] = list(svc["vpn_routes"])
            else:
                flags.append(f"{where}.vpn_routes: has no effect on a tailnet container; dropped")
        if svc.get("public_routes"):
            if mode == "tailnet":
                access["open"] = list(svc["public_routes"])
            else:
                flags.append(f"{where}.public_routes: has no effect on a public container; dropped")
        self._middleware(svc.get("middleware") or {}, access, prefix, where, box)
        if "password" in access and access.get("open"):
            flags.append(f"{where}: open paths and a password together are not supported yet")
        doc["access"] = access
        domains = [self._resolve(d, box, f"{where}.domains") for d in svc.get("domains") or []]
        if is_web:
            if domains:
                deploy["domain"] = domains[0]
            if len(domains) > 1:
                deploy["aliases"] = domains[1:]
                deploy["redirect"] = False
        elif domains:
            doc["domain"] = domains[0]
            doc["access"] = access

        # env, secrets, needs
        self._env(svc, key, prefix, box, doc, where, injected or {})
        db = svc.get("database")
        if db:
            target = db.get("accessory")
            name, user = _db(key, db)
            needs = doc.setdefault("needs", {})
            needs["postgres"] = {}
            adopted["database"], adopted["role"] = name, user
            if target not in self.lg.accessories:
                flags.append(f"{where}.database: {target} is not a shared container")
            elif box not in self._boxes_of(self.lg.accessories[target], target):
                flags.append(f"{where}.database: {target} runs on another box")
        depends = set(svc.get("depends_on") or [])
        compiled_depends = {n for n in _need_names(doc) if n in self.lg.accessories}
        if db:
            compiled_depends.add(db.get("accessory"))
        missing = sorted(depends - compiled_depends)
        if missing:
            flags.append(
                f"{where}.depends_on: {', '.join(missing)} has no need behind it; not carried over"
            )

        # mounts
        mounts, adopted_vols, adopted_files = self._mounts(svc, plan.name, key, box, where)
        if mounts:
            doc["mounts"] = mounts
        if adopted_vols:
            adopted["volumes"] = adopted_vols
        if adopted_files:
            adopted["files"] = adopted_files
        return {"doc": doc, "deploy": deploy, "adopted": adopted, "box": box, "key": key}

    def _middleware(
        self, mw: dict[str, Any], access: dict[str, Any], prefix: str, where: str, box: str
    ) -> None:
        for k in sorted(set(mw) - {"basic_auth", "rate_limit", "in_flight_req"}):
            self.flags.append(f"{where}.middleware.{k}: no bay.toml key; dropped")
        limits: dict[str, Any] = {}
        rl = mw.get("rate_limit")
        if rl:
            period = str(rl.get("period", "1s"))
            if "average" in rl and period in ("1s", "1m"):
                limits["rate"] = f"{rl['average']}/{period[1]}"
            elif "average" in rl:
                self.flags.append(
                    f"{where}.middleware.rate_limit.period: {period} is not expressible; dropped"
                )
            if "burst" in rl:
                limits["burst"] = int(rl["burst"])
        if mw.get("in_flight_req", {}).get("amount"):
            limits["concurrent"] = int(mw["in_flight_req"]["amount"])
        if limits:
            access["limits"] = limits
        ba = mw.get("basic_auth")
        if not ba:
            return
        if "credentials" not in ba:
            self.flags.append(
                f"{where}.middleware.basic_auth: pre-hashed users are not expressible; dropped"
            )
            return
        users = []
        for i, cred in enumerate(ba["credentials"]):
            user = str(cred["username"])
            if not re.match(r"^[a-z][a-z0-9_]*$", user):
                self.flags.append(
                    f"{where}.middleware.basic_auth: "
                    f"user {user} is not a valid bay.toml user name; dropped"
                )
                continue
            users.append(user)
            key = f"{prefix}_PASSWORD_{user.upper()}"
            raw = cred.get("password")
            ref = _secret_name(raw)
            if ref == key:
                continue
            if ref is not None:
                self.moves[key] = SecretMove(key, ("secret", ref))
            else:
                self.moves[key] = SecretMove(
                    key,
                    ("literal", f"{where}.middleware.basic_auth.credentials[{i}].password"),
                    value=str(raw),
                )
                self.flags.append(
                    f"{where}.middleware.basic_auth: the password is plain text today; "
                    f"add it as secret {key}"
                )
        if users:
            pw: dict[str, Any] = {"users": users}
            if "realm" in ba:
                pw["realm"] = ba["realm"]
            access["password"] = pw
        for k in sorted(set(ba) - {"credentials", "realm", "removeheader"}):
            self.flags.append(f"{where}.middleware.basic_auth.{k}: no bay.toml key; dropped")

    def _env(
        self,
        svc: dict[str, Any],
        key: str,
        prefix: str,
        box: str,
        doc: dict[str, Any],
        where: str,
        injected: dict[str, str],
    ) -> None:
        env = svc.get("env") or {}
        clear = self._resolve(dict(env.get("clear") or {}), box, f"{where}.env.clear")
        out: dict[str, str] = {}
        needs: dict[str, dict[str, Any]] = {}
        for var, value in clear.items():
            sval = _str(value)
            if injected.get(var) == sval:
                continue
            target = self._url_target(sval, key, box)
            if target is not None and target not in needs:
                default = target.upper().replace("-", "_") + "_URL"
                needs[target] = {} if var == default else {"env": var}
                self.needs_pairs.append((key, target, var))
                continue
            if sval.startswith("http://") and self._names_container(sval):
                self.flags.append(
                    f"{where}.env.clear.{var}: {sval} reaches another container but is not "
                    "exactly http://<container>:<port>; kept in [env]"
                )
            if "{{" in sval:
                self.flags.append(
                    f"{where}.env.clear.{var}: holds a template; bay.toml takes literals only"
                )
            out[var] = sval
        if out:
            doc["env"] = out
        secret = env.get("secret")
        secrets: list[str] = []
        fleet_secrets: dict[str, str] = {}
        if isinstance(secret, list):
            own = key.upper().replace("-", "_")
            for n in secret:
                if own == prefix:
                    secrets.append(n)
                else:
                    fleet_secrets[n] = f"{own}_{n}"
        elif isinstance(secret, dict):
            for var, vkey in secret.items():
                if vkey == f"{prefix}_{var}":
                    secrets.append(var)
                else:
                    fleet_secrets[var] = vkey
        if secrets:
            doc["secrets"] = secrets
        if fleet_secrets:
            doc["fleet_secrets"] = fleet_secrets
            self.aliases += len(fleet_secrets)
        if needs:
            doc["needs"] = {**doc.get("needs", {}), **needs}

    def _names_container(self, url: str) -> bool:
        host = url[len("http://") :].split("/", 1)[0].split(":", 1)[0]
        return host in self.lg.services or host in self.lg.accessories

    def _url_target(self, value: str, key: str, box: str) -> str | None:
        """The project or resource that ``value`` reaches, when it is exactly its URL."""
        m = _URL_RE.match(value)
        if not m:
            return None
        host, port = m.group(1), int(m.group(2))
        if host in self.main_of and host != key:
            pname, pport, pbox = self.main_of[host]
            if pport == port and pbox == box and pname != self._project_of(key):
                return pname
            return None
        if host in self.lg.accessories and self.kinds.get(host) == "container":
            acc = self.lg.accessories[host]
            if box not in self._boxes_of(acc, host):
                return None
            have = self.res_ports.get(host) or (
                int(str(acc["port"]).split(":")[-1]) if "port" in acc else None
            )
            if have is None:
                self.res_ports[host] = port
                return host
            return host if have == port else None
        # Another box: http://<tailnet ip of that box>:<port>
        for main, (pname, pport, pbox) in sorted(self.main_of.items()):
            ip = self.boxes[pbox].tailnet_ip
            if pbox != box and ip == host and pport == port and pname != self._project_of(key):
                svc = self.lg.services.get(main, {})
                if (svc.get("ports") or {}).get("expose") in ("tailnet", "gateway"):
                    return pname
        return None

    def _project_of(self, key: str) -> str | None:
        hit = self.main_of.get(key)
        return hit[0] if hit else None

    def _mounts(
        self, svc: dict[str, Any], project: str, key: str, box: str, where: str
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
        mounts: list[dict[str, Any]] = []
        vols: dict[str, str] = {}
        files: dict[str, str] = {}
        config_files = list(svc.get("config_files") or [])
        covered: set[str] = set()
        public = svc.get("config_files_mode") == "public"
        stem = project.replace("-", "_")
        for i, raw in enumerate(svc.get("volumes") or []):
            spec = str(self._resolve(raw, box, f"{where}.volumes[{i}]"))
            cfg = _CONFIG_BIND_RE.match(spec)
            if cfg:
                path_on_box, target, opt = cfg.group(1).rstrip("/"), cfg.group(2), cfg.group(3)
                if opt != "ro":
                    self.flags.append(
                        f"{where}.volumes[{i}]: a config mount that is not read-only; "
                        "written read-only"
                    )
                if path_on_box.startswith(project + "/"):
                    src = path_on_box[len(project) + 1 :]
                else:
                    src = path_on_box
                    files[src] = path_on_box
                mount: dict[str, Any] = {"path": target, "from": src}
                if public:
                    mount["mode"] = "0644"
                mounts.append(mount)
                for f in config_files:
                    if f == path_on_box or f.startswith(path_on_box + "/"):
                        covered.add(f)
                        own = project if path_on_box.startswith(project + "/") else None
                        self._copy_files([f], where, own)
                continue
            parts = spec.split(":")
            name = parts[0]
            if name.startswith(("/", ".", "{")) or len(parts) < 2:
                self.flags.append(
                    f"{where}.volumes[{i}]: a bind mount of a box path is not expressible; dropped"
                )
                continue
            if len(parts) > 2:
                self.flags.append(
                    f"{where}.volumes[{i}]: volume options ({parts[2]}) are not expressible; "
                    "dropped"
                )
            vkey = name
            for p in (
                f"{key.replace('-', '_')}_",
                f"{key}_",
                f"{key}-",
                f"{stem}_",
                f"{project}_",
                f"{project}-",
            ):
                if name.startswith(p) and _VOLUME_KEY_RE.match(name[len(p) :]):
                    vkey = name[len(p) :]
                    break
            vkey = vkey.lower() if _VOLUME_KEY_RE.match(vkey.lower()) else "data"
            # The old YAML fleet backed up no volume. backup = false keeps
            # that, so the import compiles to the same file; drop it to
            # start volume backups.
            mounts.append({"path": parts[1], "volume": vkey, "backup": False})
            vols[vkey] = name
        for f in config_files:
            if f not in covered:
                self.flags.append(
                    f"{where}.config_files: {f} is not under a config mount of this container; "
                    "not carried over"
                )
        return mounts, vols, files

    # ── fleet file ──────────────────────────────────────────────────────
    def _fleet_doc(self, resources: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = {"name": self.name, "format": 2, "default_box": self.default_box}
        domain = None
        if self.lg.webhook:
            wh_domain = self._resolve(
                self.lg.webhook.get("domain"), self.default_box, "webhook.domain"
            )
            domain = (
                wh_domain.split(".", 1)[1]
                if isinstance(wh_domain, str) and "." in wh_domain
                else None
            )
        if domain is None:
            first = next(
                (d for s in self.lg.services.values() for d in s.get("domains") or []),
                "example.com",
            )
            domain = self._resolve(first, self.default_box, "domains").split(".", 1)[-1]
        doc["default_domain"] = domain
        if len(self.groups) == 1:
            doc["primary_env"] = self.groups[0]
        boxes: dict[str, Any] = {}
        for name, box in sorted(self.boxes.items()):
            entry: dict[str, Any] = {"env": box.env}
            if box.group:
                entry["group"] = box.group
            if box.tailnet_ip:
                entry["tailnet_ip"] = box.tailnet_ip
            if self.lg.webhook:
                own = self._resolve(self.lg.webhook.get("domain"), name, "webhook.domain")
                default = self._resolve(
                    self.lg.webhook.get("domain"), self.default_box, "webhook.domain"
                )
                if isinstance(own, str) and own != default:
                    entry["webhook_domain"] = own
            boxes[name] = entry
        doc["boxes"] = boxes
        if resources:
            doc["resources"] = resources
        tailnet = self._tailnet(boxes)
        if tailnet:
            doc["tailnet"] = tailnet
        if self.lg.webhook:
            secret_raw = self.lg.webhook.get("secret")
            secret = _secret_name(secret_raw)
            if secret is None:
                secret = "WEBHOOK_SECRET"
                self.moves[secret] = SecretMove(
                    secret, ("literal", "webhook.secret"), value=str(secret_raw)
                )
                self.flags.append(
                    "webhook.secret: the value is plain text today; add it as secret WEBHOOK_SECRET"
                )
            doc["webhook"] = {
                "domain": self._resolve(
                    self.lg.webhook.get("domain"), self.default_box, "webhook.domain"
                ),
                "secret": secret,
            }
        tokens = self._repo_tokens()
        if tokens:
            doc["repo_tokens"] = tokens
        return doc

    def _tailnet(self, boxes: dict[str, Any]) -> dict[str, Any] | None:
        """``[tailnet]`` with today's ``tailnet_proxies`` as routes, or None.

        The routes need the ingress box and the certificate domain. When the
        group variables do not tell them, the proxies stay in their YAML file
        with a note, and ``bay route import`` moves them later.
        """
        from bay_cli import routes

        proxies = self.lg.tailnet_proxies
        if not proxies:
            return None
        count = len(proxies)
        table, problems = routes.from_proxies(proxies)
        shapes = {
            name: {"group": box.group, "groups": box.groups} for name, box in self.boxes.items()
        }
        ingress, cert, why = routes.guess_ingress(self.lg.group_vars, shapes)
        keep = f"{count} tailnet proxies stay in their own YAML file"
        if problems:
            self.flags.extend(problems)
            self.notes.append(f"{keep}: they cannot be carried over as they are")
            return None
        if ingress is None or cert is None:
            self.notes.append(
                f"{keep}: {why}; move them later with "
                "`bay route import --ingress-box <box> --cert-domain <domain>`"
            )
            return None
        tailnet = {"ingress_box": ingress, "cert_domain": cert, "routes": table}
        _, errors = routes.compile_routes({"boxes": boxes, "tailnet": tailnet})
        if errors:
            self.flags.extend(errors)
            self.notes.append(f"{keep}: the routes do not check out")
            return None
        self.notes.append(
            f"{count} tailnet proxies became [tailnet.routes] (ingress box {ingress}, "
            f"{why}); leave their old YAML file out of the new fleet"
        )
        return tailnet

    def _repo_tokens(self) -> dict[str, str]:
        wanted: dict[str, str | None] = {}
        for _key, svc in sorted(self.lg.services.items()):
            build = svc.get("build") or {}
            if "repo" in build:
                wanted[build["repo"]] = (
                    _secret_name(build.get("token")) if build.get("token") else None
                )
        by_owner: dict[str, set[str | None]] = {}
        for repo, token in wanted.items():
            by_owner.setdefault(_repo_owner(repo), set()).add(token)
        tokens: dict[str, str] = {}
        for repo, token in sorted(wanted.items()):
            if token is None:
                continue
            owner = _repo_owner(repo)
            if by_owner[owner] == {token}:
                tokens[owner] = token
            else:
                tokens[repo] = token
        return tokens


# ── helpers ─────────────────────────────────────────────────────────────────


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return s or "fleet"


def _port(svc: dict[str, Any]) -> int | None:
    internal = (svc.get("ports") or {}).get("internal")
    return int(internal) if internal is not None else None


def _db(key: str, db: dict[str, Any]) -> tuple[str, str]:
    return str(db.get("name", key)), str(db.get("user", key))


def _image_repo(image: Any) -> str | None:
    if not isinstance(image, str):
        return None
    base = image.split("@", 1)[0]
    head, _, tail = base.rpartition("/")
    return f"{head}/{tail.split(':', 1)[0]}" if head else tail.split(":", 1)[0]


def _repo_owner(repo: str) -> str:
    m = re.match(r"^(.*[:/][^/:]+/)[^/]+$", repo)
    return m.group(1) if m else repo


def _secret_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = _SECRET_REF_RE.match(value.strip())
    return (m.group(1) or m.group(2)) if m else None


def _str(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _size(value: Any, where: str, flags: list[str]) -> str:
    s = str(value).lower()
    if not _SIZE_RE.match(s):
        flags.append(f"{where}: {value} is not a size bay.toml accepts")
    return s


def _swap_flag(block: dict[str, Any], where: str, flags: list[str]) -> None:
    """Flag a swap setting bay.toml cannot say.

    ``memory = "X"`` means mem_limit X and memswap_limit X: swap is never
    allowed. So no memswap_limit, or one equal to mem_limit, needs no flag (the
    first gains the cap at its first deploy, which recreates it once). Anything
    else allows swap, or has no memory limit to cap, and is dropped.
    """
    if "memswap_limit" not in block:
        return
    swap = str(block["memswap_limit"]).lower()
    mem = block.get("mem_limit")
    if mem is None:
        flags.append(
            f"{where}.memswap_limit: {swap} has no mem_limit to match; "
            "swap-off is not expressible; dropped"
        )
    elif swap != str(mem).lower():
        flags.append(
            f"{where}.memswap_limit: {swap} differs from mem_limit {str(mem).lower()}; "
            "swap is allowed here, bay.toml never allows it; written as no swap"
        )


def _need_names(doc: dict[str, Any]) -> list[str]:
    names = list((doc.get("needs") or {}).keys())
    for svc in (doc.get("services") or {}).values():
        names += list((svc.get("needs") or {}).keys())
    return [n for n in names if n not in ("postgres", "redis")]


_PROJECT_ORDER = (
    "name",
    "fleet",
    "publish",
    "image",
    "port",
    "expose",
    "command",
    "health",
    "replicas",
    "memory",
    "update",
    "zero_downtime",
    "logs",
    "secrets",
    "log_rotation",
    "needs",
    "build",
    "env",
    "fleet_secrets",
    "access",
    "mounts",
    "services",
    "deploy",
)


def _order_project(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: doc[k] for k in _PROJECT_ORDER if k in doc}
    out.update({k: v for k, v in doc.items() if k not in out})
    return out


# ── TOML writer ─────────────────────────────────────────────────────────────

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_scalar(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def _is_table(v: Any) -> bool:
    return isinstance(v, dict)


def _is_table_array(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def to_toml(doc: dict[str, Any]) -> str:
    """A small, deterministic TOML writer for the shapes the importer emits."""
    lines: list[str] = []
    _emit(doc, [], lines)
    text = "\n".join(lines).strip("\n") + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def _emit(table: dict[str, Any], path: list[str], lines: list[str]) -> None:
    plain = {k: v for k, v in table.items() if not _is_table(v) and not _is_table_array(v)}
    for k, v in plain.items():
        lines.append(f"{_key(k)} = {_scalar(v)}")
    for k, v in table.items():
        if _is_table(v):
            sub = [*path, k]
            has_plain = any(not _is_table(x) and not _is_table_array(x) for x in v.values())
            if has_plain or not v:
                lines.append("")
                lines.append("[" + ".".join(_key(p) for p in sub) + "]")
            _emit(v, sub, lines)
        elif _is_table_array(v):
            for item in v:
                lines.append("")
                lines.append("[[" + ".".join(_key(p) for p in [*path, k]) + "]]")
                _emit(item, [*path, k], lines)
