"""Service management commands: list, show, catalog, add, edit, remove."""

from __future__ import annotations

import copy
import secrets as secrets_mod
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from bay_cli import console
from bay_cli.catalog import CatalogEntry
from bay_cli.errors import BayError

app = typer.Typer(help="Manage services and accessories in services.yml.")


def _get_config():
    from bay_cli.config import StackConfig
    from bay_cli.paths import consumer_root, find_bay_dir

    try:
        root = consumer_root(find_bay_dir())
    except BayError:
        root = Path.cwd()
    return StackConfig(root)


def _get_catalog():
    from bay_cli.catalog import _package_framework_root, load_catalog
    from bay_cli.paths import consumer_root, find_bay_dir

    try:
        bay_dir = find_bay_dir()
        return load_catalog(bay_dir, consumer_root(bay_dir))
    except BayError:
        fw_root = _package_framework_root()
        return load_catalog(fw_root, Path.cwd())


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
    env: str = typer.Option("production", "--env", "-e", help="Target environment."),
) -> None:
    """List all configured services and accessories.

    Examples:

        bin/bay service list
        bin/bay --json service list
    """
    cfg = _get_config()
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
def show(name: str = typer.Argument(help="Service or accessory name")) -> None:
    """Show the full configuration for a service or accessory.

    Includes the LINKS_* env var names generated for cross-region links.

    Examples:

        bin/bay service show myapp
        bin/bay --json service show postgres
    """
    cfg = _get_config()
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
    filter: str | None = typer.Option(
        None,
        "--filter",
        "-f",
        help="Filter by category: service or accessory",
    ),
) -> None:
    """List available service/accessory definitions from the catalog.

    Catalog IDs feed `bin/bay service add <id>`.

    Examples:

        bin/bay service catalog
        bin/bay service catalog --filter accessory
    """
    entries = _get_catalog()

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


# ── Add command helpers ──────────────────────────────────────────────


def _regions_overlap(a: list[str], b: list[str]) -> bool:
    """Check if two region lists overlap.

    An empty list means "deploys everywhere", which overlaps with anything.
    """
    if not a or not b:
        return True
    return bool(set(a) & set(b))


def _build_domain(
    domain_prefix: str | None,
    cfg: Any,
    is_multi: bool,
    env: str = "production",
) -> str | None:
    """Construct a domain string from prefix and domain_base.

    Returns None if domain_prefix is None or domain_base is unavailable.
    """
    if not domain_prefix:
        return None
    domain_base = cfg.get_domain_base(env)
    if domain_base is None:
        return None
    if is_multi:
        from ruamel.yaml.scalarstring import SingleQuotedScalarString

        return SingleQuotedScalarString(f"{domain_prefix}.{{{{ domain_base }}}}")
    return f"{domain_prefix}.{domain_base}"


def _resolve_access(
    catalog_entry: CatalogEntry | None,
    cfg: Any,
    access_flag: str | None,
) -> str:
    """Determine the access mode for a new service."""
    if access_flag:
        return access_flag
    base = catalog_entry.default_access if catalog_entry else "vpn"
    if not cfg.load_gateway():
        return "public"
    return base


def _check_domain_collision(
    domain: str,
    new_regions: list[str],
    services: dict[str, Any],
    skip_name: str | None = None,
) -> None:
    """Raise BayError.conflict if domain is already in use by an overlapping-region service."""
    for svc_name, svc_block in services.items():
        if svc_name == skip_name:
            continue
        existing_domains = svc_block.get("domains", [])
        existing_regions = list(svc_block.get("regions", []))
        if domain in existing_domains and _regions_overlap(new_regions, existing_regions):
            raise BayError.conflict("domain", str(domain), svc_name)


def _check_port_collision(
    port_value: str,
    new_regions: list[str],
    services: dict[str, Any],
    accessories: dict[str, Any],
    skip_name: str | None = None,
) -> None:
    """Raise BayError.conflict if host port binding collides with an existing entry."""
    # Extract host port from binding like "127.0.0.1:5432:5432" or "5432:5432"
    parts = str(port_value).split(":")
    if len(parts) >= 2:
        host_port = parts[-2]
    else:
        host_port = parts[0]

    for source in (services, accessories):
        for entry_name, entry_block in source.items():
            if entry_name == skip_name:
                continue
            existing_port = entry_block.get("port")
            if not existing_port:
                continue
            existing_parts = str(existing_port).split(":")
            if len(existing_parts) >= 2:
                existing_host_port = existing_parts[-2]
            else:
                existing_host_port = existing_parts[0]
            existing_regions = list(entry_block.get("regions", []))
            if host_port == existing_host_port and _regions_overlap(new_regions, existing_regions):
                raise BayError.conflict("port", str(port_value), entry_name)


def _copy_config_files(
    catalog_entry: CatalogEntry,
    consumer_root: Path,
) -> list[str]:
    """Copy config files from catalog definition to consumer's files/ dir.

    Only copies if the destination does not already exist. Returns list of
    copied file paths (relative to consumer root).
    """
    if not catalog_entry.config_files:
        return []

    files_src_dir = catalog_entry.definition_path.parent / "files"
    if not files_src_dir.is_dir():
        return []

    files_dst_dir = consumer_root / "files"
    copied: list[str] = []

    for config_file in catalog_entry.config_files:
        src = files_src_dir / config_file
        dst = files_dst_dir / config_file
        if dst.exists():
            continue
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(str(Path("files") / config_file))

    return copied


def _add_single_entry(
    name: str,
    spec_block: dict[str, Any],
    category: str,
    domain: str | None,
    access: str | None,
    regions: list[str] | None,
    cfg: Any,
    services: dict[str, Any],
    accessories: dict[str, Any],
) -> None:
    """Add a single service/accessory to the config, with collision checks."""
    new_regions = regions or []

    # Domain collision check (services only)
    if domain and category == "service":
        _check_domain_collision(domain, new_regions, services, skip_name=name)

    # Port collision check (accessories with port bindings)
    port = spec_block.get("port")
    if port:
        _check_port_collision(str(port), new_regions, services, accessories, skip_name=name)

    # Build the final block
    block = copy.deepcopy(spec_block)

    if category == "service":
        if domain:
            block["domains"] = [domain]
        if access:
            block["access"] = access

    if regions:
        block["regions"] = regions

    cfg.add_service(name, block, category=category)


@app.command()
def add(
    catalog_id: str | None = typer.Argument(
        None,
        help="Catalog entry ID to add, or omit for a custom service",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        "-n",
        help="Service name (defaults to catalog ID)",
    ),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Docker image (mutually exclusive with --build-repo)",
    ),
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Container port",
    ),
    access: str | None = typer.Option(
        None,
        "--access",
        "-a",
        help="Access mode: vpn or public",
    ),
    domain: str | None = typer.Option(
        None,
        "--domain",
        "-d",
        help="Override auto-constructed domain",
    ),
    build_repo: str | None = typer.Option(
        None,
        "--build-repo",
        help="Git repo URL for build-from-source (mutually exclusive with --image)",
    ),
    build_branch: str | None = typer.Option(
        None,
        "--build-branch",
        help="Git branch for build-from-source",
    ),
    build_dockerfile: str | None = typer.Option(
        None,
        "--build-dockerfile",
        help="Dockerfile path for build-from-source (monorepo support)",
    ),
    build_context: str | None = typer.Option(
        None,
        "--build-context",
        help="Build context path for build-from-source (monorepo support)",
    ),
    build_token: str | None = typer.Option(
        None,
        "--build-token",
        help="Vault key name for git access token (private repos)",
    ),
    database: str | None = typer.Option(
        None,
        "--database",
        help="Bind to a database accessory (e.g., postgres)",
    ),
    region: list[str] | None = typer.Option(
        None,
        "--region",
        "-r",
        help="Restrict to specific region(s)",
    ),
    no_deps: bool = typer.Option(
        False,
        "--no-deps",
        help="Skip automatic dependency resolution",
    ),
    link: list[str] | None = typer.Option(
        None,
        "--link",
        "-l",
        help="Cross-region link: TARGET:REGION (repeatable)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show YAML diff without saving",
    ),
) -> None:
    """Add a service or accessory from the catalog or a custom definition.

    Edits services.yml only — run `bin/bay deploy <env>` to apply.
    Catalog adds resolve dependencies automatically (--no-deps to skip)
    and copy starter config files. Custom services need --image or
    --build-repo, plus --port.

    Examples:

        bin/bay service add gatus
        bin/bay service add n8n --region eu --link postgres:eu
        bin/bay service add --name myapp --image nginx:alpine --port 8080
        bin/bay service add --name api --port 3000 \\
            --build-repo git@github.com:me/api.git --build-token GITHUB_TOKEN
        bin/bay service add postgres --dry-run
    """
    from bay_cli.catalog import resolve_dependencies

    # ── Validate mutually exclusive flags ────────────────────────────
    if image and build_repo:
        raise BayError.config(
            "--image and --build-repo are mutually exclusive",
            hint="Use --image for pre-built images, --build-repo for build-from-source",
        )
    if build_branch and not build_repo:
        raise BayError.config(
            "--build-branch requires --build-repo",
            hint="Provide --build-repo to use build-from-source",
        )
    if build_dockerfile and not build_repo:
        raise BayError.config(
            "--build-dockerfile requires --build-repo",
            hint="Provide --build-repo to use build-from-source",
        )
    if build_context and not build_repo:
        raise BayError.config(
            "--build-context requires --build-repo",
            hint="Provide --build-repo to use build-from-source",
        )
    if build_token and not build_repo:
        raise BayError.config(
            "--build-token requires --build-repo",
            hint="Provide --build-repo to use build-from-source",
        )

    # ── Validate database flag early (needs cfg) ────────────────────
    # (full validation deferred until after cfg is loaded below)

    # ── Load config and catalog ──────────────────────────────────────
    cfg = _get_config()
    all_catalog = _get_catalog()
    catalog_entry: CatalogEntry | None = all_catalog.get(catalog_id) if catalog_id else None

    if database:
        existing_accessories = cfg.get_accessories()
        if database not in (existing_accessories or {}):
            raise BayError.config(
                f"Accessory '{database}' not found in services.yml",
                hint=f"Available accessories: {', '.join((existing_accessories or {}).keys()) or 'none'}",
            )

    # ── Resolve service name ─────────────────────────────────────────
    svc_name: str
    if name:
        svc_name = name
    elif catalog_entry:
        svc_name = catalog_entry.id
    elif catalog_id and not catalog_entry:
        # catalog_id given but not a catalog match — use it as the name if --name not given
        raise BayError.config(
            f"'{catalog_id}' is not in the catalog and --name was not provided",
            hint="Provide --name for custom services, or use a valid catalog ID",
        )
    else:
        raise BayError.config(
            "Service name is required",
            hint="Provide a CATALOG_ID or use --name",
        )

    # ── Idempotency check ────────────────────────────────────────────
    if cfg.get_service(svc_name) is not None:
        console.success(f"Service '{svc_name}' already exists")
        if console.is_json_mode():
            console.emit_result(
                {
                    "added": [],
                    "deps_added": [],
                    "secrets_required": [],
                    "post_add_instructions": [],
                    "dry_run": dry_run,
                },
                command="service.add",
            )
        return

    # ── Build spec block ─────────────────────────────────────────────
    if catalog_entry:
        spec_block = copy.deepcopy(catalog_entry.spec_block)
        category = catalog_entry.category
        # Apply flag overrides on top of catalog spec
        if image:
            spec_block["image"] = image
            spec_block.pop("build", None)
        elif build_repo:
            spec_block.pop("image", None)
            build_section: dict[str, Any] = {"repo": build_repo}
            if build_branch:
                build_section["branch"] = build_branch
            if build_dockerfile:
                build_section["dockerfile"] = build_dockerfile
            if build_context:
                build_section["context"] = build_context
            if build_token:
                build_section["token"] = "{{ secrets." + build_token + " }}"
            spec_block["build"] = build_section
        if port is not None:
            spec_block["port"] = port
    else:
        # Custom service — requires at least --image or --build-repo, and --port
        if not image and not build_repo:
            raise BayError.config(
                "Custom services require --image or --build-repo",
                hint="Provide --image for a pre-built image, or --build-repo for build-from-source",
            )
        if port is None:
            raise BayError.config(
                "Custom services require --port",
                hint="Specify the container port with --port",
            )
        spec_block = {}
        category = "service"
        if image:
            spec_block["image"] = image
        else:
            build_section = {"repo": build_repo}
            if build_branch:
                build_section["branch"] = build_branch
            if build_dockerfile:
                build_section["dockerfile"] = build_dockerfile
            if build_context:
                build_section["context"] = build_context
            if build_token:
                build_section["token"] = "{{ secrets." + build_token + " }}"
            spec_block["build"] = build_section
        spec_block["port"] = port

    # ── Database binding ─────────────────────────────────────────────
    if database:
        spec_block["database"] = {"accessory": database}

    # ── Domain construction ──────────────────────────────────────────
    is_multi = cfg.is_multi_region()
    if domain:
        svc_domain = domain
    elif catalog_entry and catalog_entry.category == "service":
        svc_domain = _build_domain(catalog_entry.domain_prefix, cfg, is_multi)
    else:
        svc_domain = None

    # ── Access mode ──────────────────────────────────────────────────
    if category == "service":
        svc_access = _resolve_access(catalog_entry, cfg, access)
    else:
        svc_access = None

    # ── Regions ──────────────────────────────────────────────────────
    regions = region if region else []

    # ── Links ─────────────────────────────────────────────────────
    links_dict: dict[str, dict[str, str]] = {}
    if link:
        for link_spec in link:
            if ":" not in link_spec:
                raise BayError.config(
                    f"Invalid link format: '{link_spec}'",
                    hint="Use --link TARGET:REGION (e.g., --link postgres:eu)",
                )
            parts = link_spec.split(":", 1)
            target_name, link_region = parts[0], parts[1]
            links_dict[target_name] = {"region": link_region}

    # ── Snapshot existing entries for collision checks ────────────────
    existing_services = cfg.get_services()
    existing_accessories = cfg.get_accessories()

    # ── Add main entry ───────────────────────────────────────────────
    _add_single_entry(
        svc_name, spec_block, category, svc_domain, svc_access, regions or None,
        cfg, existing_services, existing_accessories,
    )
    added = [svc_name]

    # ── Validate and attach links ─────────────────────────────────
    if links_dict:
        from bay_cli.links import validate_links

        # Build a temporary services dict including the newly added service
        temp_services = cfg.get_services()
        temp_accessories = cfg.get_accessories()

        # Add links to the spec block and validate
        errors = validate_links(
            {svc_name: {**spec_block, "regions": regions, "links": links_dict}},
            temp_accessories,
        )
        if errors:
            raise BayError.config(
                f"Link validation failed: {errors[0]}",
                hint="Check target service exists and is in the correct region",
            )

        # Update the service with links
        cfg.update_service(svc_name, {"links": links_dict})

    # ── Dependency auto-resolution ───────────────────────────────────
    deps_added: list[str] = []
    if catalog_entry and not no_deps:
        existing_acc_names = list(existing_accessories.keys())
        missing_deps = resolve_dependencies([catalog_entry.id], all_catalog, existing_acc_names)
        for dep_id in missing_deps:
            dep_entry = all_catalog.get(dep_id)
            if dep_entry is None:
                console.warning(f"Dependency '{dep_id}' not found in catalog, skipping")
                continue

            # Skip if already exists
            if cfg.get_service(dep_id) is not None:
                continue

            dep_spec = copy.deepcopy(dep_entry.spec_block)
            dep_category = dep_entry.category

            # Domain for dependency
            if dep_category == "service":
                dep_domain = _build_domain(dep_entry.domain_prefix, cfg, is_multi)
                dep_access = _resolve_access(dep_entry, cfg, None)
            else:
                dep_domain = None
                dep_access = None

            # Re-read services after each add for accurate collision detection
            current_services = cfg.get_services()
            current_accessories = cfg.get_accessories()
            _add_single_entry(
                dep_id, dep_spec, dep_category, dep_domain, dep_access,
                regions or None, cfg, current_services, current_accessories,
            )
            deps_added.append(dep_id)
            added.append(dep_id)

    # ── Config file copying ──────────────────────────────────────────
    consumer_root = cfg._root
    copied_files: list[str] = []
    if catalog_entry:
        copied_files = _copy_config_files(catalog_entry, consumer_root)
    for dep_id in deps_added:
        dep_entry = all_catalog.get(dep_id)
        if dep_entry:
            copied_files.extend(_copy_config_files(dep_entry, consumer_root))

    # ── Secret scaffolding info ──────────────────────────────────────
    secrets_required: list[str] = []
    post_add_instructions: list[str] = []
    if catalog_entry:
        secrets_required = list(catalog_entry.env_secret)
        post_add_instructions = list(catalog_entry.post_add_instructions)

    # ── Database password scaffolding ─────────────────────────────────
    db_password_key: str | None = None
    db_password_value: str | None = None
    if database:
        db_password_key = f"{svc_name.upper().replace('-', '_')}_POSTGRES_PASSWORD"
        db_password_value = secrets_mod.token_urlsafe(32)
        if db_password_key not in secrets_required:
            secrets_required.append(db_password_key)
        post_add_instructions.append(
            f"Set the database password:  bay vault set <env> {db_password_key} '{db_password_value}'"
        )

    # ── Dry-run or save ──────────────────────────────────────────────
    if dry_run:
        diff_text = cfg.diff()
        if not console.is_json_mode():
            if diff_text:
                console.header("Dry-run diff")
                console.console.print(diff_text)
            else:
                console.info("No changes")
    else:
        if cfg.has_changes():
            cfg.save()

    # ── Output ───────────────────────────────────────────────────────
    if console.is_json_mode():
        result: dict[str, Any] = {
            "added": added,
            "deps_added": deps_added,
            "secrets_required": secrets_required,
            "post_add_instructions": post_add_instructions,
            "links": links_dict,
            "dry_run": dry_run,
        }
        if database:
            result["database"] = {
                "accessory": database,
                "password_key": db_password_key,
                "password_value": db_password_value,
            }
        console.emit_result(result, command="service.add")
        return

    if not dry_run:
        for entry_name in added:
            console.success(f"Added '{entry_name}'")
        if database:
            console.info(f"Database binding: {database} (key: {db_password_key})")
        if copied_files:
            for cf in copied_files:
                console.info(f"Copied config file: {cf}")

    if secrets_required:
        console.console.print()
        console.warning("Secrets required:")
        for key in secrets_required:
            console.info(f"  {key}")

    if post_add_instructions:
        console.console.print()
        console.header("Next steps")
        for instruction in post_add_instructions:
            console.info(f"  {instruction}")


# ── Edit command ───────────────────────────────────────────────────


@app.command()
def edit(
    name: str = typer.Argument(help="Service name to edit"),
    access: str | None = typer.Option(
        None,
        "--access",
        "-a",
        help="Access mode: vpn or public",
    ),
    image: str | None = typer.Option(
        None,
        "--image",
        help="Docker image",
    ),
    domain: str | None = typer.Option(
        None,
        "--domain",
        "-d",
        help="Override domain",
    ),
    region: str | None = typer.Option(
        None,
        "--region",
        "-r",
        help="Set region",
    ),
    link: list[str] | None = typer.Option(
        None,
        "--link",
        "-l",
        help="Add cross-region link: TARGET:REGION (repeatable)",
    ),
    unlink: list[str] | None = typer.Option(
        None,
        "--unlink",
        help="Remove a cross-region link by target name (repeatable)",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show YAML diff without saving",
    ),
) -> None:
    """Edit an existing service's configuration in services.yml.

    Edits services.yml only — run `bin/bay deploy <env>` to apply.

    Examples:

        bin/bay service edit myapp --access vpn
        bin/bay service edit myapp --image nginx:1.27
        bin/bay service edit n8n --link postgres:eu --unlink redis
        bin/bay service edit myapp --domain app.example.com --dry-run
    """
    from rich.prompt import Confirm

    # ── At least one flag required ──────────────────────────────────
    if access is None and image is None and domain is None and region is None and link is None and unlink is None:
        raise BayError.config(
            "At least one edit flag required",
            hint="Use --access, --image, --domain, --region, --link, or --unlink",
        )

    # ── Service must exist ──────────────────────────────────────────
    cfg = _get_config()
    current = cfg.get_service(name)
    if current is None:
        raise BayError.not_found("service", name)

    # ── Build changes dict ──────────────────────────────────────────
    changes: dict[str, Any] = {}
    change_log: dict[str, dict[str, Any]] = {}

    if access is not None:
        changes["access"] = access
        change_log["access"] = {"from": current.get("access", ""), "to": access}

    if image is not None:
        if not image or " " in image:
            raise BayError.config(
                "Invalid image name",
                hint="Image must not be empty or contain spaces",
            )
        changes["image"] = image
        change_log["image"] = {"from": current.get("image", ""), "to": image}

    if domain is not None:
        new_regions = [region] if region is not None else list(current.get("regions", []))
        _check_domain_collision(domain, new_regions, cfg.get_services(), skip_name=name)
        changes["domains"] = [domain]
        change_log["domains"] = {
            "from": list(current.get("domains", [])),
            "to": [domain],
        }

    if region is not None:
        changes["regions"] = [region]
        change_log["regions"] = {
            "from": list(current.get("regions", [])),
            "to": [region],
        }

    if link is not None or unlink is not None:
        current_links = dict(current.get("links", {}))

        # Process unlinks
        if unlink:
            for target in unlink:
                current_links.pop(target, None)

        # Process new links
        if link:
            for link_spec in link:
                if ":" not in link_spec:
                    raise BayError.config(
                        f"Invalid link format: '{link_spec}'",
                        hint="Use --link TARGET:REGION",
                    )
                parts = link_spec.split(":", 1)
                target_name, link_region = parts[0], parts[1]
                current_links[target_name] = {"region": link_region}

        # Validate the updated links
        if current_links:
            from bay_cli.links import validate_links

            svc_regions = list(changes.get("regions", current.get("regions", [])))
            temp_svc = {name: {**current, "regions": svc_regions, "links": current_links}}
            errors = validate_links(temp_svc, cfg.get_accessories())
            if errors:
                raise BayError.config(
                    f"Link validation failed: {errors[0]}",
                    hint="Check target service exists and is in the correct region",
                )

        if current_links:
            changes["links"] = current_links
        else:
            # All links removed — remove the key entirely
            changes["links"] = {}

        change_log["links"] = {
            "from": dict(current.get("links", {})),
            "to": current_links,
        }

    # ── Idempotency check ───────────────────────────────────────────
    all_same = True
    for key, new_val in changes.items():
        existing_val = current.get(key)
        # Normalize lists for comparison
        if isinstance(new_val, list):
            existing_val = list(existing_val) if existing_val else []
        if existing_val != new_val:
            all_same = False
            break

    if all_same:
        console.info("No changes needed")
        if console.is_json_mode():
            console.emit_result(
                {"service": name, "changes": {}, "dry_run": dry_run},
                command="service.edit",
            )
        return

    # ── Access warning (public → vpn) ───────────────────────────────
    if (
        access is not None
        and access == "vpn"
        and current.get("access") == "public"
        and not console.is_yes_mode()
        and not console.is_json_mode()
    ):
        confirmed = Confirm.ask(
            f"[bold yellow]Warning:[/] Changing '{name}' from public to vpn will restrict access. Continue?"
        )
        if not confirmed:
            console.info("Aborted")
            return

    # ── Apply changes ───────────────────────────────────────────────
    cfg.update_service(name, changes)

    # ── Dry-run or save ─────────────────────────────────────────────
    if dry_run:
        diff_text = cfg.diff()
        if console.is_json_mode():
            console.emit_result(
                {"service": name, "changes": change_log, "dry_run": True},
                command="service.edit",
            )
        elif diff_text:
            console.header("Dry-run diff")
            console.console.print(diff_text)
        else:
            console.info("No changes")
        return

    cfg.save()

    # ── Output ──────────────────────────────────────────────────────
    if console.is_json_mode():
        console.emit_result(
            {"service": name, "changes": change_log, "dry_run": False},
            command="service.edit",
        )
        return

    console.success(f"Updated '{name}'")
    for key, log in change_log.items():
        console.info(f"  {key}: {log['from']} → {log['to']}")


# ── Remove command ─────────────────────────────────────────────────


@app.command()
def remove(
    name: str = typer.Argument(help="Service or accessory name to remove"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show YAML diff without saving",
    ),
    keep_webhook: bool = typer.Option(
        False,
        "--keep-webhook",
        help="Skip GitHub webhook deletion when removing a service.",
    ),
) -> None:
    """Remove a service or accessory from services.yml.

    Edits services.yml only — run `bin/bay deploy <env>` to apply. Docker
    volumes, env files, and backup timers are left behind (the warnings
    list them). For build services it also offers to delete the matching
    GitHub webhook (--keep-webhook to skip).

    Examples:

        bin/bay service remove myapp
        bin/bay service remove myapp --dry-run
        bin/bay service remove myapp --keep-webhook
    """
    from rich.prompt import Confirm

    cfg = _get_config()

    # ── Idempotency: service must exist ──────────────────────────
    svc_block = cfg.get_service(name)
    if svc_block is None:
        console.success(f"Service '{name}' not found, nothing to remove")
        if console.is_json_mode():
            console.emit_result(
                {"removed": False, "name": name, "warnings": [], "dry_run": dry_run},
                command="service.remove",
            )
        return

    # Capture build info before removal for webhook cleanup
    build = svc_block.get("build") if isinstance(svc_block, dict) else None
    build_repo = build.get("repo", "") if isinstance(build, dict) else ""

    # ── Dependency check ─────────────────────────────────────────
    dependents: list[str] = []
    for svc_name, svc_blk in cfg.get_services().items():
        if svc_name == name:
            continue
        if name in list(svc_blk.get("depends_on", [])):
            dependents.append(svc_name)
    for acc_name, acc_block in cfg.get_accessories().items():
        if acc_name == name:
            continue
        if name in list(acc_block.get("depends_on", [])):
            dependents.append(acc_name)

    if dependents:
        raise BayError.dependency(name, dependents)

    # ── Residual warnings ────────────────────────────────────────
    warnings: list[str] = [
        f"Docker volumes for '{name}' remain on the server",
        "Env files remain in the consumer repo",
        "Backup timers remain until next deploy",
        "Run `bin/bay deploy` to apply changes, then manually clean up if needed",
    ]

    # ── Determine whether webhook deletion applies ────────────────
    _webhook_applicable = (
        not keep_webhook
        and bool(build_repo)
        and "github.com" in build_repo
    )

    # ── Confirmation ─────────────────────────────────────────────
    if not console.is_yes_mode() and not console.is_json_mode() and not dry_run:
        for w in warnings:
            console.warning(w)
        if not Confirm.ask(f"Remove '{name}'?", default=False):
            return

    # ── Dry-run ──────────────────────────────────────────────────
    if dry_run:
        cfg.remove_service(name)
        diff_text = cfg.diff()
        if console.is_json_mode():
            console.emit_result(
                {
                    "removed": False,
                    "name": name,
                    "warnings": warnings,
                    "dry_run": True,
                    "webhook_would_delete": _webhook_applicable,
                },
                command="service.remove",
            )
        else:
            for w in warnings:
                console.warning(w)
            if _webhook_applicable:
                console.console.print(
                    f"[dim]Dry-run: would attempt webhook deletion for '{name}'[/]"
                )
            if diff_text:
                console.header("Dry-run diff")
                console.console.print(diff_text)
            else:
                console.info("No changes")
        return

    # ── Save ─────────────────────────────────────────────────────
    cfg.remove_service(name)
    cfg.save()

    # ── GitHub webhook deletion (opt-out via --keep-webhook) ─────
    if _webhook_applicable:
        import requests as _requests

        from bay_cli.github_webhook import (
            list_repo_hooks,
            parse_repo_owner_name,
            resolve_webhook_domain,
        )

        parsed_repo = parse_repo_owner_name(build_repo)
        webhook_domain = resolve_webhook_domain(cfg._root)
        token = _read_vault_token(cfg._root, "production", "github_admin_token")

        if not webhook_domain or not token or not parsed_repo:
            console.warning(
                "Cannot delete GitHub webhook: webhook_domain not configured or "
                "github_admin_token missing from vault. Remove it manually or "
                "re-run with --keep-webhook to suppress this warning."
            )
        else:
            owner, repo_name = parsed_repo
            expected_url_prefix = f"https://{webhook_domain}/webhook/{name}"

            try:
                hooks = list_repo_hooks(owner, repo_name, token)
                norm_prefix = expected_url_prefix.rstrip("/").lower()
                matching = [
                    h for h in hooks
                    if str(h.get("config", {}).get("url", "")).rstrip("/").lower().startswith(norm_prefix)
                ]

                if not matching:
                    console.warning(
                        f"No matching webhook found for '{name}' on {owner}/{repo_name} "
                        f"— may have already been removed."
                    )
                else:
                    hook = matching[0]
                    hook_id = hook.get("id")
                    hook_url_val = hook.get("config", {}).get("url", "")

                    do_delete = console.is_yes_mode()
                    if not do_delete and not console.is_json_mode():
                        console.console.print(
                            f"\n[bold yellow]Warning:[/] This will permanently delete the "
                            f"GitHub webhook for '{name}' on {owner}/{repo_name}.\n"
                            f"Webhook URL: {hook_url_val}\n"
                        )
                        do_delete = Confirm.ask("Delete GitHub webhook?", default=False)
                        if not do_delete:
                            console.warning(
                                f"Skipped webhook deletion. Run "
                                f"'bin/bay service prune-webhooks {owner}/{repo_name}' "
                                f"later to clean up."
                            )

                    if do_delete:
                        try:
                            resp = _requests.delete(
                                f"https://api.github.com/repos/{owner}/{repo_name}/hooks/{hook_id}",
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Accept": "application/vnd.github+json",
                                },
                                timeout=10,
                            )
                            resp.raise_for_status()
                            console.success(f"Deleted GitHub webhook for '{name}'")
                        except _requests.RequestException as exc:
                            console.warning(f"Failed to delete GitHub webhook for '{name}': {exc}")

            except _requests.RequestException as exc:
                console.warning(f"GitHub API error while cleaning up webhook: {exc}")

    # ── Output ───────────────────────────────────────────────────
    if console.is_json_mode():
        console.emit_result(
            {"removed": True, "name": name, "warnings": warnings, "dry_run": False},
            command="service.remove",
        )
        return

    console.success(f"Removed '{name}'")
    for w in warnings:
        console.warning(w)


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
                        "ansible-vault", "decrypt",
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
    repo: str = typer.Argument(help="GitHub repo in owner/name format (e.g., MyOrg/myapp)"),
    env: str = typer.Option("production", "--env", "-e", help="Target environment for vault and webhook_domain lookup."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List orphan hooks without deleting them."),
) -> None:
    """List and optionally delete orphan GitHub webhooks for a repository.

    Orphans are hooks under this consumer's webhook domain that no current
    service claims. Requires github_admin_token in the vault.

    Examples:

        bin/bay service prune-webhooks MyOrg/myapp --dry-run
        bin/bay service prune-webhooks MyOrg/myapp
    """
    from rich.prompt import Confirm
    from rich.table import Table as RichTable

    import requests as _requests

    from bay_cli.github_webhook import (
        find_orphan_hooks,
        list_repo_hooks,
        resolve_webhook_domain,
    )

    cfg = _get_config()

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
            hint=f"Add a read-only GitHub PAT with 'repo' scope via 'bin/bay vault edit {env}'",
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
