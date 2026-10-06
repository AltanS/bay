"""Secret generation, password hashing, and the missing-secret check."""

import base64
import json
import secrets
from typing import Optional

import typer

from bay_cli import console, runner
from bay_cli.context import context_from
from bay_cli.errors import BayError
from bay_cli.utils.ephemeral import show_ephemeral
from bay_cli.utils.secret_gen import generate_password

# A group so `bay secret missing <env>` can live next to the generator.
# `invoke_without_command`: plain `bay secret` and `bay secret --hash ...`
# behave exactly as they did when this was a single command.
app = typer.Typer(invoke_without_command=True, no_args_is_help=False)


@app.callback()
def secret(
    ctx: typer.Context,
    hash: Optional[str] = typer.Option(
        None,
        "--hash",
        help="Hash a password: argon2 or bcrypt.",  # kept-argo: argon2 lib name, substring match only, not a rename
    ),
) -> None:
    """Generate random secrets or hash passwords.

    Prints passwords, hex tokens, and base64 keys on an ephemeral screen —
    nothing lands in scrollback. With --hash, prompts for a password and
    prints an argon2id or bcrypt hash (e.g. for Traefik basicauth).  # kept-argo: argon2 lib name, substring match only, not a rename

    `bay secret missing <env>` lists the secret names the services need
    that the env's vault does not hold (names only, never values).

    Examples:

        bay secret
        bay secret --hash argon2  # kept-argo: argon2 lib name, substring match only, not a rename
        bay secret --hash bcrypt
        bay secret missing production
    """
    if ctx.invoked_subcommand is not None:
        return
    if hash:
        _hash_password(hash)
    else:
        _generate_secrets()


@app.command("missing")
def missing(
    ctx: typer.Context,
    env: str = typer.Argument(..., help="Target environment (e.g., production)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List secret NAMES the services need that the env's vault lacks.

    Compares names only. The vault is decrypted in memory and only its key
    names are kept; no value is printed, logged or passed on a command line.
    Exits 1 when a name is missing. With no vault password the check cannot
    run: that is a warning (exit 0), not a failure.

    Examples:

        bay secret missing production
        bay secret missing production --json
    """
    from bay_cli import secrets_check

    cx = context_from(ctx)
    try:
        found = secrets_check.missing_secrets(cx, env)
    except secrets_check.SecretsUncheckable as exc:
        reason = f"cannot check: {exc}"
        if as_json:
            print(json.dumps({"env": env, "checked": False, "warning": reason, "missing": []}, indent=2))
        else:
            console.warning(reason)
        return

    if as_json:
        print(json.dumps(
            {"env": env, "checked": True, "warning": None,
             "missing": [m.to_dict() for m in found]},
            indent=2,
        ))
    elif found:
        for item in found:
            console.error(f"{item.name}  (used by {', '.join(item.used_by)})")
        console.info(f"{len(found)} secret name(s) missing in {env}. Add them with: bay vault edit {env}")
    else:
        console.success(f"No secret name is missing in {env}")
    if found:
        raise typer.Exit(code=1)


def _generate_secrets() -> None:
    pw32 = generate_password(32)
    pw64 = generate_password(64)
    hex_token = secrets.token_hex(32)
    b64_32 = base64.b64encode(secrets.token_bytes(32)).decode()
    b64_64 = base64.b64encode(secrets.token_bytes(64)).decode()

    content = (
        "\n[bold]Generated secrets[/bold] (copy what you need)"
        "\n"
        "\n  [dim]Password (32 chars):[/dim]"
        f"\n    {pw32}"
        "\n"
        "\n  [dim]Password (64 chars):[/dim]"
        f"\n    {pw64}"
        "\n"
        "\n  [dim]Hex token (32 bytes):[/dim]"
        f"\n    {hex_token}"
        "\n"
        "\n  [dim]Base64 key (32 bytes):[/dim]"
        f"\n    {b64_32}"
        "\n"
        "\n  [dim]Base64 key (64 bytes):[/dim]"
        f"\n    {b64_64}"
        "\n"
        "\n  Paste into secrets with: [bold]bay vault edit <env>[/bold]"
        "\n"
    )
    show_ephemeral(content)


def _hash_password(algorithm: str) -> None:
    if algorithm not in ("argon2", "bcrypt"):  # kept-argo: argon2 lib name, substring match only, not a rename
        raise BayError(f"Unknown hash type '{algorithm}'. Valid options: argon2, bcrypt")  # kept-argo: argon2 lib name, substring match only, not a rename

    import getpass

    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        raise BayError("Passwords do not match")

    if algorithm == "argon2":  # kept-argo: argon2 lib name, substring match only, not a rename
        _hash_argon2(pw1)  # kept-argo: argon2 lib name, substring match only, not a rename
    else:
        _hash_bcrypt(pw1)


def _hash_argon2(password: str) -> None:  # kept-argo: argon2 lib name, substring match only, not a rename
    try:
        from argon2 import PasswordHasher, Type  # kept-argo: argon2 lib name, substring match only, not a rename

        ph = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=Type.ID
        )
        result = ph.hash(password)
    except ImportError:
        # Fall back to uv run --with
        r = runner.run(
            [
                "uv", "run", "--with", "argon2-cffi", "python3", "-c",  # kept-argo: argon2 lib name, substring match only, not a rename
                "from argon2 import PasswordHasher, Type; "  # kept-argo: argon2 lib name, substring match only, not a rename
                "ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=Type.ID); "
                f"print(ph.hash({password!r}))",
            ],
            message="Hashing password...",
        )
        result = r.stdout.strip()

    content = (
        "\n  [bold]Argon2id hash:[/bold]"  # kept-argo: argon2 lib name, substring match only, not a rename
        "\n"
        f"\n  {result}"
        "\n"
        "\n  Paste into secrets with: [bold]bay vault edit <env>[/bold]"
        "\n"
    )
    show_ephemeral(content, clipboard=result)


def _hash_bcrypt(password: str) -> None:
    try:
        import bcrypt

        result = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    except ImportError:
        r = runner.run(
            [
                "uv", "run", "--with", "bcrypt", "python3", "-c",
                "import bcrypt; "
                f"print(bcrypt.hashpw({password!r}.encode(), bcrypt.gensalt(rounds=12)).decode())",
            ],
            message="Hashing password...",
        )
        result = r.stdout.strip()

    content = (
        "\n  [bold]bcrypt hash:[/bold]"
        "\n"
        f"\n  {result}"
        "\n"
        "\n  Paste into secrets with: [bold]bay vault edit <env>[/bold]"
        "\n"
    )
    show_ephemeral(content, clipboard=result)
