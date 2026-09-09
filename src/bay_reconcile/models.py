"""Typed domain model for the server-side reconciler.

Pure data — no docker SDK, no I/O. The functional core (:mod:`planner`) consumes
these types; the imperative shell (:mod:`docker_client`, executor) turns them
into docker calls.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

ContainerType = Literal["service", "accessory", "infra"]

_DURATION_UNITS_NS: dict[str, int] = {
    "ns": 1,
    "us": 1_000,
    "µs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}

_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")

_DURATION_KEYS = ("interval", "timeout", "start_period")


def duration_ns(value: object) -> int:
    """A Go duration string or a number of nanoseconds -> integer nanoseconds.

    The docker SDK's ``Healthcheck`` wants ``interval``, ``timeout`` and
    ``start_period`` as integer nanoseconds. A services.yml healthcheck writes
    them in compose syntax (``5s``), and the daemon rejects a string with
    "cannot unmarshal string into Go struct field
    HealthcheckConfig.Config.Healthcheck.Interval of type time.Duration".
    Numbers pass through as int, so a spec that already carries nanoseconds is
    converted a second time without changing.
    """
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int | float):
        return int(value)
    text = str(value).strip()
    if not text:
        raise ValueError("not a duration: empty string")
    sign = 1
    if text[0] in "+-":
        sign = -1 if text[0] == "-" else 1
        text = text[1:]
    total = 0.0
    position = 0
    for match in _DURATION_PART_RE.finditer(text):
        if match.start() != position:
            break
        total += float(match.group(1)) * _DURATION_UNITS_NS[match.group(2)]
        position = match.end()
    if position != len(text) or not position:
        raise ValueError(f"not a duration: {value!r}")
    return sign * int(total)


def healthcheck_to_sdk(hc: Mapping[str, object]) -> dict[str, Any]:
    """A healthcheck block -> the docker SDK ``healthcheck`` argument.

    Where this runs is the whole point. The reconciler calls it from
    ``bundle.spec_from_dict``, while the JSON bundle is being turned into
    ContainerSpecs, which is before the fleet is observed and before a single
    action is planned. That is the only place a rejected duration is free. By
    the time ``SdkDockerClient.create`` runs, a Recreate has already removed
    the running container, so raising there costs the same outage the daemon's
    own 400 cost: on 2026-09-10 postgres was absent for about ten minutes and
    every service on that host that talks to it went down. A bad duration must
    therefore stop the run before the first remove, not at the create.

    ``create`` still calls this, on a spec whose durations are already ints. It
    is a passthrough then, and it stays because a ContainerSpec built by hand
    rather than loaded from a bundle carries whatever its caller wrote, and
    ``healthcheck`` is typed ``Mapping[str, object]``, which guarantees
    nothing.

    Only the three duration fields are rewritten. ``test`` is handed over
    untouched and ``retries`` is coerced to int.
    """
    out: dict[str, Any] = dict(hc)
    for key in _DURATION_KEYS:
        if key in out and out[key] is not None:
            try:
                out[key] = duration_ns(out[key])
            except ValueError as exc:
                raise ValueError(f"healthcheck {key}: {exc}") from exc
    if "retries" in out and out["retries"] is not None:
        out["retries"] = int(out["retries"])  # type: ignore[arg-type]
    return out


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
    user: str | None = None  # "<uid>:<gid>" — see the webhook receiver spec
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
    image: str | None = None  # the image REFERENCE, e.g. "bay-webhook:latest"
    config_hash: str | None = None
    status: str | None = None  # running | exited | created | ...
    health: str | None = None  # healthy | unhealthy | starting | None
    managed: bool = False  # carries the "managed" label (either spelling)
    restart_count: int = 0
    port_bindings: tuple[str, ...] = ()  # normalized '<ip>:<host_port>', sorted
    # The image id the RUNNING container was created from (docker's top-level
    # ``.Image``), and the id that same reference resolves to on the host RIGHT
    # NOW. They diverge when the tag is floating and the image was rebuilt or
    # re-pulled underneath a container that is still running the old layers.
    image_id: str | None = None
    local_image_id: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def image_drifted(self) -> bool:
        """True when the local image for this reference is not the running one.

        Unknown is not drift. A never-pulled image (``local_image_id`` None) or
        a container docker reported without an id yields False, so a missing
        image can never force a redeploy on its own.
        """
        if not self.image_id or not self.local_image_id:
            return False
        return self.image_id != self.local_image_id


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
    # 1s, not 5s: the timeout is the safety budget, the poll is only the
    # granularity of noticing. A canary that goes healthy at t=0.3s used to be
    # waited on for a full 5s per swap. The attempt count rises to match; the
    # worst case is unchanged.
    healthcheck_poll: float = 1.0
    stop_timeout: int = 30
