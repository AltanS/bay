"""Pure planning core: desired + observed -> Plan.

No I/O, no docker SDK — deterministic and fully unit-testable. This is the
server-side analogue of the Ansible ``container_lifecycle`` decision logic and
is pinned by the same parity-oracle assertions (S3).
"""
from __future__ import annotations

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
