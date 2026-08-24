"""Pure observation helpers (M85-S3): docker attrs -> ContainerState.

The single batched ``list(all=True)`` happens in the SDK client (S4); this
module is the pure, testable parsing + port normalization it delegates to.
The port logic mirrors the ``bay_port_binding_tuple`` / ``bay_port_spec_tuple``
Ansible filters exactly — parity oracle: ``tests/test_port_drift.py``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .models import ContainerState

HASH_LABEL = "com.bay.config-hash"
MANAGED_LABEL = "bay.managed"
STACK_LABEL = "bay.stack"

# Pre-1.0 label spellings. Containers created before the M108 rename
# carry these and nothing else; discovery, config-hash comparison and orphan
# collection all read them as a fallback so a renamed fleet neither loses
# track of running containers nor recreates every one of them on the first
# post-rename deploy. New containers are only ever stamped with the new keys.
# Both tags below are dual-read only; removal is deferred to a future major
# release, not v1.1 (see docs/rename-map.md).
LEGACY_HASH_LABEL = "com.argo.config-hash"  # legacy-argo: dual-read
LEGACY_MANAGED_LABEL = "argo.managed"  # legacy-argo: dual-read


def port_binding_tuple(entry: Mapping[str, Any]) -> str:
    """A docker PortBindings entry -> '<ip>:<port>'.

    {"HostIp": "100.64.0.1", "HostPort": "5432"} -> "100.64.0.1:5432"
    {"HostIp": "",            "HostPort": "5432"} -> "0.0.0.0:5432"
    """
    if not isinstance(entry, Mapping):
        return ""
    host_ip = str(entry.get("HostIp") or "").strip() or "0.0.0.0"
    host_port = str(entry.get("HostPort") or "").strip()
    return f"{host_ip}:{host_port}"


def port_spec_tuple(spec: object) -> str:
    """A compose-style port spec -> '<ip>:<host_port>'.

    "5432:5432"            -> "0.0.0.0:5432"
    "127.0.0.1:5432:5432"  -> "127.0.0.1:5432"
    "100.64.0.1:5432:5432" -> "100.64.0.1:5432"
    """
    if spec is None:
        return ""
    s = str(spec).strip()
    if not s:
        return ""
    parts = s.split(":")
    if len(parts) == 1:
        return f"0.0.0.0:{parts[0]}"
    if len(parts) == 2:
        return f"0.0.0.0:{parts[0]}"
    return f"{parts[0]}:{parts[1]}"


def desired_port_tuples(ports: Sequence[str]) -> tuple[str, ...]:
    """Normalized, sorted host-binding tuples for a spec's desired ports."""
    return tuple(sorted(t for t in (port_spec_tuple(p) for p in ports) if t))


def observed_port_tuples(port_bindings: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Normalized, sorted host-binding tuples from HostConfig.PortBindings."""
    if not port_bindings:
        return ()
    out: list[str] = []
    for entries in port_bindings.values():
        for entry in entries or []:
            t = port_binding_tuple(entry)
            if t:
                out.append(t)
    return tuple(sorted(out))


def parse_state(
    attrs: Mapping[str, Any],
    *,
    managed_label: str,
    hash_label: str = HASH_LABEL,
) -> ContainerState:
    """Parse one docker inspect/list attrs dict into a ContainerState.

    Both the managed flag and the config hash are dual-read: a container
    stamped only with the pre-1.0 label keys is still reported as managed and
    still yields its config hash, so the planner NoOps it instead of
    recreating the entire fleet after the rename.
    """
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    state = attrs.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    host_config = attrs.get("HostConfig") or {}
    return ContainerState(
        name=str(attrs.get("Name") or "").lstrip("/"),
        exists=True,
        image=config.get("Image"),
        # Both label reads below fall back to the pre-1.0 key (remove in a
        # future major release, not v1.1 as previously noted here).
        config_hash=labels.get(hash_label, labels.get(LEGACY_HASH_LABEL)),
        status=state.get("Status"),
        health=health,
        managed=managed_label in labels or LEGACY_MANAGED_LABEL in labels,
        restart_count=int(attrs.get("RestartCount") or 0),
        port_bindings=observed_port_tuples(host_config.get("PortBindings")),
    )
