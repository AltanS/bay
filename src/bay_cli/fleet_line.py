"""``fleet: <name> (<path>)``: the first stderr line of every verb that changes something.

A verb that writes to a fleet repo or acts on a box prints which fleet it
works on before it does anything, so a stale or wrong fleet is seen at once.
The line goes to stderr, also with ``--json`` (stdout keeps the one JSON
document) and with ``--log`` (the line is printed before the log takes over).

Every command of the CLI is in exactly one of :data:`MUTATING_VERBS` and
:data:`QUIET_VERBS`. ``tests/test_self_and_fleet.py`` walks the Typer app and
fails on a command in neither, so a new verb cannot skip the line by
accident. :func:`install` wraps the callback of each mutating verb; the
wrapper finds the fleet the same way the verb will (``--fleet``,
``BAY_FLEET``, the ``fleet =`` line of the bay.toml here, ``BAY_FLEET_NAME``)
without side effects, prints the line, then runs the verb. When no fleet can
be found, nothing is printed: the verb then stops with "no fleet selected".
"""

from __future__ import annotations

import functools
import os
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import typer

#: Verbs that write to a fleet repo or act on a box. Paths are the words
#: after ``bay``. Names that do not exist yet (adopt, remove, route) are
#: listed so they print the line the day they are added.
MUTATING_VERBS: frozenset[str] = frozenset(
    {
        # daily
        "init",
        "plan",
        "approve",
        "up",
        "rollback",
        "adopt",
        "remove",
        "route add",
        "route rm",
        "route import",
        # fleet files
        "compile",
        "import",
        "fleet init",
        "alerts enable",
        "alerts disable",
        "vault edit",
        "vault encrypt",
        "vault decrypt",
        "vault set",
        "service add",
        "service edit",
        "service remove",
        "service prune-webhooks",
        "server add",
        "server remove",
        "region add",
        # boxes
        "deploy",
        "provision",
        "restore",
        "restart",
        "prune",
        "admin-shell",
        "alerts test",
        "backup run",
        "backup restore",
        "build reset",
        "gateway add-user",
        "gateway apikey",
        "gateway delete-node",
        "gateway delete-user",
        "gateway enroll",
        "gateway key",
        "gateway migrate-namespace",
        "gateway rename-node",
        "gateway rename-user",
        "gateway route-approve",
    }
)

#: Verbs that only read, or that do not act on a fleet at all (``self``).
QUIET_VERBS: frozenset[str] = frozenset(
    {
        "show",
        "status",
        "doctor",
        "validate",
        "test",
        "logs",
        "healthcheck",
        "webhook",
        "toml validate",
        "self version",
        "self update",
        "fleet ls",
        "secret missing",
        "route ls",
        "alerts list",
        "alerts doctor",
        "backup list",
        "backup status",
        "backup check",
        "build status",
        "gateway acl audit",
        "gateway nodes",
        "gateway routes",
        "gateway status",
        "gateway user-info",
        "gateway users",
        "server inspect",
        "server list",
        "service catalog",
        "service list",
        "service show",
        "vault view",
    }
)

#: Verbs that fall back to the working directory as the fleet (``context_or_cwd``).
_CWD_IS_FLEET = frozenset(
    {"service add", "service edit", "service remove", "service prune-webhooks",
     "server add", "server remove"}
)
#: Verbs that also find a fleet directory at or above the working directory.
_FLEET_DIR_ABOVE = frozenset({"plan", "up", "approve"})

#: Marks a wrapped callback, so a test can tell that :func:`install` covered it.
MARK = "__bay_fleet_line__"


def fleet_name(root: Path) -> str:
    """``name`` in ``bay.fleet.toml``, else the directory name."""
    try:
        name = tomllib.loads((root / "bay.fleet.toml").read_text()).get("name")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        name = None
    return name if isinstance(name, str) and name else root.name


def line(root: Path, name: str | None = None) -> str:
    return f"fleet: {name or fleet_name(root)} ({root})"


def _bay_toml_identity(start: Path) -> tuple[str | None, str | None]:
    """``(name, fleet)`` of the nearest bay.toml at or above ``start``."""
    for directory in [start, *start.parents]:
        path = directory / "bay.toml"
        if path.is_file():
            try:
                doc = tomllib.loads(path.read_text())
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return None, None
            name, fleet = doc.get("name"), doc.get("fleet")
            return (
                name if isinstance(name, str) else None,
                fleet if isinstance(fleet, str) and fleet else None,
            )
    return None, None


def _named(name: str | None) -> Path | None:
    from bay_cli.context import fleets_root

    if not name:
        return None
    path = fleets_root() / name
    return path if path.is_dir() else None


def _fleet_dir_above(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        if (directory / "bay.fleet.toml").is_file():
            return directory
    return None


#: The root object of the running command (``typer.Context.obj``), stored by
#: the root callback (:func:`remember`). The wrapper reads it here, because a
#: newer Typer ships Click inside itself and ``import click`` may fail.
_ROOT_OBJ: list[Any] = [None]


def remember(obj: Any) -> None:
    """Called by the root callback with the ``GlobalOptions`` (or ``Context``) it stores."""
    _ROOT_OBJ[0] = obj


def _global_fleet() -> Path | None:
    from bay_cli.context import FLEET_ENV, Context, GlobalOptions

    obj = _ROOT_OBJ[0]
    if isinstance(obj, Context):
        return obj.fleet_root
    if isinstance(obj, GlobalOptions) and obj.fleet is not None:
        return Path(obj.fleet)
    env = os.environ.get(FLEET_ENV)
    return Path(env) if env else None


def resolve(path: str, kwargs: dict[str, Any]) -> tuple[Path, str | None] | None:
    """``(fleet root, name or None)`` the verb ``path`` will act on, or None. No side effects."""
    from bay_cli.context import FLEET_NAME_ENV, fleets_root

    if path == "import":
        source, out = kwargs.get("fleet"), kwargs.get("out")
        target = Path(out or source) if (out or source) else None
        if target is None:
            return None
        name = kwargs.get("name") or (Path(source).name if source else None)
        return target.expanduser().absolute(), name
    if path == "fleet init":
        return fleets_root() / str(kwargs.get("name")), str(kwargs.get("name"))
    if path == "compile" and kwargs.get("fleet") is not None:
        return Path(kwargs["fleet"]).expanduser().resolve(), None

    found = _global_fleet()
    if found is None:
        # A fleet named by name decides, also when it is missing: the verb
        # then stops with "fleet not found", and no line is printed.
        name = (
            kwargs.get("fleet") if path == "init" else _bay_toml_identity(Path.cwd())[1]
        ) or os.environ.get(FLEET_NAME_ENV)
        if name:
            found = _named(name)
            if found is None:
                return None
    if found is None and path in _FLEET_DIR_ABOVE:
        found = _fleet_dir_above(Path.cwd())
    if found is None and path in _CWD_IS_FLEET:
        found = Path.cwd()
    if found is None:
        return None
    return found.expanduser().resolve(), None


def announce(path: str, kwargs: dict[str, Any]) -> None:
    """Print the fleet line for verb ``path`` on stderr, when its fleet can be found."""
    try:
        found = resolve(path, kwargs)
    except (OSError, ValueError):
        found = None
    if found is not None:
        typer.echo(line(*found), err=True)


def _wrap(path: str, callback: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(callback, MARK, False):
        return callback

    @functools.wraps(callback)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        announce(path, kwargs)
        return callback(*args, **kwargs)

    setattr(wrapper, MARK, True)
    return wrapper


def registered(app: typer.Typer, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    """``(path, CommandInfo)`` for every command registered on ``app`` and its sub-apps."""
    for info in app.registered_commands:
        callback = info.callback
        name = info.name or (callback.__name__.replace("_", "-") if callback else "")
        yield " ".join((*prefix, name)), info
    for group in app.registered_groups:
        sub = group.typer_instance
        if sub is None:
            continue
        name = group.name or (sub.info.name if isinstance(sub.info.name, str) else "")
        yield from registered(sub, (*prefix, name))


def install(app: typer.Typer) -> None:
    """Wrap the callback of every mutating verb registered on ``app``."""
    for path, info in registered(app):
        if path in MUTATING_VERBS and info.callback is not None:
            info.callback = _wrap(path, info.callback)
