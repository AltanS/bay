"""Pure planning core (M85-S2/S3): desired + observed -> Plan.

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
    - config_hash matches (stamped)-> NoOp
    - config_hash differs/absent    -> CanarySwap (zero-downtime service) else Recreate
    - managed container not desired -> Remove (orphan cleanup)
    """
    actions: list[Action] = []
    desired_names = {spec.name for spec in desired}

    for spec in desired:
        state = observed.get(spec.name)
        if state is None or not state.exists:
            actions.append(Create(spec))
        elif state.config_hash and state.config_hash == spec.config_hash:
            actions.append(NoOp(spec.name))
        else:
            reason = _change_reason(spec, state)
            # Port-binding drift forces a standard recreate: two containers
            # cannot share a host port, so the canary path would deadlock on
            # `address already in use` (mirrors M88 in deploy_service.yml).
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
    return f"config-hash changed ({state.config_hash[:12]} -> {spec.config_hash[:12]})"
