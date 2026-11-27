"""Service commands: list, show, catalog, prune-webhooks, add-webhook."""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from bay_cli import ansible as _ansible
from bay_cli import console
from bay_cli.context import context_or_cwd
from bay_cli.errors import BayError

app = typer.Typer(
    help="List services, show the catalog, add or repair GitHub push hooks, and prune orphan ones."
)


def _get_config(ctx: typer.Context):
    from bay_cli.config import StackConfig

    return StackConfig(context_or_cwd(ctx).fleet_root)


def _get_catalog(ctx: typer.Context):
    from bay_cli.catalog import load_catalog

    cx = context_or_cwd(ctx)
    return load_catalog(cx.framework_root, cx.fleet_root)


def _get_all_domain_bases(cfg: Any, env: str = "production") -> dict[str, str]:
    """Read domain_base from each region's group_vars.

    Returns a dict of {region_name: domain_base}. For single-server setups,
    returns {"": domain_base} if available.
    """
    from ruamel.yaml import YAML

    root = cfg._root
    inv_path = root / "hosts" / env
    if not inv_path.is_file():
        db = cfg.get_domain_base(env)
        return {"": db} if db else {}

    text = inv_path.read_text()
    if f"[{env}:children]" not in text:
        db = cfg.get_domain_base(env)
        return {"": db} if db else {}

    # Parse inventory children
    children: list[str] = []
    in_children = False
    for line in text.splitlines():
        line = line.strip()
        if line == f"[{env}:children]":
            in_children = True
            continue
        if in_children:
            if line.startswith("["):
                break
            if line and not line.startswith("#"):
                children.append(line)

    yaml = YAML()
    bases: dict[str, str] = {}
    for region in children:
        path = root / "group_vars" / region / "main.yml"
        if path.is_file():
            with path.open() as f:
                data = yaml.load(f)
            if isinstance(data, dict) and "domain_base" in data:
                bases[region] = str(data["domain_base"])
    return bases


def _filter_domain_bases(
    domain_bases: dict[str, str], regions: list[str]
) -> dict[str, str]:
    """Filter domain_bases to only include entries for the given regions.

    If regions is empty (meaning "all regions"), returns the full dict.
    """
    if not regions:
        return domain_bases
    return {r: b for r, b in domain_bases.items() if r in regions}


def _resolve_domains(
    domains: list[str], domain_bases: dict[str, str]
) -> list[str]:
    """Expand {{ domain_base }} templates using per-region values."""
    resolved: list[str] = []
    for domain in domains:
        if "{{ domain_base }}" in domain or "{{domain_base}}" in domain:
            for base in domain_bases.values():
                expanded = domain.replace("{{ domain_base }}", base)
                expanded = expanded.replace("{{domain_base}}", base)
                resolved.append(expanded)
        else:
            resolved.append(domain)
    return resolved


@app.command("list")
def list_services(
    ctx: typer.Context,
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """List all configured services and accessories.

    Examples:

        bay service list
        bay --json service list
    """
    cfg = _get_config(ctx)
    services = cfg.get_services()
    accessories = cfg.get_accessories()
    domain_bases = _get_all_domain_bases(cfg, env)

    if console.is_json_mode():
        svc_list = []
        for name, svc in services.items():
            raw_domains = list(svc.get("domains", []))
            svc_regions = list(svc.get("regions", []))
            filtered_bases = _filter_domain_bases(domain_bases, svc_regions)
            svc_list.append({
                "name": name,
                "category": "service",
                "image": svc.get("image", ""),
                "access": svc.get("access", ""),
                "domains": _resolve_domains(raw_domains, filtered_bases),
                "domains_raw": raw_domains,
                "regions": svc_regions,
                "update": svc.get("update", "monitor"),
                "public_routes": list(svc.get("public_routes", [])),
                "links_count": len(svc.get("links", {})),
            })
        acc_list = []
        for name, acc in accessories.items():
            acc_list.append({
                "name": name,
                "category": "accessory",
                "image": acc.get("image", ""),
                "port": str(acc.get("port", "")),
                "regions": list(acc.get("regions", [])),
                "update": acc.get("update", "monitor"),
            })
        console.emit_result(
            {"services": svc_list, "accessories": acc_list},
            command="service.list",
        )
        return

    # ── Services table ──────────────────────────────────────────────
    if services:
        svc_table = Table(title="Services", show_edge=False, pad_edge=False)
        svc_table.add_column("Name", style="bold", width=16)
        svc_table.add_column("Access", width=8)
        svc_table.add_column("Domains", min_width=30)
        svc_table.add_column("Regions", width=10)
        svc_table.add_column("Update", width=10)

        for name, svc in services.items():
            access = svc.get("access", "")
            if access == "public":
                access_display = "[green]public[/green]"
            elif access == "vpn":
                access_display = "[yellow]vpn[/yellow]"
                public_routes = svc.get("public_routes", [])
                if public_routes:
                    access_display += "[dim]*[/dim]"
            else:
                access_display = access

            raw_domains = list(svc.get("domains", []))
            regions = svc.get("regions", [])
            filtered_bases = _filter_domain_bases(domain_bases, list(regions))
            resolved = _resolve_domains(raw_domains, filtered_bases)
            domains_display = "\n".join(resolved) if resolved else "[dim]none[/dim]"
            regions_display = ", ".join(regions) if regions else "all"

            update = str(svc.get("update", "monitor"))
            if update == "auto":
                update_display = "[green]auto[/green]"
            elif update == "false" or update == "False":
                update_display = "[red]off[/red]"
            else:
                update_display = "[dim]monitor[/dim]"

            svc_table.add_row(name, access_display, domains_display, regions_display, update_display)

        console.console.print()
        console.console.print(svc_table)

    # ── Accessories table ───────────────────────────────────────────
    if accessories:
        acc_table = Table(title="Accessories", show_edge=False, pad_edge=False)
        acc_table.add_column("Name", style="bold", width=16)
        acc_table.add_column("Image", min_width=20)
        acc_table.add_column("Port", width=22)
        acc_table.add_column("Regions", width=10)
        acc_table.add_column("Update", width=10)

        for name, acc in accessories.items():
            port = str(acc.get("port", ""))
            port_display = port if port else "[dim]-[/dim]"

            regions = acc.get("regions", [])
            regions_display = ", ".join(regions) if regions else "all"

            update = str(acc.get("update", "monitor"))
            if update == "auto":
                update_display = "[green]auto[/green]"
            elif update == "false" or update == "False":
                update_display = "[red]off[/red]"
            else:
                update_display = "[dim]monitor[/dim]"

            acc_table.add_row(name, str(acc.get("image", "")), port_display, regions_display, update_display)

        console.console.print()
        console.console.print(acc_table)

    console.console.print()


@app.command()
def show(ctx: typer.Context, name: str = typer.Argument(help="Service or accessory name")) -> None:
    """Show the full configuration for a service or accessory.

    Includes the LINKS_* env var names generated for cross-region links.

    Examples:

        bay service show myapp
        bay --json service show postgres
    """
    cfg = _get_config(ctx)
    svc = cfg.get_service(name)

    if svc is None:
        raise BayError.not_found("service", name)

    if console.is_json_mode():
        console.emit_result({"service": svc}, command="service.show")
        return

    from ruamel.yaml import YAML
    from io import StringIO

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)

    buf = StringIO()
    yaml.dump({name: svc}, buf)

    console.console.print()
    console.console.print(f"[bold]{name}[/bold]")
    console.console.print()
    console.console.print(buf.getvalue().rstrip())
    console.console.print()

    # Show link environment variable names
    links = svc.get("links", {})
    if links:
        from bay_cli.links import link_env_var_name

        console.console.print()
        console.console.print("[bold]Cross-Region Links[/bold]")
        console.console.print()
        for target, link_cfg in links.items():
            prefix = link_env_var_name(target)
            region = link_cfg.get("region", "?")
            console.console.print(f"  {target} (region: {region})")
            console.console.print(f"    LINKS_{prefix}_HOST")
            console.console.print(f"    LINKS_{prefix}_PORT")
            console.console.print(f"    LINKS_{prefix}_URL")


@app.command()
def catalog(
    ctx: typer.Context,
    filter: str | None = typer.Option(
        None,
        "--filter",
        "-f",
        help="Filter by category: service or accessory",
    ),
) -> None:
    """List available service/accessory definitions from the catalog.

    To run a catalog app, write a bay.toml for it (see docs/bay-toml.md).

    Examples:

        bay service catalog
        bay service catalog --filter accessory
    """
    entries = _get_catalog(ctx)

    if filter:
        entries = {k: v for k, v in entries.items() if v.category == filter}

    if console.is_json_mode():
        entry_list = [
            {
                "id": e.id,
                "name": e.name,
                "category": e.category,
                "dependencies": e.dependencies,
                "default_access": e.default_access,
                "domain_prefix": e.domain_prefix,
            }
            for e in entries.values()
        ]
        console.emit_result({"entries": entry_list}, command="service.catalog")
        return

    table = Table(title="Available Catalog Entries")
    table.add_column("ID", style="bold")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Access")
    table.add_column("Dependencies")

    for entry in entries.values():
        deps = ", ".join(entry.dependencies) if entry.dependencies else ""
        table.add_row(
            entry.id,
            entry.name,
            entry.category,
            entry.default_access,
            deps,
        )

    console.console.print()
    console.console.print(table)
    console.console.print()


# ── Vault token helper ─────────────────────────────────────────────────────


def _read_vault_token(root: Path, env: str, key: str) -> str | None:
    """Read a single key from vault-encrypted secrets.yml.

    Tries group_vars/<env>/secrets.yml first, then group_vars/all/secrets.yml.
    Returns None on any failure (file not found, no .vault_pass, decrypt error,
    key not present).  The vault password file must exist at <root>/.vault_pass.
    """
    from ruamel.yaml import YAML

    vault_pass = root / ".vault_pass"
    if not vault_pass.exists():
        return None

    candidates = [
        root / "group_vars" / env / "secrets.yml",
        root / "group_vars" / "all" / "secrets.yml",
    ]

    yaml = YAML()

    for path in candidates:
        if not path.is_file():
            continue
        try:
            first_line = path.read_text(errors="replace").split("\n", 1)[0]
        except OSError:
            continue

        if first_line.strip().startswith("$ANSIBLE_VAULT"):
            try:
                proc = subprocess.run(
                    [
                        _ansible.tool("ansible-vault"), "decrypt",
                        "--vault-password-file", str(vault_pass),
                        "--output=-",
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode != 0:
                    continue
                data = yaml.load(StringIO(proc.stdout))
            except Exception:
                continue
        else:
            try:
                with path.open() as fh:
                    data = yaml.load(fh)
            except Exception:
                continue

        if not isinstance(data, dict):
            continue

        # Unwrap `secrets:` key if present
        secrets = data.get("secrets", data) if isinstance(data.get("secrets"), dict) else data
        val = secrets.get(key)
        if val:
            return str(val)

    return None


# ── prune-webhooks command ─────────────────────────────────────────────────


@app.command("prune-webhooks")
def prune_webhooks(
    ctx: typer.Context,
    repo: str = typer.Argument(help="GitHub repo in owner/name format (e.g., MyOrg/myapp)"),
    env: str = typer.Option("production", "--env", "-e", help="Target environment for vault and webhook_domain lookup."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphan hooks without deleting them."),
) -> None:
    """List and optionally delete orphan GitHub webhooks for a repository.

    Orphans are hooks under this consumer's webhook domain that no current
    service claims. Requires github_admin_token in the vault.

    Examples:

        bay service prune-webhooks MyOrg/myapp --dry-run
        bay service prune-webhooks MyOrg/myapp
    """
    from rich.prompt import Confirm
    from rich.table import Table as RichTable

    import requests as _requests

    from bay_cli.github_webhook import (
        find_orphan_hooks,
        list_repo_hooks,
        resolve_webhook_domain,
    )

    cfg = _get_config(ctx)

    # 1. Resolve webhook_domain
    webhook_domain = resolve_webhook_domain(cfg._root, env)
    if webhook_domain is None:
        raise BayError.config(
            "webhook_domain not configured",
            hint="Set webhook_domain in group_vars/all/main.yml",
        )

    # 2. Resolve github_admin_token from vault
    token = _read_vault_token(cfg._root, env, "github_admin_token")
    if not token:
        raise BayError.config(
            "github_admin_token not found in vault",
            hint=f"Add a read-only GitHub PAT with 'repo' scope via 'bay vault edit {env}'",
        )

    # 3. Parse repo argument
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise BayError.config(
            "repo must be in owner/name format",
            hint="e.g., MyOrg/myapp",
        )
    owner, repo_name = parts[0], parts[1]

    # 4. Fetch hooks
    try:
        hooks = list_repo_hooks(owner, repo_name, token)
    except _requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in (401, 403):
            raise BayError.config(
                f"github_admin_token lacks read access to hooks on {repo} (HTTP {code})",
                hint="Token needs 'repo' scope",
            )
        if code == 404:
            raise BayError.not_found("repo", repo)
        raise BayError(f"GitHub API error: HTTP {code}")

    # 5. Find orphans scoped to this consumer
    service_names = set(cfg.get_services().keys())
    orphans = find_orphan_hooks(hooks, webhook_domain, service_names)

    if not orphans:
        console.success(f"No orphan webhooks found for {repo}")
        if console.is_json_mode():
            console.emit_result(
                {"repo": repo, "orphans": [], "deleted": [], "dry_run": dry_run},
                command="service.prune-webhooks",
            )
        return

    # 6. Display orphans table
    table = RichTable(title=f"Orphan webhooks for {repo}", show_edge=False, pad_edge=False)
    table.add_column("Hook ID", style="bold", width=12)
    table.add_column("URL", min_width=40)
    table.add_column("Last Response", width=16)
    table.add_column("Created At", width=22)

    for h in orphans:
        hook_id = str(h.get("id", ""))
        url = h.get("config", {}).get("url", "")
        last_resp = h.get("last_response") or {}
        last_code = str(last_resp.get("code", "—")) if isinstance(last_resp, dict) else "—"
        created_at = str(h.get("created_at", ""))
        table.add_row(hook_id, url, last_code, created_at)

    console.console.print()
    console.console.print(table)
    console.console.print()

    orphan_summary = [{"id": h.get("id"), "url": h.get("config", {}).get("url", "")} for h in orphans]

    if dry_run:
        if console.is_json_mode():
            console.emit_result(
                {"repo": repo, "orphans": orphan_summary, "deleted": [], "dry_run": True},
                command="service.prune-webhooks",
            )
        return

    # 7. Confirm deletion
    if not console.is_yes_mode() and not console.is_json_mode():
        if not Confirm.ask(f"Delete {len(orphans)} orphan webhook(s) from {repo}?", default=False):
            console.info("Aborted")
            return

    # 8. Delete
    import requests as _req2

    deleted_ids: list[int | str] = []
    for h in orphans:
        hook_id = h.get("id")
        hook_url_val = h.get("config", {}).get("url", "")
        try:
            resp = _req2.delete(
                f"https://api.github.com/repos/{owner}/{repo_name}/hooks/{hook_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            console.success(f"Deleted hook {hook_id}: {hook_url_val}")
            deleted_ids.append(hook_id)
        except _req2.RequestException as e:
            console.warning(f"Failed to delete hook {hook_id}: {e}")

    if console.is_json_mode():
        console.emit_result(
            {"repo": repo, "orphans": orphan_summary, "deleted": deleted_ids, "dry_run": False},
            command="service.prune-webhooks",
        )


# ── add-webhook command (2.27.0) ───────────────────────────────────────────


def _vault_secret(cx: Any, box_env: str, name: str) -> str | None:
    """The value of ``secrets.<name>`` as the deploy of ``box_env`` reads it. None when unset.

    Ansible replaces a mapping, it does not merge it: a host of ``box_env``
    sees the ``secrets`` of ``group_vars/<box_env>/secrets.yml`` when that
    file exists, else those of ``group_vars/all/secrets.yml``. The file is
    decrypted into memory only; no error text carries its content.
    """
    from bay_cli.secrets_check import secrets_file_for

    path = secrets_file_for(cx, box_env)
    if path is None:
        return None
    data: Any = None
    raw = path.read_text(errors="replace")
    if raw.lstrip().startswith("$ANSIBLE_VAULT"):
        if not cx.vault_pass.exists():
            return None
        try:
            proc = subprocess.run(
                [
                    _ansible.tool("ansible-vault"), "decrypt",
                    "--vault-password-file", str(cx.vault_pass),
                    "--output=-", str(path),
                ],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        raw = proc.stdout
    import yaml

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    secrets = data.get("secrets")
    value = secrets.get(name) if isinstance(secrets, dict) else None
    return str(value) if value else None


def _build_server_place(cx: Any, fleet_doc: dict[str, Any], box_env: str) -> tuple[list[str], str]:
    """``(inventory groups of the build server host, its box env)``."""
    from bay_cli import plan as planmod
    from bay_cli import receipts

    server = planmod._group_var(cx, box_env, "build_server")
    if not server:
        raise BayError.config(
            f"a remote build needs build_server, and group_vars of {box_env} do not set it",
            hint="Set build_server to the inventory host that builds.",
        )
    if "{{" in str(server):
        raise BayError.config(
            f"build_server is a Jinja value ({server}); Bay cannot tell where a push must land",
            hint="Set build_server to the inventory host name itself.",
        )
    try:
        names = receipts.inventory_names(cx.fleet_root, cx.framework_root)
    except RuntimeError as exc:
        raise BayError(f"cannot read the inventory to find the build server: {exc}") from None
    host = str(server)
    groups = sorted(g for g, hosts in names.items() if host in hosts and g != host)
    if host not in names:
        raise BayError.config(
            f"build_server {host} is not a host of the inventory",
            hint="Set build_server to a host in hosts/<env>.",
        )
    envs = sorted({str(b.get("env")) for b in (fleet_doc.get("boxes") or {}).values()
                   if isinstance(b, dict) and b.get("env")})
    env = box_env if box_env in groups else next((e for e in envs if e in groups), None)
    if env is None:
        raise BayError.config(
            f"build_server {host} is in no box env of bay.fleet.toml, so Bay cannot tell "
            "which vault holds the secret of its receiver",
        )
    return groups, env


def _render_rows(doc: dict[str, Any]) -> None:
    for row in doc["hooks"]:
        built = (
            f" (also builds {', '.join(row['members'][1:])})" if len(row["members"]) > 1 else ""
        )
        line = f"{row['container']}: {row['action']}"
        if row["verification"]:
            line += f", {row['verification']}"
        typer.echo(f"{line}{built}")
        typer.echo(f"  repo {row['repo']}, url {row['url']}")
        if row["problems"] and row["action"] in ("broken", "repaired"):
            typer.echo(f"  {'was' if row['action'] == 'repaired' else 'is'}: "
                       + "; ".join(row["problems"]))
        if row["detail"]:
            typer.echo(f"  {row['detail']}")
        for url in row["redundant_hooks"]:
            typer.echo(f"  warning: a second hook {url} is redundant; one hook builds the group")
        for url in row["stale_hooks"]:
            typer.echo(f"  warning: an old hook {url} names the same container; remove it "
                       "in the repo settings")
        if row["next"]:
            typer.echo(f"  next: {row['next']}")
    for skip in doc["skipped"]:
        typer.echo(f"{skip['container']}: skipped, {skip['reason']}")


@app.command("add-webhook")
def add_webhook(
    ctx: typer.Context,
    project: str = typer.Argument(help="The project whose push hooks to create or repair."),
    env: str | None = typer.Option(
        None,
        "--env",
        "-e",
        help="The environment (deploy.<env>). Default: the fleet's primary env.",
    ),
    check: bool = typer.Option(
        False,
        "--check",
        help="Only read the hooks and report; write nothing. Exit 1 when one is missing "
        "or broken.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON document."),
) -> None:
    """Create or repair the GitHub push hook of each build container of a project.

    A push reaches Bay through a GitHub hook on the app repo:
    https://<webhook domain>/webhook/<container>. For a local build that is
    the domain of the box that runs the container; for a remote build, the
    domain of the build server. The extra services of one bay.toml need one
    hook. The hook is active, sends push events as JSON, checks TLS and
    carries the secret that [webhook] secret of bay.fleet.toml names. Every
    write sends the full config. After a create or a repair the verb sends
    a ping and reports the receiver's answer.

    bay init and bay up never create a hook. A hook is a write to an outside
    service with a shared secret, it is outside the plan, and before the
    first bay up the receiver does not know the service. Run this verb once
    after the first bay up of a build project.

    Needs github_admin_token in the vault, with write access to the webhooks
    of the repo (classic token: admin:repo_hook; fine-grained token:
    Webhooks read and write). Exit code: 0 when every hook is ok or was
    made, 1 otherwise.

    Examples:

        bay service add-webhook shop
        bay service add-webhook shop --env staging --check
    """
    from bay_cli.commands.project_cmd import (
        _echo_json,
        _json_error,
        _read_identity,
        find_bay_toml,
        fleet_context,
    )

    try:
        here = find_bay_toml(Path.cwd())
        _, fleet_name = _read_identity(here) if here else (None, None)
        cx = fleet_context(ctx, fleet_name, allow_cwd=True)
        doc = _add_webhook(cx, project, env, check=check)
    except BayError as exc:
        if not as_json and not console.is_json_mode():
            raise
        _json_error(exc)
    if as_json or console.is_json_mode():
        _echo_json(doc)
    else:
        _render_rows(doc)
    raise typer.Exit(int(doc["exit_code"]))


def _add_webhook(
    cx: Any,
    project: str,
    env: str | None,
    *,
    check: bool,
    http: Any = None,
    sleep: Any = None,
) -> dict[str, Any]:
    """The verb without the CLI: resolve everything, then make the GitHub calls.

    ``http`` replaces the ``requests`` module and ``sleep`` the wait between
    two polls (tests).
    """
    import time

    from bay_cli import lockfile
    from bay_cli import plan as planmod
    from bay_cli.errors import ErrorCode
    from bay_cli.github_webhook import GitHubHooks, ensure_hook, hook_targets

    proj = planmod.load_project(cx, project, cwd=Path.cwd(), fetch=False)
    env = env or proj.primary_env
    pin = lockfile.env_pin(proj.lock, env)
    pinned = planmod.doc_at(proj, pin)
    if pinned is None:
        raise BayError(
            f"{project} has no pinned bay.toml for {env}, so Bay does not know its containers yet",
            hint=f"Run `bay up {env} --project {project}` first, then run this again.",
        )
    names = planmod.project_containers(project, pinned, proj.lock, proj.primary_env).get(env)
    if not names:
        raise BayError(
            f"{project} has no [deploy.{env}] in its pinned bay.toml", code=ErrorCode.NOT_FOUND
        )
    box, box_env = planmod.resolve_box(proj, env, pinned)
    if box is None or box_env is None:
        raise BayError.config(f"{project} {env}: box {box} is not in bay.fleet.toml [boxes]")
    services, _state = planmod.current_services(cx)
    services = services.get("services") or {}
    default = planmod._group_var(cx, box_env, "git_deploy_build_strategy")
    targets, skipped = hook_targets(
        proj.fleet,
        services,
        sorted(set(names.values())),
        main=names.get("web"),
        box=box,
        box_env=box_env,
        strategy_of=lambda entry: planmod.build_strategy(entry, default),
        build_server=lambda: _build_server_place(cx, proj.fleet, box_env),
    )
    if not targets:
        why = "; ".join(f"{s['container']}: {s['reason']}" for s in skipped)
        raise BayError(
            f"{project} has no build container in {env} that a push must reach"
            + (f" ({why})" if why else ""),
            code=ErrorCode.NOT_FOUND,
            hint="Only a build with strategy local or remote from a github.com repo needs a hook.",
        )

    # Every credential before any call: a missing one writes nothing.
    token = _read_vault_token(cx.fleet_root, box_env, "github_admin_token")
    if not token:
        raise BayError.config(
            f"github_admin_token not found in the vault of {box_env}",
            hint=f"Add a GitHub token via `bay vault edit {box_env}`. It needs write access to "
            "the webhooks of the repo: a classic token needs admin:repo_hook, a fine-grained "
            "token needs Webhooks: Read and write. Without one, register the hook by hand "
            "(docs/build-pipeline.md).",
        )
    secret_name = str((proj.fleet.get("webhook") or {}).get("secret") or "")
    secrets: dict[str, str] = {}
    if not check:
        for target_env in sorted({t.secret_env for t in targets}):
            value = _vault_secret(cx, target_env, secret_name) if secret_name else None
            if not value:
                raise BayError.config(
                    f"the webhook secret {secret_name or '(none)'} that [webhook] secret names "
                    f"is not in the vault of {target_env}; no hook was written",
                    hint=f"Add it via `bay vault edit {target_env}`, run `bay up {env}`, then "
                    "run this again.",
                )
            secrets[target_env] = value

    client = GitHubHooks(token, http=http, secrets=tuple(secrets.values()))
    listed: dict[str, list[dict[str, Any]]] = {}
    rows = []
    for target in targets:
        if target.repo not in listed:
            listed[target.repo] = client.list_hooks(target.owner, target.name)
        rows.append(
            ensure_hook(
                client,
                target,
                secrets.get(target.secret_env, ""),
                listed[target.repo],
                project=project,
                env=env,
                check=check,
                sleep=sleep or time.sleep,
            )
        )
    good = {"ok", "created", "repaired"}
    failed = any(r["action"] not in good for r in rows)
    return {
        "project": project,
        "env": env,
        "check": check,
        "hooks": rows,
        "skipped": skipped,
        "result": "failed" if failed else "ok",
        "exit_code": 1 if failed else 0,
    }
