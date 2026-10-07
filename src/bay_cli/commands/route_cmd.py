"""`bay route`: add, list, remove and import the tailnet routes in bay.fleet.toml.

A route gives a machine on the tailnet that Bay does not run a name with a
trusted certificate, served by the ingress box (``[tailnet] ingress_box``).
See docs/tailnet-ingress.md, "Routes in bay.fleet.toml (2.1)".

Every verb that writes edits ``bay.fleet.toml`` with a small text edit
(comments and order stay), checks the result, and commits the fleet repo
(``bay: route add <name>``) unless ``--no-commit``. ``bay up`` commits only
the compiled file; a route that sits uncommitted in the fleet file would be
compiled into a pushed ``services.yml`` that no other clone can reproduce.
Then run ``bay plan <env>`` for a project on the ingress box env.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Any

import typer

from bay_cli import console, gitrepo
from bay_cli.context import Context, context_from
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import FLEET_FILE, validate_fleet

app = typer.Typer(
    help="Tailnet routes in bay.fleet.toml: add, list, remove, import.", no_args_is_help=True
)

_NoCommitOpt = Annotated[
    bool, typer.Option("--no-commit", help="Edit bay.fleet.toml, do not commit the fleet repo.")
]
_IngressOpt = Annotated[
    str | None,
    typer.Option("--ingress-box", help="Set [tailnet] ingress_box: the box that serves routes."),
]
_CertOpt = Annotated[
    str | None,
    typer.Option("--cert-domain", help="Set [tailnet] cert_domain, such as *.ts.example.com."),
]


# ── helpers ─────────────────────────────────────────────────────────────────
# bay_cli.routes is imported inside each verb: every module the CLI imports at
# start-up is paid for by every invocation (tests/test_cli_import_time.py).


def _fleet_file(cx: Context) -> Path:
    path = cx.fleet_root / FLEET_FILE
    if not path.is_file():
        raise BayError(f"{FLEET_FILE} not found in {cx.fleet_root}", code=ErrorCode.NOT_FOUND)
    return path


def _in_git(root: Path) -> bool:
    return gitrepo.is_repo(root) and gitrepo.head(root) is not None


def _refuse_dirty(cx: Context, commit: bool) -> None:
    if commit and _in_git(cx.fleet_root) and gitrepo.path_dirty(cx.fleet_root, FLEET_FILE):
        raise BayError(
            f"{FLEET_FILE} has uncommitted changes, and the route edit would commit them too",
            code=ErrorCode.CONFLICT,
            hint="Commit or drop the change first, or pass --no-commit.",
        )


def _check(text: str) -> dict[str, Any]:
    """Refuse a fleet file the compile would refuse. Returns the parsed document."""
    from bay_cli import routes

    doc = tomllib.loads(text)
    problems = [f"{FLEET_FILE}: {v}" for v in validate_fleet(doc)]
    _, errors = routes.compile_routes(doc)
    problems += errors
    if problems:
        raise BayError(
            "the route does not check out:\n  " + "\n  ".join(problems),
            hint="Nothing was written.",
        )
    return doc


def _write(cx: Context, path: Path, text: str, *, commit: bool, message: str,
           extra: list[Path] | None = None) -> str | None:
    path.write_text(text)
    if not commit or not _in_git(cx.fleet_root):
        return None
    paths = [path, *(extra or [])]
    try:
        return gitrepo.commit_paths(cx.fleet_root, paths, message)
    except gitrepo.GitError as exc:
        raise BayError(f"cannot commit the fleet repo: {exc}") from None


def _next(doc: dict[str, Any], commit: str | None) -> None:
    from bay_cli import routes

    env = routes.ingress_env(doc) or "<env>"
    if commit:
        console.info(f"fleet commit {commit[:12]}")
    else:
        console.info(f"commit {FLEET_FILE} before bay up.")
    console.info(f"Next: run `bay plan {env}` for a project on {env}, then `bay up {env}`.")


def _warn_old_file(cx: Context) -> None:
    from bay_cli import routes

    if (cx.fleet_root / routes.OLD_FILE).is_file():
        console.warning(
            f"{routes.OLD_FILE} still defines routes; bay compile refuses both. "
            "Run `bay route import` to move them."
        )


# ── verbs ───────────────────────────────────────────────────────────────────


@app.command("add")
def add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Route name. Kept as the Traefik router name.")],
    domain: Annotated[str, typer.Option("--domain", help="The name, under cert_domain.")],
    upstream: Annotated[
        str,
        typer.Option("--upstream", help="http://<tailnet name or address>:<port>"),
    ],
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Host the upstream receives: client (default) or upstream "
            "(for a backend behind tailscale serve).",
        ),
    ] = "client",
    identity: Annotated[
        bool, typer.Option("--identity", help="Inject X-Tailnet-Device, the caller's device.")
    ] = False,
    alias: Annotated[
        list[str] | None, typer.Option("--alias", help="One more name for the route. Repeatable.")
    ] = None,
    entrypoint: Annotated[
        str | None, typer.Option("--entrypoint", help="Traefik entrypoint. Default: the role's.")
    ] = None,
    ingress_box: _IngressOpt = None,
    cert_domain: _CertOpt = None,
    no_commit: _NoCommitOpt = False,
) -> None:
    """Add a tailnet route to bay.fleet.toml.

    Examples:

        bay route add notes --domain notes.ts.example.com --upstream http://laptop:8080
        bay route add nas --domain nas.ts.example.com --upstream http://100.64.0.9:5000 \\
            --host upstream --identity
    """
    from bay_cli import routes

    if host not in (routes.HOST_CLIENT, routes.HOST_UPSTREAM):
        raise BayError(f"--host is {host}; use client or upstream")
    cx = context_from(ctx)
    path = _fleet_file(cx)
    _refuse_dirty(cx, not no_commit)
    route: dict[str, Any] = {"domain": domain, "upstream": upstream}
    if host == routes.HOST_UPSTREAM:
        route["host"] = host
    if identity:
        route["identity"] = True
    if alias:
        route["aliases"] = list(alias)
    if entrypoint:
        route["entrypoint"] = entrypoint
    text = path.read_text()
    keys = {k: v for k, v in (("ingress_box", ingress_box), ("cert_domain", cert_domain)) if v}
    if keys:
        text = routes.set_tailnet_keys(text, keys)
    text = routes.add_route_text(text, name, route)
    doc = _check(text)
    commit = _write(cx, path, text, commit=not no_commit, message=f"bay: route add {name}")
    if console.is_json_mode():
        console.emit_result({"route": name, **route, "commit": commit}, command="route add")
        return
    console.success(f"added route {name}: {domain} -> {upstream}")
    _warn_old_file(cx)
    _next(doc, commit)


@app.command("ls")
def ls(ctx: typer.Context) -> None:
    """List the tailnet routes in bay.fleet.toml: name, domain, upstream, host, identity."""
    from bay_cli import routes

    cx = context_from(ctx)
    doc = tomllib.loads(_fleet_file(cx).read_text())
    table = routes.table(doc)
    rows = [
        {
            "name": name,
            "domain": r.get("domain"),
            "upstream": r.get("upstream"),
            "host": r.get("host", routes.HOST_CLIENT),
            "identity": bool(r.get("identity", False)),
            "aliases": list(r.get("aliases", [])),
            "entrypoint": r.get("entrypoint"),
        }
        for name, r in sorted(table.items())
    ]
    if console.is_json_mode():
        tailnet = routes.tailnet_of(doc)
        console.emit_result(
            {
                "ingress_box": tailnet.get("ingress_box"),
                "cert_domain": tailnet.get("cert_domain"),
                "routes": rows,
            },
            command="route ls",
        )
        return
    if not rows:
        console.info("No tailnet routes. Add one with: bay route add <name> --domain --upstream")
        return
    table_rows = [("NAME", "DOMAIN", "UPSTREAM", "HOST", "IDENTITY")] + [
        (
            r["name"],
            r["domain"] + (f" (+{len(r['aliases'])})" if r["aliases"] else ""),
            r["upstream"],
            r["host"],
            "yes" if r["identity"] else "no",
        )
        for r in rows
    ]
    widths = [max(len(str(row[i])) for row in table_rows) for i in range(4)]
    for row in table_rows:
        typer.echo("  " + "  ".join(str(row[i]).ljust(widths[i]) for i in range(4)) + "  " + row[4])


@app.command("rm")
def rm(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Route name.")],
    no_commit: _NoCommitOpt = False,
) -> None:
    """Remove a tailnet route from bay.fleet.toml.

    Example:

        bay route rm notes
    """
    from bay_cli import routes

    cx = context_from(ctx)
    path = _fleet_file(cx)
    _refuse_dirty(cx, not no_commit)
    text = routes.remove_route_text(path.read_text(), name)
    doc = _check(text)
    commit = _write(cx, path, text, commit=not no_commit, message=f"bay: route rm {name}")
    if console.is_json_mode():
        console.emit_result({"route": name, "removed": True, "commit": commit}, command="route rm")
        return
    console.success(f"removed route {name}")
    _next(doc, commit)


def _group_vars(root: Path) -> dict[str, dict[str, Any]]:
    """Plain variables per group (never a secrets file, never an encrypted one)."""
    from bay_cli.importer import _is_secret_file, _read_yaml

    out: dict[str, dict[str, Any]] = {}
    gv = root / "group_vars"
    for gdir in sorted(p for p in gv.iterdir() if p.is_dir()) if gv.is_dir() else []:
        merged: dict[str, Any] = {}
        for path in sorted([*gdir.glob("*.yml"), *gdir.glob("*.yaml")]):
            if _is_secret_file(path):
                continue
            data = _read_yaml(path)
            if isinstance(data, dict):
                merged.update(data)
        out[gdir.name] = merged
    return out


@app.command("import")
def import_routes(
    ctx: typer.Context,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", help="The old file. Default: group_vars/all/tailnet_proxies.yml."
        ),
    ] = None,
    ingress_box: _IngressOpt = None,
    cert_domain: _CertOpt = None,
    no_commit: _NoCommitOpt = False,
) -> None:
    """Move tailnet_proxies from the old YAML file into bay.fleet.toml, names kept.

    The first name of each entry's domains becomes domain, the rest aliases.
    The old file is deleted. Run it once per fleet, then plan and up: the plan
    shows one route_added step per route, and the Traefik file on the box does
    not change. The ingress box and the certificate domain come from the
    options, else from [tailnet], else from tailnet_ingress_cert_domain in the
    group variables.

    Example:

        bay route import
    """
    from bay_cli import routes
    from bay_cli.importer import _read_yaml

    cx = context_from(ctx)
    path = _fleet_file(cx)
    _refuse_dirty(cx, not no_commit)
    source = (file if file is not None else cx.fleet_root / routes.OLD_FILE).resolve()
    if not source.is_file():
        raise BayError(f"{source} not found", code=ErrorCode.NOT_FOUND,
                       hint="Pass --file <path> to the YAML file with tailnet_proxies.")
    data = _read_yaml(source)
    if not isinstance(data, dict) or not isinstance(data.get("tailnet_proxies"), dict):
        raise BayError(f"{source} defines no tailnet_proxies map")
    others = sorted(str(k) for k in data if k != "tailnet_proxies")
    if others:
        raise BayError(
            f"{source} holds more than tailnet_proxies ({', '.join(others)}), so it "
            "cannot be deleted",
            hint="Move the other keys to another file first.",
        )
    table, problems = routes.from_proxies(data["tailnet_proxies"])
    if problems:
        raise BayError("the routes cannot be carried over:\n  " + "\n  ".join(problems))

    text = path.read_text()
    doc = tomllib.loads(text)
    tailnet = routes.tailnet_of(doc)
    taken = sorted(set(table) & set(routes.table(doc)))
    if taken:
        raise BayError(f"bay.fleet.toml already has route(s) {', '.join(taken)}",
                       code=ErrorCode.CONFLICT, hint="Remove them first, or edit the old file.")
    boxes = {
        name: {"group": (b or {}).get("group")} for name, b in (doc.get("boxes") or {}).items()
    }
    guessed_box, guessed_cert, why = routes.guess_ingress(_group_vars(cx.fleet_root), boxes)
    keys: dict[str, str] = {}
    box = ingress_box or tailnet.get("ingress_box") or guessed_box
    cert = cert_domain or tailnet.get("cert_domain") or guessed_cert
    if not box or not cert:
        raise BayError(
            f"cannot tell the ingress box and the certificate domain ({why})",
            hint="Pass --ingress-box <box> --cert-domain '*.ts.example.com'.",
        )
    if tailnet.get("ingress_box") != box:
        keys["ingress_box"] = box
    if tailnet.get("cert_domain") != cert:
        keys["cert_domain"] = cert
    if keys:
        text = routes.set_tailnet_keys(text, keys)
    for name in table:
        text = routes.add_route_text(text, name, table[name])
    doc = _check(text)

    extra: list[Path] = []
    rel = None
    try:
        rel = source.relative_to(cx.fleet_root.resolve())
    except ValueError:
        pass
    path.write_text(text)
    source.unlink()
    if rel is not None and _in_git(cx.fleet_root) and gitrepo.is_tracked(cx.fleet_root, str(rel)):
        extra.append(Path(rel))
    commit = _write(
        cx, path, text, commit=not no_commit,
        message=f"bay: import {len(table)} tailnet route(s)", extra=extra,
    )
    if console.is_json_mode():
        console.emit_result(
            {"routes": sorted(table), "ingress_box": box, "cert_domain": cert,
             "removed": str(source), "commit": commit},
            command="route import",
        )
        return
    for name in sorted(table):
        r = table[name]
        console.success(f"route {name}: {r['domain']} -> {r['upstream']}")
    console.info(f"ingress box {box}, cert_domain {cert}; removed {source}")
    console.info(
        "The plan shows one route_added step per route. The box's Traefik file stays the same."
    )
    _next(doc, commit)
