"""Imperative shell: apply a Plan via a DockerClient.

The pure planner decided WHAT to do; this does it — concurrently within a
dependency phase (infra -> accessory -> service), with orphan removals last so a
linked accessory is up before the service that needs it. Every action
terminates in a recorded ActionResult (observability contract); a failure is
captured, not raised, so one bad container doesn't abort the rest.

CanarySwap runs the zero-downtime sequence: start a ``-new`` canary (identical
Traefik labels => load-balanced), health-gate it, stop+remove the old, rename
the canary into place. On any failure it rescues — tears down the canary and
falls back to a standard recreate (brief downtime), mirroring deploy_service.yml.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from .docker_client import DockerClient
from .models import (
    Action,
    ActionResult,
    CanarySwap,
    ContainerSpec,
    Create,
    ExecutionReport,
    NoOp,
    Plan,
    ReconcilerConfig,
    Recreate,
    Remove,
)

_PHASE_ORDER = {"infra": 0, "accessory": 1, "service": 2}


class _CanaryUnhealthy(RuntimeError):
    """Canary never reached a healthy state within the timeout."""


def execute(
    plan: Plan,
    client: DockerClient,
    *,
    config: ReconcilerConfig | None = None,
    max_workers: int = 8,
) -> ExecutionReport:
    """Apply every action in ``plan`` and return a per-action report."""
    cfg = config or ReconcilerConfig()
    results: list[ActionResult] = [
        ActionResult(a, "skipped", "config-hash match")
        for a in plan.actions
        if isinstance(a, NoOp)
    ]

    changes = [a for a in plan.actions if isinstance(a, Create | Recreate | CanarySwap)]
    removes = [a for a in plan.actions if isinstance(a, Remove)]

    # Dependency phases: infra/accessories up before services that link them.
    for phase in (0, 1, 2):
        batch = [a for a in changes if _PHASE_ORDER.get(a.spec.type, 99) == phase]
        results.extend(_run_phase(batch, client, cfg, max_workers))

    # Orphan removals last.
    results.extend(_run_batch(list(removes), client, cfg, max_workers))
    return ExecutionReport(tuple(results))


def _run_phase(
    batch: Sequence[Action],
    client: DockerClient,
    cfg: ReconcilerConfig,
    max_workers: int,
) -> list[ActionResult]:
    """Run one dependency phase: canary swaps one at a time, the rest in parallel.

    A canary swap keeps the old container running until the new one is
    healthy, so for the length of its health wait it doubles that service's
    memory. Run several at once and those peaks add up. On 2026-09-18 three
    canaries started together on a 4 GB host whose swap was already full, the
    kernel killed one of them host-wide, and its rescue recreated the service
    with downtime. One at a time, the peak is the largest single service, not
    the sum. The other actions keep running in parallel first; a Recreate
    removes before it creates, so it adds no peak of its own.

    Results come back in plan order, whatever order they ran in.
    """
    canaries = [a for a in batch if isinstance(a, CanarySwap)]
    others = [a for a in batch if not isinstance(a, CanarySwap)]
    done = _run_batch(others, client, cfg, max_workers)
    done += _run_batch(canaries, client, cfg, 1)
    position = {id(a): i for i, a in enumerate(batch)}
    return sorted(done, key=lambda r: position[id(r.action)])


def _run_batch(
    actions: Sequence[Action],
    client: DockerClient,
    cfg: ReconcilerConfig,
    max_workers: int,
) -> list[ActionResult]:
    if not actions:
        return []
    out: list[ActionResult] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(actions))) as pool:
        futures = {pool.submit(_apply, a, client, cfg): a for a in actions}
        for future in futures:
            action = futures[future]
            try:
                out.append(ActionResult(action, "done", future.result()))
            except Exception as exc:
                # A Recreate removes the old container before it creates the new
                # one, so a create that fails here leaves nothing running. On
                # 2026-09-10 a healthcheck written in compose syntax was handed
                # to the daemon unconverted, the create returned 400, and
                # postgres was absent for about ten minutes; every service on
                # that host went down with it. Healthcheck durations are now
                # converted and validated in bundle.spec_from_dict, before this
                # function is ever reached, but the ordering is still the real
                # hazard: any other create the daemon rejects lands here with
                # the old container already gone. The fix is
                # to create the replacement first and remove the old one only
                # after it exists, or at least to dry create against the daemon
                # before the remove. Neither is done yet.
                out.append(ActionResult(action, "failed", f"{type(exc).__name__}: {exc}"))
    return out


def _apply(action: Action, client: DockerClient, cfg: ReconcilerConfig) -> str:
    if isinstance(action, Create):
        if not action.spec.build:
            client.pull(action.spec.image)
        client.create(action.spec)
        return f"created {action.spec.name}"
    if isinstance(action, CanarySwap):
        return _canary_swap(action.spec, client, cfg)
    if isinstance(action, Recreate):
        # Pull first: the old container keeps serving while the image downloads.
        if not action.spec.build:
            client.pull(action.spec.image)
        _stop_then_remove(client, action.spec.name, cfg)
        client.create(action.spec)
        return f"recreated {action.spec.name}"
    if isinstance(action, Remove):
        _stop_then_remove(client, action.name, cfg)
        return f"removed {action.name}"
    return "noop"


def _stop_then_remove(client: DockerClient, name: str, cfg: ReconcilerConfig) -> None:
    """Stop ``name`` gracefully, then remove it.

    A bare ``remove`` is a force remove, and Docker SIGKILLs the process. On
    2026-10-01 a recreate killed a running postgres that way and it came back
    with "database system was not properly shut down; automatic recovery in
    progress". Postgres replays its WAL; Redis or Valkey would lose every write
    since its last snapshot. ``stop`` sends the image's STOPSIGNAL (SIGINT for
    postgres) and waits ``stop_timeout`` seconds before Docker kills it anyway,
    so a hung process still goes. The remove stays forced for that case. Both
    calls are no-ops for a container that is already stopped or gone.
    """
    client.stop(name, timeout=cfg.stop_timeout)
    client.remove(name)


def _canary_swap(spec: ContainerSpec, client: DockerClient, cfg: ReconcilerConfig) -> str:
    canary = f"{spec.name}{cfg.canary_suffix}"
    if not spec.build:
        client.pull(spec.image)
    client.create(spec, name_override=canary)
    try:
        if not _wait_healthy(client, canary, cfg):
            raise _CanaryUnhealthy(f"{canary} did not become healthy")
        _stop_then_remove(client, spec.name, cfg)
        client.rename(canary, spec.name)
        return f"canary-swapped {spec.name}"
    except Exception as exc:
        # Rescue: tear down the canary, fall back to a standard recreate.
        # Both containers get the graceful stop. The swap can fail after the
        # old container was already stopped and removed (for example `rename`
        # raised), and then the canary is the only live copy of the workload.
        # A force remove would SIGKILL it. A canary that failed its health gate
        # is stopped just the same, which costs nothing. Both calls tolerate a
        # container that is already stopped or gone.
        _stop_then_remove(client, canary, cfg)
        _stop_then_remove(client, spec.name, cfg)
        client.create(spec)
        return f"recreated {spec.name} (canary fallback: {type(exc).__name__})"


def _wait_healthy(client: DockerClient, name: str, cfg: ReconcilerConfig) -> bool:
    """Poll until the canary is healthy.

    Fails fast on a crash loop (restart_count > 0). A container with no
    healthcheck passes once running. A transient 'unhealthy'/'starting' is
    tolerated — we keep polling rather than fail on a single bad read
    (Docker 29 reports a brief unhealthy right after start).
    """
    poll = max(cfg.healthcheck_poll, 0.001)
    attempts = max(1, int(cfg.healthcheck_timeout / poll))
    for attempt in range(attempts):
        state = client.inspect(name)
        if state.restart_count > 0:
            return False
        if state.health is None or state.health == "healthy":
            return True
        if attempt < attempts - 1:
            time.sleep(cfg.healthcheck_poll)
    return False
