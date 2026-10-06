"""The round-trip check behind ``bay import --check`` and the real-fleet gate.

It imports a fleet into a scratch directory, compiles it, then renders BOTH
today's ``services.yml`` and the compiled one through the deploy code that
builds the container specs: ``roles/container_lifecycle/tasks/build_specs.yml``
for every box, plus ``roles/deploy_stack/templates/env.j2`` for every env
file, run as a local Ansible play. It then compares, per container:

* the spec build_specs.yml produces (name, image, labels, ports, volumes,
  command, healthcheck, memory limits, networks, restart policy, ...),
* the env file as names and values (a secret renders as ``<secret NAME>``, so
  the comparison sees which secret a variable reads, never a value),
* the raw keys the spec does not carry but deploy reads: ``depends_on``,
  ``build``, ``healthcheck_path``, ``replicas``, ``log_retention``,
* per box: the set of shared containers, the database provisioning inputs
  (database, user, password secret) and the config files copied to the box.

Secret values are never read. Secrets render as fixed placeholders; a secret
the import moves (see :class:`bay_cli.importer.SecretMove`) renders the value
it moves on the compiled side, so a plain-text password moved into a secret
compares equal.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bay_cli.importer import ImportResult, Legacy, import_fleet, load_legacy

#: Raw services.yml keys compared beside the spec.
RAW_KEYS = ("depends_on", "build", "healthcheck_path", "replicas", "log_retention")
#: Per-box sets compared beside the containers.
BOX_KEYS = ("shared containers", "databases", "config files")

_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


@dataclass(frozen=True)
class Exception_:
    """One accepted difference: ``key`` is a spec key, a raw key or ``env``."""

    fleet: str
    container: str
    key: str
    reason: str


@dataclass
class ContainerDiff:
    box: str
    container: str
    keys: list[str]
    diff: str


@dataclass
class GateResult:
    fleet: str
    imported: ImportResult
    unsupported: list[str]
    diffs: list[ContainerDiff] = field(default_factory=list)
    #: containers whose env file differs only in line order (the config hash changes)
    env_order_only: list[str] = field(default_factory=list)
    containers: int = 0
    excepted: list[tuple[str, str, str]] = field(default_factory=list)
    #: (today, compiled) renders, kept so a caller can compare again
    renders: tuple[dict[str, Any], dict[str, Any]] | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.diffs and not self.unsupported

    def summary_lines(self) -> list[str]:
        out = [
            f"{self.fleet}: {self.containers} container(s) compared on {len(self.imported.boxes)} "
            "box(es)"
        ]
        out += [f"cannot compile without --allow-unsupported: {u}" for u in self.unsupported]
        for d in self.diffs:
            out.append(f"DIFF {d.container} on box {d.box}: {', '.join(d.keys)}")
        for box, container, key in self.excepted:
            out.append(f"accepted: {container} on box {box}: {key}")
        if self.env_order_only:
            out.append(env_order_line(self.env_order_only))
        return out


def env_order_line(names: list[str]) -> str:
    """The env-order recreate list, worded the same in ``bay import --check`` and ``bay plan``.

    ``bay import --check`` finds these by rendering today's file and the
    compiled one; ``bay plan --remote`` by the box's own prediction. The same
    line in both lets the operator compare the two lists.
    """
    return (
        "env files with the same variables in another order (the deploy hashes "
        f"the file, so these recreate once): {', '.join(sorted(set(names)))}"
    )


def framework_root() -> Path:
    from bay_cli.context import package_root

    return package_root()


# ── Rendering ───────────────────────────────────────────────────────────────


def _secret_names(blocks: list[dict[str, Any]], text: str) -> set[str]:
    names = set(_TOKEN_RE.findall(text))
    for block in blocks:
        for key, svc in block.items():
            env = (svc or {}).get("env") or {}
            secret = env.get("secret")
            if isinstance(secret, list):
                names |= {key.upper().replace("-", "_") + "_" + n for n in secret}
            elif isinstance(secret, dict):
                names |= set(secret.values())
            db = (svc or {}).get("database")
            if db:
                names.add(str(db.get("user", key)).upper().replace("-", "_") + "_POSTGRES_PASSWORD")
    return names


def placeholder(name: str) -> str:
    return f"<secret {name}>"


def render(
    data: dict[str, Any],
    boxes: dict[str, dict[str, Any]],
    secrets: dict[str, str],
    workdir: Path,
) -> dict[str, dict[str, Any]]:
    """Run build_specs.yml and env.j2 for every box. Return box -> rendered state.

    ``boxes`` maps a box name to ``{"groups": [...], "vars": {...}}``.
    """
    root = framework_root()
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "out"
    inventory: dict[str, Any] = {"all": {"children": {}}}
    children = inventory["all"]["children"]
    for box, spec in boxes.items():
        host = f"box-{box}"
        for g in spec["groups"]:
            children.setdefault(g, {"hosts": {}})["hosts"][host] = {
                "box_name": box,
                **spec["vars"],
            }
    (workdir / "inventory.json").write_text(json.dumps(inventory))
    play_vars = {
        "stack_dir": "/opt/fleet",
        "stack_name": "fleet",
        "secrets": secrets,
        "bay_alert_host": "box",
        "services": data.get("services") or {},
        "accessories": data.get("accessories") or {},
        "render_out": str(out),
    }
    if data.get("webhook"):
        play_vars["webhook"] = data["webhook"]
    (workdir / "vars.json").write_text(json.dumps(play_vars))
    (workdir / "ansible.cfg").write_text("[defaults]\n")
    env_j2 = root / "roles/deploy_stack/templates/env.j2"
    play = f"""\
- hosts: all
  connection: local
  gather_facts: false
  tasks:
    - name: Initialize active services
      ansible.builtin.set_fact:
        active_services: {{}}
        active_accessories: {{}}
    - name: Compute active services for this box (as deploy.yml does)
      ansible.builtin.set_fact:
        active_services: "{{{{ active_services | combine({{item.key: item.value}}) }}}}"
      loop: "{{{{ (services | default({{}})) | dict2items }}}}"
      when: >-
        item.value.regions is not defined
        or item.value.regions | length == 0
        or (item.value.regions | intersect(group_names) | length > 0)
    - name: Compute active shared containers for this box (as deploy.yml does)
      ansible.builtin.set_fact:
        active_accessories: "{{{{ active_accessories | combine({{item.key: item.value}}) }}}}"
      loop: "{{{{ (accessories | default({{}})) | dict2items }}}}"
      when: >-
        item.value.regions is not defined
        or item.value.regions | length == 0
        or (item.value.regions | intersect(group_names) | length > 0)
    - name: Build the container specs
      ansible.builtin.include_tasks: {root / "roles/container_lifecycle/tasks/build_specs.yml"}
    - name: Make the output directory
      ansible.builtin.file:
        path: "{{{{ render_out }}}}/{{{{ box_name }}}}/env"
        state: directory
        mode: '0700'
    - name: Write the specs
      ansible.builtin.copy:
        content: "{{{{ container_specs | to_nice_json }}}}"
        dest: "{{{{ render_out }}}}/{{{{ box_name }}}}/specs.json"
        mode: '0600'
    - name: Write the active definitions
      ansible.builtin.copy:
        content: >-
          {{{{ {{'services': active_services, 'accessories': active_accessories}}
             | to_nice_json }}}}
        dest: "{{{{ render_out }}}}/{{{{ box_name }}}}/active.json"
        mode: '0600'
    - name: Render service env files (as deploy_stack does)
      ansible.builtin.template:
        src: {env_j2}
        dest: "{{{{ render_out }}}}/{{{{ box_name }}}}/env/{{{{ item.key }}}}.env"
        mode: '0600'
      loop: "{{{{ active_services | dict2items }}}}"
      vars:
        env_service_name: "{{{{ item.key }}}}"
        env_clear: "{{{{ item.value.env.get('clear', {{}}) }}}}"
        env_secrets: "{{{{ item.value.env.get('secret', []) }}}}"
        env_database: "{{{{ item.value.database | default(false) }}}}"
      when: item.value.env is defined or item.value.database is defined
    - name: Render shared container env files (as deploy_stack does)
      ansible.builtin.template:
        src: {env_j2}
        dest: "{{{{ render_out }}}}/{{{{ box_name }}}}/env/{{{{ item.key }}}}.env"
        mode: '0600'
      loop: "{{{{ active_accessories | dict2items }}}}"
      vars:
        env_service_name: "{{{{ item.key }}}}"
        env_clear: "{{{{ item.value.env.get('clear', {{}}) }}}}"
        env_secrets: "{{{{ item.value.env.get('secret', []) }}}}"
        env_database: false
      when: item.value.env is defined
"""
    (workdir / "play.yml").write_text(play)
    plugins = os.pathsep.join(
        [str(root / "roles/container_lifecycle/filter_plugins"), str(root / "filter_plugins")]
    )
    env = {
        **os.environ,
        "ANSIBLE_FILTER_PLUGINS": plugins,
        "ANSIBLE_CONFIG": str(workdir / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
        "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ansible.cli.playbook",
            "-i",
            str(workdir / "inventory.json"),
            "-e",
            f"@{workdir / 'vars.json'}",
            str(workdir / "play.yml"),
        ],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError("the render play failed:\n" + proc.stdout[-4000:] + proc.stderr[-2000:])
    result: dict[str, dict[str, Any]] = {}
    for box in boxes:
        bdir = out / box
        envs = {p.stem: p.read_text() for p in sorted((bdir / "env").glob("*.env"))}
        result[box] = {
            "specs": {s["name"]: s for s in json.loads((bdir / "specs.json").read_text())},
            "active": json.loads((bdir / "active.json").read_text()),
            "env": envs,
        }
    return result


# ── Comparison ──────────────────────────────────────────────────────────────


def _env_pairs(text: str | None) -> list[tuple[str, str]]:
    if text is None:
        return []
    return [
        (line.split("=", 1)[0], line.split("=", 1)[1])
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    ]


def _raw(block: dict[str, Any] | None) -> dict[str, Any]:
    if block is None:
        return {}
    out: dict[str, Any] = {}
    for key in RAW_KEYS:
        if key in block:
            value = block[key]
            if key == "depends_on":
                value = sorted(value)
            out[key] = value
    return out


def _box_sets(active: dict[str, Any]) -> dict[str, Any]:
    services = active.get("services") or {}
    accessories = active.get("accessories") or {}
    dbs = sorted(
        {
            (
                str(svc["database"].get("accessory")),
                str(svc["database"].get("name", key)),
                str(svc["database"].get("user", key)),
                str(svc["database"].get("user", key)).upper().replace("-", "_")
                + "_POSTGRES_PASSWORD",
            )
            for key, svc in services.items()
            if svc.get("database")
        }
    )
    files: set[tuple[str, bool]] = set()
    for block in (*services.values(), *accessories.values()):
        public = block.get("config_files_mode") == "public"
        for f in block.get("config_files") or []:
            files.add((f, public))
    # A file another container already ships as public is public on the box.
    public_files = {f for f, p in files if p}
    return {
        "shared containers": sorted(accessories),
        "databases": [list(d) for d in dbs],
        "config files": sorted(
            {f"{f} ({'public' if f in public_files else 'private'})" for f, _ in files}
        ),
    }


def compare(
    fleet: str,
    original: dict[str, dict[str, Any]],
    compiled: dict[str, dict[str, Any]],
    exceptions: list[Exception_] | tuple[Exception_, ...] = (),
) -> tuple[list[ContainerDiff], list[str], int, list[tuple[str, str, str]]]:
    """Diff two renders. Return (diffs, env-order-only containers, count, excepted)."""
    diffs: list[ContainerDiff] = []
    order_only: list[str] = []
    excepted: list[tuple[str, str, str]] = []
    count = 0
    skip = {(e.container, e.key) for e in exceptions if e.fleet == fleet}
    for box in sorted(original):
        a, b = original[box], compiled[box]
        names = sorted(set(a["specs"]) | set(b["specs"]))
        for name in names:
            count += 1
            left = _container_view(a, name)
            right = _container_view(b, name)
            for key in sorted(set(left) | set(right)):
                if (name, key) not in skip:
                    continue
                if left.get(key) != right.get(key):
                    excepted.append((box, name, key))
                left.pop(key, None)
                right.pop(key, None)
            if left != right:
                keys = sorted(k for k in set(left) | set(right) if left.get(k) != right.get(k))
                text = "".join(
                    difflib.unified_diff(
                        json.dumps(left, indent=2, sort_keys=True).splitlines(keepends=True),
                        json.dumps(right, indent=2, sort_keys=True).splitlines(keepends=True),
                        fromfile=f"{box}/{name} (today)",
                        tofile=f"{box}/{name} (compiled)",
                    )
                )
                diffs.append(ContainerDiff(box, name, keys, text))
            elif a["env"].get(name) != b["env"].get(name):
                order_only.append(name)
        sa, sb = _box_sets(a["active"]), _box_sets(b["active"])
        for key in BOX_KEYS:
            if ("*", key) in skip:
                continue
            if sa[key] != sb[key]:
                text = "".join(
                    difflib.unified_diff(
                        [f"{x}\n" for x in map(str, sa[key])],
                        [f"{x}\n" for x in map(str, sb[key])],
                        fromfile=f"{box}: {key} (today)",
                        tofile=f"{box}: {key} (compiled)",
                    )
                )
                diffs.append(ContainerDiff(box, f"[{key}]", [key], text))
    return diffs, order_only, count, excepted


def _container_view(render: dict[str, Any], name: str) -> dict[str, Any]:
    spec = render["specs"].get(name)
    if spec is None:
        return {"missing": True}
    view = {k: v for k, v in spec.items()}
    active = render["active"]
    block = (active.get("services") or {}).get(name) or (active.get("accessories") or {}).get(name)
    view["env"] = dict(_env_pairs(render["env"].get(name)))
    view.update(_raw(block))
    return view


# ── Gate ────────────────────────────────────────────────────────────────────


def run_gate(
    fleet_path: Path,
    *,
    name: str | None = None,
    workdir: Path | None = None,
    exceptions: list[Exception_] | tuple[Exception_, ...] = (),
) -> GateResult:
    """Import, compile and compare one fleet. Writes only under ``workdir``."""
    import yaml

    from bay_cli import compiler
    from bay_cli.context import Context
    from bay_cli.plan import compiled_fleet

    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="bay-import-check-") as tmp:
            return run_gate(fleet_path, name=name, workdir=Path(tmp), exceptions=exceptions)

    legacy = load_legacy(fleet_path)
    imported = import_fleet(fleet_path, name)
    out = workdir / "fleet"
    imported.write(out)
    # Compile through the path `bay up` runs: a git repo, copied by
    # plan._materialize, never the working tree. A fleet input that path drops
    # (the files/ tree once) then shows here as a diff.
    _commit_scratch(out)
    with compiled_fleet(Context.for_fleet_root(out)) as comp:
        if comp.result is None:
            raise compiler.CompileError(comp.errors)
        result = comp.result
    gate = GateResult(
        fleet=imported.fleet_name,
        imported=imported,
        unsupported=[str(u) for u in result.unsupported],
    )
    compiled_data = yaml.safe_load(result.body())
    original_data = _original_data(legacy)

    boxes = _box_inputs(legacy, imported)
    text = "\n".join(legacy.texts) + "\n" + result.body()
    names = _secret_names(
        [
            original_data.get("services") or {},
            original_data.get("accessories") or {},
            compiled_data.get("services") or {},
            compiled_data.get("accessories") or {},
        ],
        text,
    )
    base = {n: placeholder(n) for n in sorted(names)}
    moved = dict(base)
    for move in imported.secret_moves:
        kind, ref = move.source
        moved[move.name] = (move.value or "") if kind == "literal" else placeholder(ref)
    original = render(original_data, boxes, base, workdir / "today")
    compiled = render(compiled_data, boxes, moved, workdir / "compiled")
    gate.diffs, gate.env_order_only, gate.containers, gate.excepted = compare(
        gate.fleet, original, compiled, exceptions
    )
    gate.renders = (original, compiled)
    return gate


def _commit_scratch(root: Path) -> None:
    """Make the imported fleet a one-commit git repo, as a real fleet is."""
    base = [
        "git", "-C", str(root), "-c", "user.name=bay", "-c", "user.email=bay@localhost",
        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
    ]
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "import"]):
        subprocess.run([*base, *args], check=True, capture_output=True, env=env)


def _original_data(legacy: Legacy) -> dict[str, Any]:
    data: dict[str, Any] = {"services": legacy.services, "accessories": legacy.accessories}
    if legacy.webhook:
        data["webhook"] = legacy.webhook
    plain: dict[str, Any] = json.loads(json.dumps(data, default=str))
    return plain


def _box_inputs(legacy: Legacy, imported: ImportResult) -> dict[str, dict[str, Any]]:
    wanted = legacy.var_names()
    # The webhook receiver on the build box lists every remote build of the
    # fleet; point build_server at the box whose group holds that machine.
    build_box = None
    server = legacy.group_vars.get("all", {}).get("build_server")
    for name, box in imported.boxes.items():
        if server is not None and server in legacy.hosts.get(box.group or box.env, []):
            build_box = f"box-{name}"
    out: dict[str, dict[str, Any]] = {}
    for name, box in imported.boxes.items():
        variables = {k: v for k, v in box.variables.items() if k in wanted}
        variables["gateway_bind_ip"] = box.tailnet_ip or "192.0.2.10"
        if build_box is not None:
            variables["build_server"] = build_box
        strategy = box.variables.get("git_deploy_build_strategy")
        if isinstance(strategy, str):
            variables["git_deploy_build_strategy"] = strategy
        out[name] = {"groups": box.groups, "vars": variables}
    return out
