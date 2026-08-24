"""Typed domain model for the server-side reconciler (M85-S2).

Pure data — no docker SDK, no I/O. The functional core (:mod:`planner`) consumes
these types; the imperative shell (:mod:`docker_client`, executor) turns them
into docker calls.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

ContainerType = Literal["service", "accessory", "infra"]


@dataclass(frozen=True)
class ContainerSpec:
    """Desired state of one container, fully resolved on the CLI side.

    ``config_hash`` is precomputed at bundle time (where vault is already
    decrypted) so the reconciler compares opaque strings and never touches
    secrets. For build services the hash must incorporate the resolved image
    digest, so a rebuilt image under a constant tag still busts the gate.
    """

    name: str
    image: str
    type: ContainerType
    config_hash: str
    env: Mapping[str, str] = field(default_factory=dict)
    volumes: Sequence[str] = ()
    networks: Sequence[str] = ()
    network_mode: str | None = None
    ports: Sequence[str] = ()
    command: str | Sequence[str] | None = None
    entrypoint: str | Sequence[str] | None = None
    restart_policy: str = "unless-stopped"
    mem_limit: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    healthcheck: Mapping[str, object] | None = None
    log_driver: str | None = None
    log_options: Mapping[str, str] | None = None
    zero_downtime: bool = False
    build: bool = False


@dataclass(frozen=True)
class ContainerState:
    """Observed state of one container, parsed from a single docker list call."""

    name: str
    exists: bool
    image: str | None = None
    config_hash: str | None = None
    status: str | None = None  # running | exited | created | ...
    health: str | None = None  # healthy | unhealthy | starting | None
    managed: bool = False  # carries the "managed" label (either spelling)
    restart_count: int = 0
    port_bindings: tuple[str, ...] = ()  # normalized '<ip>:<host_port>', sorted

    @property
    def running(self) -> bool:
        return self.status == "running"


# ── Actions (discriminated union) ──────────────────────────────────────────


@dataclass(frozen=True)
class NoOp:
    """Container already matches desired state — nothing to do."""

    name: str


@dataclass(frozen=True)
class Create:
    """Container does not exist yet — create it."""

    spec: ContainerSpec


@dataclass(frozen=True)
class Recreate:
    """Container exists but drifted — stop/replace in place (brief downtime)."""

    spec: ContainerSpec
    reason: str


@dataclass(frozen=True)
class CanarySwap:
    """Container drifted — zero-downtime swap via a -new canary."""

    spec: ContainerSpec
    reason: str


@dataclass(frozen=True)
class Remove:
    """Managed container no longer desired — remove it."""

    name: str
    reason: str


Action = NoOp | Create | Recreate | CanarySwap | Remove


@dataclass(frozen=True)
class Plan:
    """The ordered set of actions to converge observed -> desired."""

    actions: tuple[Action, ...] = ()

    @property
    def changes(self) -> tuple[Action, ...]:
        """Actions that actually mutate state (everything but NoOp)."""
        return tuple(a for a in self.actions if not isinstance(a, NoOp))

    @property
    def is_noop(self) -> bool:
        return len(self.changes) == 0

    def summary(self) -> dict[str, int]:
        """Count of actions by kind, for the observability report."""
        out: dict[str, int] = {}
        for action in self.actions:
            kind = type(action).__name__
            out[kind] = out.get(kind, 0) + 1
        return out


def action_target(action: Action) -> str:
    """The container name an action operates on."""
    if isinstance(action, (Create, Recreate, CanarySwap)):
        return action.spec.name
    return action.name


ActionStatus = Literal["done", "skipped", "failed"]


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one applied action.

    Observability contract: every action terminates in a recorded,
    operator-visible status — a failure is captured here, never swallowed.
    """

    action: Action
    status: ActionStatus
    detail: str = ""


@dataclass(frozen=True)
class ExecutionReport:
    """The result of executing a Plan — serialized to JSON for the operator."""

    results: tuple[ActionResult, ...] = ()

    @property
    def failed(self) -> tuple[ActionResult, ...]:
        return tuple(r for r in self.results if r.status == "failed")

    @property
    def ok(self) -> bool:
        return len(self.failed) == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "changed": any(r.status == "done" for r in self.results),
            "results": [
                {
                    "kind": type(r.action).__name__,
                    "name": action_target(r.action),
                    "status": r.status,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


@dataclass(frozen=True)
class ReconcilerConfig:
    """Tunables for the execute shell — mirror the container_lifecycle defaults."""

    canary_suffix: str = "-new"
    healthcheck_timeout: float = 120.0
    healthcheck_poll: float = 5.0
    stop_timeout: int = 30
