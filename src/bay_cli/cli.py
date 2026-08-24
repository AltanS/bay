"""Bay CLI — main application entry point."""

from pathlib import Path

import typer

from bay_cli import console
from bay_cli.commands import alerts, backup, build, doctor, framework, gateway, healthcheck as healthcheck_cmd, ops, prune as prune_cmd, region, secret, server, service, test, validate, vault, webhook
from bay_cli.errors import BayError

app = typer.Typer(
    name="bay",
    help="Bay — Infrastructure Management",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

# Allow extra args to pass through to ansible-playbook
_allow_extra = {"allow_extra_args": True, "allow_interspersed_args": False}


def version_callback(value: bool) -> None:
    if value:
        import subprocess

        try:
            from bay_cli.paths import find_bay_dir

            bay_dir = find_bay_dir()

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
        except BayError:
            pass
        typer.echo("bay (version unknown)")
        raise typer.Exit()


def skill_callback(value: bool) -> None:
    """Print SKILL.md — the framework's single-file orientation document.

    Written straight to stdout, unformatted: the consumer is usually an agent
    piping it somewhere, not a terminal.
    """
    if not value:
        return
    from bay_cli import paths

    # The package lives at <framework>/src/bay_cli, so the framework root is
    # two levels up — resolved from the import, not the cwd, so this works in
    # a consumer (.bay/), in dev-link mode, and from the framework repo alike.
    skill = Path(__file__).resolve().parents[2] / "SKILL.md"
    if not skill.is_file():
        try:
            skill = paths.find_bay_dir() / "SKILL.md"
        except BayError:
            pass
    if not skill.is_file():
        raise BayError(
            "SKILL.md not found in the framework checkout",
            hint="Update to a framework version that ships it (bin/bay update).",
        )
    typer.echo(skill.read_text(), nl=False)
    raise typer.Exit()


@app.callback()
def main(
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


# Framework commands (top-level)
app.command(rich_help_panel="Framework")(framework.setup)
app.command(rich_help_panel="Framework")(framework.install)
app.command(rich_help_panel="Framework")(framework.update)
app.command(rich_help_panel="Framework")(framework.status)
app.command(rich_help_panel="Framework")(framework.guide)
app.command(rich_help_panel="Framework")(framework.dev_link)
app.command(rich_help_panel="Framework")(framework.dev_unlink)

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

# Webhook (top-level)
app.command(rich_help_panel="Operations")(webhook.webhook)

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

# Doctor (top-level)
app.command(rich_help_panel="Utilities")(doctor.doctor)

# Secret (top-level)
app.command(rich_help_panel="Utilities")(secret.secret)

# Test (top-level)
app.command(rich_help_panel="Utilities")(test.test)


def _main() -> None:
    """Entry point that catches BayError for clean output."""
    import sys

    # legacy-argo: the `argo` console-script alias (pyproject.toml) forwards
    # here unchanged; warn once so un-migrated consumer wrappers fail soft,
    # not weird. Removed in a future major release, along with the alias
    # itself — not v1.1 as an earlier draft of this comment said.
    invoked_as = Path(sys.argv[0]).name if sys.argv else ""
    if invoked_as == "argo":  # legacy-argo: alias detection, remove in a future major release
        print(
            "warning: 'argo' has been renamed to bay; this alias will be "  # legacy-argo: warning text, remove in a future major release
            "removed in a future release — invoke 'bay' instead",
            file=sys.stderr,
        )

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
