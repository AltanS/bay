"""Missing-secret check: which secret NAMES the fleet needs but does not hold.

The check compares names only. The vault is decrypted in memory, its key
names are kept, and the decrypted data is dropped right away. No value is
ever returned, printed, logged or put on a command line: a caller gets
:class:`MissingSecret` objects, which hold a name and the services that use
it, nothing else.

What a service needs (``services.yml``, services and accessories alike):

* ``env.secret`` as a list: ``<NAME upper, - to _>_<KEY>`` (``env.j2`` adds
  the prefix).
* ``env.secret`` as a dict: each value is the vault key, used as is.
* ``database:``: ``<user (default: the service name) upper, - to _>_POSTGRES_PASSWORD``.
* ``middleware.basic_auth`` (``credentials[].password``, ``users[]``) and
  ``build`` (``secrets``, ``token``): every ``{{ secrets.KEY }}`` reference.
  A plain value, or a reference to a non-``secrets`` variable, is not a
  vault name and is not checked.

What the fleet holds: the keys of the ``secrets:`` mapping in
``group_vars/<env>/secrets.yml`` (or the file's top-level keys when it has no
``secrets:`` mapping, the same reading ``bay validate`` uses). When the env
has no secrets file, ``group_vars/all/secrets.yml`` is read instead.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bay_cli import ansible as _ansible
from bay_cli.context import Context

_SECRET_REF_RE = re.compile(
    r"""secrets\.([A-Za-z_][A-Za-z0-9_]*)|secrets\[\s*['"]([^'"]+)['"]\s*\]"""
)

NO_VAULT_PASSWORD = "no vault password"


@dataclass(frozen=True)
class MissingSecret:
    """One secret name a service needs that the vault does not hold. Never a value."""

    name: str
    used_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "used_by": list(self.used_by)}


class SecretsUncheckable(Exception):
    """The vault could not be read, so nothing can be said about missing names.

    ``str(exc)`` is a short reason (for example ``no vault password``). It
    never carries vault content.
    """


# ── What the services need ──────────────────────────────────────────────


def _prefix(name: str) -> str:
    return name.upper().replace("-", "_")


def _refs(value: Any) -> Iterable[str]:
    """Every ``secrets.KEY`` reference in a string, list or mapping, recursively."""
    if isinstance(value, str):
        for match in _SECRET_REF_RE.finditer(value):
            yield match.group(1) or match.group(2)
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _refs(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _refs(item)


def required_secrets(
    services: Mapping[str, Any] | None,
    accessories: Mapping[str, Any] | None,
    *,
    include_build_token: bool = True,
) -> dict[str, list[str]]:
    """``{vault name: [service names]}`` for every secret the entries need.

    ``include_build_token=False`` leaves out ``build.token``: ``bay validate``
    already checks it in ``_validate_build_tokens``, and one missing name
    should fail validation once, not twice.
    """
    required: dict[str, list[str]] = {}

    def need(name: str, entry_name: str) -> None:
        users = required.setdefault(name, [])
        if entry_name not in users:
            users.append(entry_name)

    for entry_name, entry in {**(accessories or {}), **(services or {})}.items():
        if not isinstance(entry, Mapping):
            continue
        env = entry.get("env")
        secret = env.get("secret") if isinstance(env, Mapping) else None
        if isinstance(secret, list):
            for key in secret:
                need(f"{_prefix(str(entry_name))}_{key}", str(entry_name))
        elif isinstance(secret, Mapping):
            for vault_key in secret.values():
                need(str(vault_key), str(entry_name))

        db = entry.get("database")
        if isinstance(db, Mapping):
            user = db.get("user") or entry_name
            need(f"{_prefix(str(user))}_POSTGRES_PASSWORD", str(entry_name))

        middleware = entry.get("middleware")
        if isinstance(middleware, Mapping) and "basic_auth" in middleware:
            for ref in _refs(middleware.get("basic_auth")):
                need(ref, str(entry_name))

        build = entry.get("build")
        if isinstance(build, Mapping):
            sources = {"secrets": build.get("secrets")}
            if include_build_token:
                sources["token"] = build.get("token")
            for ref in _refs(sources):
                need(ref, str(entry_name))
    return required


def _load_services(cx: Context) -> tuple[dict[str, Any], dict[str, Any]]:
    if not cx.services_file.is_file():
        return {}, {}
    data = yaml.safe_load(cx.services_file.read_text()) or {}
    if not isinstance(data, Mapping):
        return {}, {}
    return dict(data.get("services") or {}), dict(data.get("accessories") or {})


# ── What the vault holds ────────────────────────────────────────────────


def names_in(vault_data: Any) -> set[str]:
    """Key names of a decrypted vault mapping. Values are not looked at."""
    if not isinstance(vault_data, Mapping):
        return set()
    inner = vault_data.get("secrets")
    source = inner if isinstance(inner, Mapping) else vault_data
    return {str(key) for key in source}


def secrets_file_for(cx: Context, env: str) -> Path | None:
    """``group_vars/<env>/secrets.yml``, else ``group_vars/all/secrets.yml``, else None."""
    for candidate in (cx.secrets_file(env), cx.secrets_file("all")):
        if candidate.is_file():
            return candidate
    return None


def vault_names(cx: Context, env: str) -> set[str]:
    """Names held by the env's vault. Raises :class:`SecretsUncheckable`.

    Same decryption path as ``bay validate`` and ``bay build``:
    ``ansible-vault decrypt --output=-`` with the fleet's ``.vault_pass``,
    read from a pipe, never written to disk. A file that is not encrypted
    is read as plain YAML.
    """
    path = secrets_file_for(cx, env)
    if path is None:
        return set()
    raw = path.read_text(errors="replace")
    if raw.lstrip().startswith("$ANSIBLE_VAULT"):
        if not cx.vault_pass.exists():
            raise SecretsUncheckable(NO_VAULT_PASSWORD)
        try:
            proc = subprocess.run(
                [
                    _ansible.tool("ansible-vault"),
                    "decrypt",
                    "--vault-password-file",
                    str(cx.vault_pass),
                    "--output=-",
                    str(path),
                ],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise SecretsUncheckable("ansible-vault not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise SecretsUncheckable("vault decrypt timed out") from exc
        if proc.returncode != 0:
            raise SecretsUncheckable("vault decrypt failed")
        raw = proc.stdout
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        # The parser's message quotes the offending line, which may be a value.
        raise SecretsUncheckable("vault content is not valid YAML") from None
    return names_in(data)


# ── The check ───────────────────────────────────────────────────────────


def compare(required: Mapping[str, list[str]], held: set[str]) -> list[MissingSecret]:
    """Names in ``required`` that ``held`` lacks, sorted by name."""
    return [
        MissingSecret(name=name, used_by=tuple(sorted(required[name])))
        for name in sorted(required)
        if name not in held
    ]


def missing_secrets(cx: Context, env: str, *, held: set[str] | None = None) -> list[MissingSecret]:
    """Every secret name the fleet's services need that ``env``'s vault lacks.

    ``held`` skips the decryption when the caller already has the names (for
    example ``bay validate``, which decrypts every vault file once). Raises
    :class:`SecretsUncheckable` when the vault cannot be read.
    """
    services, accessories = _load_services(cx)
    required = required_secrets(services, accessories)
    if not required:
        return []
    if held is None:
        held = vault_names(cx, env)
    return compare(required, held)
