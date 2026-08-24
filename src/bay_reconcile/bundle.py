"""Desired-state bundle (M85-S6): the CLI -> server contract.

The CLI renders every container's fully-resolved spec — env (incl. vault
secrets) already merged, config_hash precomputed where vault is decrypted — into
a JSON bundle and ships it over the existing SSH connection. This module loads
it back into typed ContainerSpecs on the server. Secrets live only in the
in-flight bundle (deleted after the run) and the container env; never a new
persistent store, and never in the config_hash label (that's an opaque hash).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import ContainerSpec
from .observe import MANAGED_LABEL

_VALID_TYPES = ("service", "accessory", "infra")


def spec_from_dict(d: Mapping[str, Any]) -> ContainerSpec:
    """Build a ContainerSpec from one bundle entry (raising on a bad type)."""
    ctype = d["type"]
    if ctype not in _VALID_TYPES:
        raise ValueError(f"invalid container type {ctype!r} for {d.get('name')!r}")
    return ContainerSpec(
        name=d["name"],
        image=d["image"],
        type=ctype,
        config_hash=d["config_hash"],
        env=dict(d.get("env") or {}),
        volumes=tuple(d.get("volumes") or ()),
        networks=tuple(d.get("networks") or ()),
        network_mode=d.get("network_mode"),
        ports=tuple(d.get("ports") or ()),
        command=d.get("command"),
        entrypoint=d.get("entrypoint"),
        restart_policy=d.get("restart_policy") or "unless-stopped",
        mem_limit=d.get("mem_limit"),
        labels=dict(d.get("labels") or {}),
        healthcheck=d.get("healthcheck"),
        log_driver=d.get("log_driver"),
        log_options=d.get("log_options"),
        zero_downtime=bool(d.get("zero_downtime", False)),
        build=bool(d.get("build", False)),
    )


@dataclass(frozen=True)
class Bundle:
    """A complete desired-state payload for one reconcile pass."""

    stack: str
    managed_label: str
    containers: tuple[ContainerSpec, ...]
    remove_orphans: bool = False


def load_bundle(data: Mapping[str, Any]) -> Bundle:
    """Parse the JSON bundle dict into a typed Bundle."""
    raw = data.get("containers") or []
    return Bundle(
        stack=str(data.get("stack") or "bay"),
        managed_label=str(
            data.get("managed_label") or MANAGED_LABEL
        ),
        containers=tuple(spec_from_dict(c) for c in raw),
        remove_orphans=bool(data.get("remove_orphans", False)),
    )
