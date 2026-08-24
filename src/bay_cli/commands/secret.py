"""Secret generation and password hashing commands."""

import base64
import secrets
from typing import Optional

import typer

from bay_cli import runner
from bay_cli.errors import BayError
from bay_cli.utils.ephemeral import show_ephemeral


def secret(
    hash: Optional[str] = typer.Option(
        None,
        "--hash",
        help="Hash a password: argon2 or bcrypt.",  # legacy-argo: argon2 lib name, substring match only, not a rename
    ),
) -> None:
    """Generate random secrets or hash passwords.

    Prints passwords, hex tokens, and base64 keys on an ephemeral screen —
    nothing lands in scrollback. With --hash, prompts for a password and
    prints an argon2id or bcrypt hash (e.g. for Traefik basicauth).  # legacy-argo: argon2 lib name, substring match only, not a rename

    Examples:

        bin/bay secret
        bin/bay secret --hash argon2  # legacy-argo: argon2 lib name, substring match only, not a rename
        bin/bay secret --hash bcrypt
    """
    if hash:
        _hash_password(hash)
    else:
        _generate_secrets()


def _generate_secrets() -> None:
    pw32 = secrets.token_urlsafe(48)[:32]
    pw64 = secrets.token_urlsafe(96)[:64]
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
        "\n  Paste into secrets with: [bold]bin/bay vault edit <env>[/bold]"
        "\n"
    )
    show_ephemeral(content)


def _hash_password(algorithm: str) -> None:
    if algorithm not in ("argon2", "bcrypt"):  # legacy-argo: argon2 lib name, substring match only, not a rename
        raise BayError(f"Unknown hash type '{algorithm}'. Valid options: argon2, bcrypt")  # legacy-argo: argon2 lib name, substring match only, not a rename

    import getpass

    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        raise BayError("Passwords do not match")

    if algorithm == "argon2":  # legacy-argo: argon2 lib name, substring match only, not a rename
        _hash_argon2(pw1)  # legacy-argo: argon2 lib name, substring match only, not a rename
    else:
        _hash_bcrypt(pw1)


def _hash_argon2(password: str) -> None:  # legacy-argo: argon2 lib name, substring match only, not a rename
    try:
        from argon2 import PasswordHasher, Type  # legacy-argo: argon2 lib name, substring match only, not a rename

        ph = PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=Type.ID
        )
        result = ph.hash(password)
    except ImportError:
        # Fall back to uv run --with
        r = runner.run(
            [
                "uv", "run", "--with", "argon2-cffi", "python3", "-c",  # legacy-argo: argon2 lib name, substring match only, not a rename
                "from argon2 import PasswordHasher, Type; "  # legacy-argo: argon2 lib name, substring match only, not a rename
                "ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=Type.ID); "
                f"print(ph.hash({password!r}))",
            ],
            message="Hashing password...",
        )
        result = r.stdout.strip()

    content = (
        "\n  [bold]Argon2id hash:[/bold]"  # legacy-argo: argon2 lib name, substring match only, not a rename
        "\n"
        f"\n  {result}"
        "\n"
        "\n  Paste into secrets with: [bold]bin/bay vault edit <env>[/bold]"
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
        "\n  Paste into secrets with: [bold]bin/bay vault edit <env>[/bold]"
        "\n"
    )
    show_ephemeral(content, clipboard=result)
