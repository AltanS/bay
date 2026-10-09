"""Bay CLI — main application entry point."""

from pathlib import Path

import typer

from bay_cli import console, fleet_line
from bay_cli.commands import compile_cmd, fleet_cmd, import_cmd, project_cmd, remove_cmd, route_cmd, self_cmd, skill_cmd, toml_cmd
from bay_cli.commands import alerts, backup, build, doctor, framework, gateway, healthcheck as healthcheck_cmd, ops, prune as prune_cmd, region, secret, server, service, test, validate, vault
from bay_cli.context import GlobalOptions, package_root
from bay_cli.errors import BayError

app = typer.Typer(
    name="bay",
    help="Bay — Infrastructure Management",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# Allow extra args to pass through to ansible-playbook
_allow_extra = {"allow_extra_args": True, "allow_interspersed_args": False}


def fleet_callback(ctx: typer.Context, value: Path | None) -> Path | None:
    """Store the global options where every command can reach them.

    Eager, and declared before ``--version``/``--skill``, so those callbacks
    already see ``--fleet`` when it comes first on the command line.
    """
    ctx.obj = GlobalOptions(fleet=value)
    fleet_line.remember(ctx.obj)
    return value


def version_callback(ctx: typer.Context, value: bool) -> None:
    if value:
        import subprocess

        bay_dir = package_root()

        # Prefer git tag (source of truth)
        result = subprocess.run(
            ["git", "-C", str(bay_dir), "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            typer.echo(f"bay {result.stdout.strip()}")
            raise typer.Exit()

        result = subprocess.run(
            ["git", "-C", str(bay_dir), "describe", "--tags"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            typer.echo(f"bay {result.stdout.strip()}")
            raise typer.Exit()
        typer.echo("bay (version unknown)")
        raise typer.Exit()


def skill_callback(ctx: typer.Context, value: bool) -> None:
    """Print SKILL.md — the framework's single-file orientation document.

    Written straight to stdout, unformatted: the reader is usually an agent
    piping it somewhere, not a terminal.
    """
    if not value:
        return
    # The package lives at <framework>/src/bay_cli, so the framework root is
    # two levels up, resolved from the import and not from the working directory.
    skill = package_root() / "SKILL.md"
    if not skill.is_file():
        raise BayError(
            "SKILL.md not found in the framework checkout",
            hint="Update to a version that ships it: bay self update.",
        )
    typer.echo(skill.read_text(), nl=False)
    raise typer.Exit()


@app.callback()
def main(
    fleet: Path | None = typer.Option(
        None,
        "--fleet",
        callback=fleet_callback,
        is_eager=True,
        help="Fleet directory to operate on. Overrides BAY_FLEET.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    skill: bool = typer.Option(
        False,
        "--skill",
        callback=skill_callback,
        is_eager=True,
        help="Print SKILL.md — a compiled overview of the CLI and docs.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output structured JSON instead of human-readable text.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip all interactive confirmations.",
    ),
) -> None:
    """Bay — Infrastructure Management"""
    console.set_json_mode(json_output)
    console.set_yes_mode(yes)


# Daily verbs (top-level): WANTED / PINNED / RUNNING, see docs/plan.md
app.command(rich_help_panel="Daily")(project_cmd.init)
app.command(rich_help_panel="Daily")(project_cmd.plan)
app.command(rich_help_panel="Daily")(project_cmd.approve)
app.command(rich_help_panel="Daily")(project_cmd.up)
app.command(rich_help_panel="Daily")(project_cmd.show)
app.command(rich_help_panel="Daily")(project_cmd.rollback)
app.command(rich_help_panel="Daily")(project_cmd.adopt)
app.command(rich_help_panel="Daily")(remove_cmd.remove)
app.add_typer(route_cmd.app, name="route", rich_help_panel="Daily")

# Framework commands (top-level)
app.command(rich_help_panel="Framework")(framework.status)

# Install and fleets on this machine
app.add_typer(self_cmd.app, name="self", rich_help_panel="Framework")
app.add_typer(fleet_cmd.app, name="fleet", rich_help_panel="Framework")
app.add_typer(skill_cmd.app, name="skill", rich_help_panel="Framework")

# Operational commands (top-level, allow extra args for ansible passthrough)
app.command(rich_help_panel="Operations", context_settings=_allow_extra)(ops.deploy)
app.command(rich_help_panel="Operations", context_settings=_allow_extra)(ops.provision)
app.command(rich_help_panel="Operations", context_settings=_allow_extra)(ops.restore)

# Runtime commands (top-level)
app.command(rich_help_panel="Operations")(ops.logs)
app.command(rich_help_panel="Operations")(ops.restart)
app.command(rich_help_panel="Operations")(healthcheck_cmd.healthcheck)
app.command("admin-shell", rich_help_panel="Operations")(ops.admin_shell)
app.command(rich_help_panel="Operations")(prune_cmd.prune)

# Build (sub-app)
app.add_typer(build.app, name="build", rich_help_panel="Operations")

# Vault (sub-app)
app.add_typer(vault.app, name="vault", rich_help_panel="Vault")

# Backup (sub-app)
app.add_typer(backup.app, name="backup", rich_help_panel="Backup")

# Gateway (sub-app)
app.add_typer(gateway.app, name="gateway", rich_help_panel="Operations")

# Service (sub-app)
app.add_typer(service.app, name="service", rich_help_panel="Stack Manager")

# Server (sub-app)
app.add_typer(server.app, name="server", rich_help_panel="Stack Manager")

# Region (sub-app)
app.add_typer(region.app, name="region", rich_help_panel="Operations")

app.add_typer(alerts.app, name="alerts", rich_help_panel="Operations")

# Validate (top-level)
app.command(rich_help_panel="Utilities")(validate.validate)

# bay.toml (sub-app)
app.add_typer(toml_cmd.app, name="toml", rich_help_panel="Utilities")

# Compile (top-level)
app.command("compile", rich_help_panel="Utilities")(compile_cmd.compile_fleet)

# Import (top-level)
app.command("import", rich_help_panel="Utilities")(import_cmd.import_fleet)

# Doctor (top-level)
app.command(rich_help_panel="Utilities")(doctor.doctor)

# Secret (sub-app: `bay secret` still generates; `bay secret missing <env>`)
app.add_typer(secret.app, name="secret", rich_help_panel="Utilities")

# Test (top-level)
app.command(rich_help_panel="Utilities")(test.test)

# Last: every verb that changes a fleet or a box prints `fleet: <name> (<path>)`
# on stderr before it starts. Register new commands above this line.
fleet_line.install(app)


def _main() -> None:
    """Entry point that catches BayError for clean output."""
    import sys

    try:
        app()
    except BayError as e:
        if console.is_json_mode():
            console.emit_error([e.to_dict()])
        else:
            console.error(str(e))
            if e.hint:
                console.info(f"Hint: {e.hint}")
        sys.exit(e.exit_code)
