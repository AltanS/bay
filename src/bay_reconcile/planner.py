"""Pure planning core: desired + observed -> Plan.

No I/O, no docker SDK — deterministic and fully unit-testable. This is the
server-side analogue of the Ansible ``container_lifecycle`` decision logic and
is pinned by the same parity-oracle assertions (S3).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .models import (
    Action,
    CanarySwap,
    ContainerSpec,
    ContainerState,
    Create,
    NoOp,
    Plan,
    Recreate,
    Remove,
    action_target,
)
from .observe import desired_port_tuples


def plan(
    desired: Sequence[ContainerSpec],
    observed: Mapping[str, ContainerState],
    *,
    remove_orphans: bool = False,
) -> Plan:
    """Compute the reconcile plan from desired specs and observed state.

    Decision table (mirrors the S1 config-hash gate, fleet-wide):

    - container missing            -> Create
    - config_hash matches AND the image id matches -> NoOp
    - config_hash differs/absent    -> CanarySwap (zero-downtime service) else Recreate
    - image id differs from the local one -> same change path as a config change
    - managed container not desired -> Remove (orphan cleanup)

    The image check is what the config hash alone cannot see. The hash covers
    the config TEXT, so a rebuilt image under an unchanged reference (the
    classic ``:latest``, but equally a pinned tag re-pulled after a fix) left
    the old container running and the run reported NoOp.
    """
    actions: list[Action] = []
    desired_names = {spec.name for spec in desired}

    for spec in desired:
        state = observed.get(spec.name)
        if state is None or not state.exists:
            actions.append(Create(spec))
        elif (
            state.config_hash
            and state.config_hash == spec.config_hash
            and not state.image_drifted
        ):
            actions.append(NoOp(spec.name))
        else:
            reason = _change_reason(spec, state)
            # Port-binding drift forces a standard recreate: two containers
            # cannot share a host port, so the canary path would deadlock on
            # `address already in use` (mirrors the service host-port binding
            # logic in deploy_service.yml).
            ports_drifted = desired_port_tuples(spec.ports) != state.port_bindings
            if spec.zero_downtime and spec.type == "service" and not ports_drifted:
                actions.append(CanarySwap(spec, reason))
            else:
                if ports_drifted and spec.zero_downtime and spec.type == "service":
                    reason += " (port drift — canary unsafe, standard recreate)"
                actions.append(Recreate(spec, reason))

    # Orphan removal is opt-in: a bundle that doesn't enumerate every managed
    # container must never remove one (e.g. a partial/transitional desired set).
    if remove_orphans:
        for name, state in observed.items():
            if state.managed and name not in desired_names:
                actions.append(Remove(name, "orphaned — not in desired set"))

    return Plan(tuple(actions))


#: Reason codes in :func:`describe`. Each reason is ``"<code>: <detail>"``; the
#: CLI reads the code (``bay plan --remote``), a person reads the detail.
REASON_CODES = (
    "missing",
    "orphan",
    "config_hash",
    "image",
    "env",
    "labels",
    "ports",
    "volumes",
    "memory",
    "stopped",
)

_ACTION_WORD = {
    NoOp: "noop",
    Create: "create",
    Recreate: "recreate",
    CanarySwap: "recreate",
    Remove: "remove",
}

_MAX_KEYS = 8


def describe(
    the_plan: Plan,
    desired: Sequence[ContainerSpec],
    observed: Mapping[str, ContainerState],
) -> list[dict[str, object]]:
    """One entry per container of the plan: ``{name, action, reasons}``.

    ``action`` is ``noop``, ``create``, ``recreate`` (a canary swap counts as
    one), ``start`` or ``remove``. The planner never emits ``start`` today: a
    stopped container whose hash matches is a NoOp and stays stopped, which the
    ``stopped`` reason says.

    The decision is the planner's (the config hash and the image id). The
    reasons go further and say what differs, from what docker reports for the
    running container: the image reference, env values (KEY NAMES only, never a
    value), labels, ports, volumes and the memory caps (``memory``). When the
    hash changed and none of those differ, the reason is ``config_hash`` alone.
    The hash folds in the env FILE bytes and other settings (command,
    healthcheck, networks, log options) that docker inspect does not report,
    so the cause is not observable and the report does not guess one. In
    particular it never says the env file changed in line order: the old file
    is not on the box to compare.
    """
    by_name = {spec.name: spec for spec in desired}
    out: list[dict[str, object]] = []
    for action in the_plan.actions:
        name = action_target(action)
        state = observed.get(name)
        reasons: list[str] = []
        if isinstance(action, Create):
            reasons.append("missing: no container with this name on the box")
        elif isinstance(action, Remove):
            reasons.append("orphan: a managed container that the deploy no longer lists")
        elif isinstance(action, NoOp):
            if state is not None and state.status and not state.running:
                reasons.append(
                    f"stopped: the container is {state.status}; the deploy leaves it as it is"
                )
        else:
            assert state is not None
            reasons = _diff_reasons(by_name[name], state)
            if isinstance(action, CanarySwap):
                reasons.append("zero_downtime: a canary takes over before the old one stops")
        out.append({"name": name, "action": _ACTION_WORD[type(action)], "reasons": reasons})
    return out


def _keys(keys: Sequence[str]) -> str:
    shown = ", ".join(keys[:_MAX_KEYS])
    more = len(keys) - _MAX_KEYS
    return shown + (f" and {more} more" if more > 0 else "")


def _diff_reasons(spec: ContainerSpec, state: ContainerState) -> list[str]:
    reasons: list[str] = []
    hash_changed = state.config_hash != spec.config_hash
    detail: list[str] = []
    if state.image and state.image != spec.image:
        detail.append(f"image: reference changed ({state.image} -> {spec.image})")
    if state.image_drifted:
        detail.append(f"image: {_image_reason(state)}")
    if state.env is not None:
        changed = sorted(k for k, v in spec.env.items() if state.env.get(k) != str(v))
        if changed:
            detail.append(f"env: values differ for {_keys(changed)}")
    labels = sorted(k for k, v in spec.labels.items() if state.labels.get(k) != str(v))
    if labels:
        detail.append(f"labels: differ for {_keys(labels)}")
    if desired_port_tuples(spec.ports) != state.port_bindings:
        detail.append(
            "ports: the published ports differ"
            + (" (a canary is unsafe, so a plain recreate)" if spec.zero_downtime else "")
        )
    if state.volumes is not None and tuple(sorted(spec.volumes)) != state.volumes:
        detail.append("volumes: the mounts differ")
    memory = _memory_reason(spec, state)
    if memory:
        detail.append(memory)
    if not state.config_hash:
        reasons.append("config_hash: the container has no config-hash label")
    elif hash_changed:
        text = f"config_hash: changed ({state.config_hash[:12]} -> {spec.config_hash[:12]})"
        if not detail:
            text += (
                "; image, env, labels, ports, volumes and memory match, and the cause "
                "(the env file bytes, command, healthcheck or another hashed setting) "
                "is not readable from docker inspect"
            )
        reasons.append(text)
    reasons.extend(detail)
    return reasons


_SIZE = re.compile(r"^(\d+)([bkmg]?)b?$", re.IGNORECASE)
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str | None) -> int | None:
    """Bytes in a docker size string (``512m``); 0 for unset; None when unreadable."""
    if value is None or value == "":
        return 0
    match = _SIZE.match(str(value).strip())
    if match is None:
        return None
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def _fmt_bytes(n: int) -> str:
    if n < 0:
        return "unlimited"
    if n == 0:
        return "none"
    for unit, size in (("g", 1024**3), ("m", 1024**2), ("k", 1024)):
        if n % size == 0:
            return f"{n // size}{unit}"
    return f"{n}b"


def _memory_reason(spec: ContainerSpec, state: ContainerState) -> str | None:
    """Name a difference in the memory cap or the memory-plus-swap cap.

    The running container reports both in bytes. A spec with no memswap_limit
    leaves swap to docker, which sets the swap cap to twice the memory cap
    (0 when there is no memory cap), so those two values count as a match.
    """
    want_mem, want_swap = _bytes(spec.mem_limit), _bytes(spec.memswap_limit)
    if want_mem is None or want_swap is None:
        return None
    parts: list[str] = []
    if state.memory is not None and state.memory != want_mem:
        parts.append(f"mem_limit {_fmt_bytes(state.memory)} -> {_fmt_bytes(want_mem)}")
    if state.memory_swap is not None:
        if spec.memswap_limit:
            swap_ok = state.memory_swap == want_swap
        else:
            swap_ok = state.memory_swap in (0, 2 * (state.memory or 0))
        if not swap_ok:
            now = _fmt_bytes(state.memory_swap)
            then = _fmt_bytes(want_swap) if spec.memswap_limit else "docker's default"
            parts.append(f"memswap_limit {now} -> {then}")
    if not parts:
        return None
    return "memory: " + ", ".join(parts)


def _change_reason(spec: ContainerSpec, state: ContainerState) -> str:
    if not state.config_hash:
        return "no config-hash label (pre-reconciler container)"
    if state.config_hash == spec.config_hash:
        return _image_reason(state)
    reason = f"config-hash changed ({state.config_hash[:12]} -> {spec.config_hash[:12]})"
    if state.image_drifted:
        reason += f"; {_image_reason(state)}"
    return reason


def _image_reason(state: ContainerState) -> str:
    running = _short_id(state.image_id)
    local = _short_id(state.local_image_id)
    return f"image changed under {state.image or 'its reference'} ({running} -> {local})"


def _short_id(image_id: str | None) -> str:
    """'sha256:abcdef...' -> 'abcdef012345'; a bare id is shortened the same way."""
    if not image_id:
        return "unknown"
    return image_id.split(":", 1)[-1][:12]
