"""Imperative shell (M85-S4/S5): apply a Plan via a DockerClient.

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
        results.extend(_run_batch(batch, client, cfg, max_workers))

    # Orphan removals last.
    results.extend(_run_batch(list(removes), client, cfg, max_workers))
    return ExecutionReport(tuple(results))


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
        if not action.spec.build:
            client.pull(action.spec.image)
        client.remove(action.spec.name)
        client.create(action.spec)
        return f"recreated {action.spec.name}"
    if isinstance(action, Remove):
        client.remove(action.name)
        return f"removed {action.name}"
    return "noop"


def _canary_swap(spec: ContainerSpec, client: DockerClient, cfg: ReconcilerConfig) -> str:
    canary = f"{spec.name}{cfg.canary_suffix}"
    if not spec.build:
        client.pull(spec.image)
    client.create(spec, name_override=canary)
    try:
        if not _wait_healthy(client, canary, cfg):
            raise _CanaryUnhealthy(f"{canary} did not become healthy")
        client.stop(spec.name, timeout=cfg.stop_timeout)
        client.remove(spec.name)
        client.rename(canary, spec.name)
        return f"canary-swapped {spec.name}"
    except Exception as exc:
        # Rescue: tear down the canary, fall back to a standard recreate.
        client.remove(canary)
        client.remove(spec.name)
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
