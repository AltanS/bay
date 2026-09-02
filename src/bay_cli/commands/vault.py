"""Vault management commands."""

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
import yaml

from bay_cli import ansible, console, paths, runner
from bay_cli.errors import BayError

app = typer.Typer(help="Manage encrypted secrets (ansible-vault).")


def _vault_file(env: str, file: Optional[str]) -> str:
    if file:
        return file
    return f"group_vars/{env}/secrets.yml"


@app.command()
def edit(
    env: str = typer.Argument(..., help="Target environment."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Vault file path."),
) -> None:
    """Edit encrypted secrets in $EDITOR (decrypt, edit, re-encrypt).

    Secrets live under the `secrets:` dict. Key casing is a convention the
    framework relies on: UPPERCASE keys are container env vars (referenced
    from services.yml `env.secret:`), lowercase keys are consumed by
    framework roles (headscale_api_key, backup_restic_password, ...).
    Referencing a role secret by an UPPERCASE name silently resolves
    undefined.

    Examples:

        bin/bay vault edit production
        bin/bay vault edit eu --file group_vars/eu/secrets.yml
    """
    bay_dir = paths.find_bay_dir()
    ansible.vault_cmd("edit", _vault_file(env, file), bay_dir=bay_dir)


@app.command()
def view(
    env: str = typer.Argument(..., help="Target environment."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Vault file path."),
) -> None:
    """View encrypted secrets (read-only, no temp files).

    Examples:

        bin/bay vault view production
    """
    bay_dir = paths.find_bay_dir()
    ansible.vault_cmd("view", _vault_file(env, file), bay_dir=bay_dir)


@app.command()
def encrypt(
    env: str = typer.Argument(..., help="Target environment."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Vault file path."),
) -> None:
    """Encrypt a plaintext secrets file in place.

    Examples:

        bin/bay vault encrypt production
    """
    bay_dir = paths.find_bay_dir()
    ansible.vault_cmd("encrypt", _vault_file(env, file), bay_dir=bay_dir)


@app.command()
def decrypt(
    env: str = typer.Argument(..., help="Target environment."),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Vault file path."),
) -> None:
    """Decrypt a secrets file in place — leaves PLAINTEXT on disk.

    Re-encrypt with `bin/bay vault encrypt` before committing. For a
    read-only look use `bin/bay vault view` instead.

    Examples:

        bin/bay vault decrypt production
    """
    bay_dir = paths.find_bay_dir()
    ansible.vault_cmd("decrypt", _vault_file(env, file), bay_dir=bay_dir)


def _uv_run_cmd(bay_dir: Path) -> list[str]:
    return ["uv", "run", "--project", str(bay_dir)]


def _normalise_stdin_value(raw: str) -> str:
    """Normalise a value read from stdin.

    A single-line value (exactly one trailing "\\n" and no other newline)
    has that trailing newline stripped, so `echo` and a heredoc both work.
    A multi-line value, such as a PEM key, is returned verbatim, including
    its terminating newline, because that newline is significant.
    """
    if raw.count("\n") == 1 and raw.endswith("\n"):
        value = raw[:-1]
        if value.endswith("\r"):
            value = value[:-1]
        return value
    return raw


@app.command("set")
def set_key(
    env: str = typer.Argument(..., help="Target environment."),
    key: str = typer.Argument(..., help="Secret key name."),
    value: Optional[str] = typer.Argument(
        None,
        help="Secret value. OMIT IT and pipe the value on stdin instead.",
    ),
) -> None:
    """Set one secret key non-interactively (decrypt, modify, re-encrypt).

    Writes under the `secrets:` dict, creating it if missing. Mind the
    casing convention (see `bin/bay vault edit --help`).

    Prefer stdin. A value passed as an argument lands in the operator's shell
    history and is readable in /proc/<pid>/cmdline by any local user for as
    long as the command runs. Omitting it reads the value from stdin. A
    single-line value has its one trailing newline stripped, so `echo` and a
    heredoc both work. A multi-line value, such as a PEM key, is stored
    verbatim, including its terminating newline, because that newline is
    significant. The positional form still works for one transition
    release.

    Examples:

        printf %s 's3cret' | bin/bay vault set production POSTGRES_PASSWORD
        bin/bay vault set production GITHUB_TOKEN < token.txt
        bin/bay vault set production POSTGRES_PASSWORD 's3cret'   # deprecated
    """
    if value is None:
        if sys.stdin.isatty():
            raise BayError.config(
                "No value given and stdin is a terminal.",
                hint=(
                    f"Pipe the secret in: printf %s '<value>' | "
                    f"bay vault set {env} {key}"
                ),
            )
        value = _normalise_stdin_value(sys.stdin.read())
        if not value:
            raise BayError.config(
                "Empty value on stdin.",
                hint="Pipe the secret value in, or pass it as an argument.",
            )
    else:
        console.warning(
            "Passing the secret as an argument puts it in your shell history "
            "and in /proc/<pid>/cmdline. Pipe it on stdin instead."
        )

    bay_dir = paths.find_bay_dir()
    vault_file = Path(_vault_file(env, None))

    if not vault_file.exists():
        raise BayError.config(
            f"Vault file not found: {vault_file}",
            hint=f"Create it with: bay vault edit {env}",
        )

    # Create temp file for decrypted contents
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".yml")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    try:
        # Decrypt vault to temp file
        runner.run(
            [*_uv_run_cmd(bay_dir), "ansible-vault", "decrypt", str(vault_file), "--output", str(tmp_path)],
            message=f"Decrypting vault for {env}...",
        )

        # Read, modify, write
        data = yaml.safe_load(tmp_path.read_text()) or {}
        if "secrets" not in data:
            data["secrets"] = {}
        data["secrets"][key] = value
        tmp_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

        # Re-encrypt back to original file
        runner.run(
            [*_uv_run_cmd(bay_dir), "ansible-vault", "encrypt", str(tmp_path), "--output", str(vault_file)],
            message=f"Encrypting vault for {env}...",
        )

        console.success(f"Set secret '{key}' in {vault_file}")
    finally:
        tmp_path.unlink(missing_ok=True)
