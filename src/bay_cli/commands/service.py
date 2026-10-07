"""Service commands: list, show, catalog, prune-webhooks."""

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

app = typer.Typer(help="List services, show the catalog, and prune orphan GitHub webhooks.")


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
