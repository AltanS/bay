"""The Context object: every path a command needs, resolved once.

A command used to call ``paths.find_bay_dir()`` and ``paths.consumer_root()``
itself, which scattered 100+ copies of "where is the project?" through the CLI.
Now the CLI builds one :class:`Context` in :meth:`Context.resolve` and every
command and helper receives it as a parameter (commands get it from
``typer.Context.obj`` through :func:`context_from`).

Vocabulary:

``fleet_root``
    The directory holding ``group_vars/``, ``hosts/`` and ``.vault_pass``.
    Today that is the consumer repo root.
``framework_root``
    The Bay framework checkout (roles, playbooks, ``version.yml``). Today that
    is the consumer's ``.bay/`` clone. When the fleet was found by path, the
    CLI is installed on the machine and the framework is the repo this very
    package runs from.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from bay_cli.errors import BayError

if TYPE_CHECKING:
    import typer

#: Environment variable naming the fleet directory (discovery step 2).
FLEET_ENV = "BAY_FLEET"
#: Environment variable choosing one fleet under ``~/.config/bay/fleets``.
FLEET_NAME_ENV = "BAY_FLEET_NAME"

# Where the CLI keeps the fleets it manages (discovery step 3).
_FLEETS_DIR = Path(".config") / "bay" / "fleets"

#: Where a Context came from. Shown by ``bay status`` when it matters.
SOURCE_FLAG = "--fleet"
SOURCE_ENV = "BAY_FLEET"
SOURCE_FLEETS_DIR = "~/.config/bay/fleets"
SOURCE_WALK_UP = "walk-up"
SOURCE_CWD = "cwd"


def package_root() -> Path:
    """The repo root of the running ``bay_cli`` package (``src/bay_cli/../..``)."""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Context:
    """Resolved locations for one CLI invocation. Build with :meth:`resolve`."""

    fleet_root: Path
    framework_root: Path
    source: str = SOURCE_WALK_UP

    # ── Derived locations ────────────────────────────────────────────────
    @property
    def group_vars(self) -> Path:
        return self.fleet_root / "group_vars"

    @property
    def hosts_dir(self) -> Path:
        return self.fleet_root / "hosts"

    @property
    def vault_pass(self) -> Path:
        return self.fleet_root / ".vault_pass"

    @property
    def services_file(self) -> Path:
        return self.group_vars / "all" / "services.yml"

    @property
    def main_vars_file(self) -> Path:
        """``group_vars/all/main.yml``, the file that holds ``stack_name`` and friends."""
        return self.group_vars / "all" / "main.yml"

    # ── Helpers for paths that were repeated as literals ─────────────────
    def inventory(self, env: str) -> Path:
        """Inventory file for an environment: ``hosts/<env>``."""
        return self.hosts_dir / env

    def env_dir(self, env: str) -> Path:
        """``group_vars/<env>/`` (``env`` may be ``all`` or a region)."""
        return self.group_vars / env

    def env_file(self, env: str, name: str) -> Path:
        """``group_vars/<env>/<name>``, for example ``env_file("all", "main.yml")``."""
        return self.group_vars / env / name

    def secrets_file(self, env: str) -> Path:
        """Encrypted secrets for an environment: ``group_vars/<env>/secrets.yml``."""
        return self.group_vars / env / "secrets.yml"

    # ── Construction ─────────────────────────────────────────────────────
    @classmethod
    def for_fleet_root(
        cls,
        fleet_root: Path,
        framework_root: Path | None = None,
        *,
        source: str = SOURCE_FLAG,
    ) -> Context:
        """Build a Context for a known fleet directory.

        ``framework_root`` defaults to the running package's own repo.
        """
        return cls(
            fleet_root=fleet_root,
            framework_root=framework_root if framework_root is not None else package_root(),
            source=source,
        )

    def with_fleet_root(self, fleet_root: Path) -> Context:
        return replace(self, fleet_root=fleet_root)

    @classmethod
    def resolve(cls, fleet: Path | None = None) -> Context:
        """Find the fleet and build the Context. The only place that does so.

        Discovery order:

        1. ``fleet``, the value of the global ``--fleet <path>`` option.
        2. The ``BAY_FLEET`` environment variable.
        3. ``~/.config/bay/fleets/<name>``, only when ``BAY_FLEET_NAME`` is
           set. There is no auto-pick of a lone fleet while step 4 exists:
           the first fleet dir on a machine would otherwise capture every
           command run inside a consumer repo.
        4. TRANSITION ONLY: walk up from the working directory to a ``.bay/``
           framework clone (the consumer layout). This keeps every current
           consumer command working unchanged until the cutover. Step 4 is
           removed in S08, together with ``find_bay_dir`` and ``consumer_root``.

        Steps 1 to 3 give a fleet that carries no framework clone, so the
        framework root is the repo this package runs from. Step 4 keeps the
        ``.bay/`` clone as the framework root.
        """
        # Imported here, not at module scope, so `paths` stays off the import
        # path of commands that never resolve a Context. Reached through the
        # module attribute so a test can patch `bay_cli.paths.find_bay_dir`.
        from bay_cli import paths

        if fleet is not None:
            return cls._from_fleet_path(Path(fleet), SOURCE_FLAG)

        env_fleet = os.environ.get(FLEET_ENV)
        if env_fleet:
            return cls._from_fleet_path(Path(env_fleet), SOURCE_ENV)

        named = _fleet_from_fleets_dir()
        if named is not None:
            return cls._from_fleet_path(named, SOURCE_FLEETS_DIR)

        # Step 4 (transition only, removed in S08).
        bay_dir = paths.find_bay_dir()
        return cls(
            fleet_root=paths.consumer_root(bay_dir),
            framework_root=bay_dir,
            source=SOURCE_WALK_UP,
        )

    @classmethod
    def for_fleet_name(cls, name: str) -> Context:
        """The fleet ``~/.config/bay/fleets/<name>``, as ``BAY_FLEET_NAME=<name>`` would find it.

        Used when a bay.toml names its fleet (``fleet = "<name>"``) and no
        ``--fleet <path>`` was given.
        """
        return cls._from_fleet_path(fleet_dir_for_name(name), SOURCE_FLEETS_DIR)

    @classmethod
    def _from_fleet_path(cls, path: Path, source: str) -> Context:
        fleet_root = path.expanduser().resolve()
        if not fleet_root.is_dir():
            raise BayError(
                f"fleet directory not found: {fleet_root} (from {source})",
                hint="Pass an existing directory to --fleet, or unset BAY_FLEET.",
            )
        return cls(fleet_root=fleet_root, framework_root=package_root(), source=source)


def _fleet_from_fleets_dir() -> Path | None:
    """Discovery step 3: ``~/.config/bay/fleets/<name>``, only if ``BAY_FLEET_NAME`` is set.

    S08 may reinstate "use the one fleet that exists" once the walk-up (step 4)
    is gone. Until then it would hijack consumer-repo runs, so it is off.
    """
    name = os.environ.get(FLEET_NAME_ENV)
    if not name:
        return None
    return fleet_dir_for_name(name, hint=f"Unset {FLEET_NAME_ENV}, or create the fleet there.")


def fleets_root() -> Path:
    """``~/.config/bay/fleets``, the directory that holds every fleet on this machine."""
    return Path.home() / _FLEETS_DIR


def fleet_dir_for_name(name: str, *, hint: str | None = None) -> Path:
    """``~/.config/bay/fleets/<name>``. Raises BayError when it is not a directory."""
    candidate = fleets_root() / name
    if not candidate.is_dir():
        raise BayError(
            f"fleet '{name}' not found at {candidate}",
            hint=hint or "Clone the fleet there, or pass --fleet <path>.",
        )
    return candidate


@dataclass
class GlobalOptions:
    """What the root Typer callback stores in ``typer.Context.obj``.

    ``Context`` is resolved lazily: commands such as ``bay secret`` must keep
    working outside any fleet, so the lookup waits until a command asks.
    """

    fleet: Path | None = None
    _resolved: Context | None = field(default=None, repr=False)

    def context(self) -> Context:
        if self._resolved is None:
            self._resolved = Context.resolve(self.fleet)
        return self._resolved


def context_from(ctx: typer.Context | None) -> Context:
    """Return the :class:`Context` for a running command.

    ``ctx.obj`` is the root callback's :class:`GlobalOptions`, or an already
    built :class:`Context` (tests and programmatic callers). With no context
    at all, discovery runs with no ``--fleet`` flag.
    """
    obj = ctx.find_root().obj if ctx is not None else None
    if isinstance(obj, Context):
        return obj
    if isinstance(obj, GlobalOptions):
        return obj.context()
    return Context.resolve(None)


def _fleet_is_explicit(ctx: typer.Context | None) -> bool:
    obj = ctx.find_root().obj if ctx is not None else None
    if isinstance(obj, Context):
        return obj.source in (SOURCE_FLAG, SOURCE_ENV, SOURCE_FLEETS_DIR)
    if isinstance(obj, GlobalOptions) and obj.fleet is not None:
        return True
    return bool(os.environ.get(FLEET_ENV) or os.environ.get(FLEET_NAME_ENV))


def context_or_cwd(ctx: typer.Context | None) -> Context:
    """Like :func:`context_from`, but a project-less directory means "here".

    For the commands that edit ``services.yml`` and work before any framework
    checkout exists (``bay service add`` in a fresh directory). A fleet the
    operator named on purpose (``--fleet``, ``BAY_FLEET``, ``BAY_FLEET_NAME``)
    is never replaced by the working directory: that error is raised.
    """
    try:
        return context_from(ctx)
    except BayError:
        if _fleet_is_explicit(ctx):
            raise
        return Context.for_fleet_root(Path.cwd(), source=SOURCE_CWD)
