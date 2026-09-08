"""`bay healthcheck <env>` — post-deploy reachability audit.

Hits every public service's domains via HTTPS and reports 2xx/3xx as
pass, 4xx/5xx/timeout/TLS-error as fail. Runs automatically after every
successful `bin/bay deploy` unless `--skip-healthcheck` is passed.

See src/bay_cli/healthcheck.py for the probe logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer

from bay_cli import console, paths
from bay_cli.config import StackConfig
from bay_cli.healthcheck import (
    CheckResult,
    render_results,
    run_healthcheck,
    summarize,
)


def _emit_json(env: str, results: list[CheckResult]) -> None:
    summary = summarize(results)
    payload = {
        "env": env,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "results": [r.to_dict() for r in results],
        "summary": summary,
    }
    console.emit_result(payload, command="healthcheck")


def _emit_rich(env: str, consumer: str, results: list[CheckResult]) -> None:
    summary = summarize(results)
    header = f"Post-deploy healthcheck ({consumer} [{env}] — {summary['total']} checks)"
    console.header(header)
    render_results(
        results,
        headline="Not all services are healthy.",
    )


def healthcheck(
    env: str = typer.Argument(..., help="Target environment (e.g. 'production', 'testing')."),
    include_vpn: bool = typer.Option(
        False,
        "--include-vpn",
        help=(
            "Include fully VPN-only services (access: vpn with no "
            "public_routes). By default they are skipped because they "
            "require tailnet context to reach."
        ),
    ),
    service_filter: str | None = typer.Option(
        None,
        "--service",
        help="Check only the named service (useful for partial-deploy debugging).",
    ),
) -> None:
    """Hit every public service's domains and report reachability.

    Exit code is non-zero if any service returns 4xx/5xx, times out,
    or has a TLS error. Skipped services (VPN-only) don't count against
    the exit code.

    Note: this catches "container down entirely", "Traefik misconfigured",
    "TLS cert expired", and "container restart loop". The nginx-up-but-
    Node-dead supervisor pattern (a static-served root path masking a dead
    backend) is caught when the service declares `healthcheck_path:` in
    services.yml — the probe hits that path instead of `/`, exercising the
    dynamic backend. Without `healthcheck_path:`, the root is probed and
    the supervisor pattern can still slip through.

    Examples:

        bin/bay healthcheck production
        bin/bay healthcheck production --service myapp
        bin/bay healthcheck production --include-vpn
    """
    bay_dir = paths.find_bay_dir()
    root = paths.consumer_root(bay_dir)
    # Consumer name for the header — use the root dir's folder name.
    consumer = Path(root).name

    cfg = StackConfig(root)
    services = cfg.get_services()
    if not services:
        console.warning("No services defined in services.yml — nothing to check.")
        raise typer.Exit(code=0)

    results = run_healthcheck(
        services,
        include_vpn=include_vpn,
        only=service_filter,
        region_vars=cfg.resolve_region_vars(env),
    )

    if console.is_json_mode():
        _emit_json(env, results)
    else:
        _emit_rich(env, consumer, results)

    summary = summarize(results)
    if summary["failed"]:
        raise typer.Exit(code=1)
