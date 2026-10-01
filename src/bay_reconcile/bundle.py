"""Desired-state bundle: the CLI -> server contract.

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

from .models import ContainerSpec, ReconcilerConfig, healthcheck_to_sdk
from .observe import MANAGED_LABEL

_VALID_TYPES = ("service", "accessory", "infra")


def _healthcheck(d: Mapping[str, Any]) -> Mapping[str, object] | None:
    """This entry's healthcheck with its durations already in nanoseconds.

    Load time is deliberate. A duration the docker SDK cannot accept has to
    stop the run HERE, before the fleet is observed and before any action is
    planned, because a Recreate removes the running container before it creates
    the replacement. Validating at create time is validating after the outage
    has started. The message names the service, the key and the value, so the
    operator reads a fixable sentence instead of a Go unmarshal error.

    The spec's config_hash is NOT recomputed from this. It arrives precomputed
    in the bundle from `bay_spec_hash` over the raw inventory spec, so
    converting here cannot recreate a container that was a NoOp before.
    """
    raw = d.get("healthcheck")
    if not raw:
        return raw
    try:
        return healthcheck_to_sdk(raw)
    except ValueError as exc:
        raise ValueError(f"{d.get('name')!r}: {exc}") from exc


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
        user=d.get("user"),
        restart_policy=d.get("restart_policy") or "unless-stopped",
        mem_limit=d.get("mem_limit"),
        labels=dict(d.get("labels") or {}),
        healthcheck=_healthcheck(d),
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
    config: ReconcilerConfig = ReconcilerConfig()


# The tunables a bundle may carry, each with the type it is parsed as. An
# unknown key is an error, not ignored: a typo (`stop_timout`) would otherwise
# leave the default in force while the operator believes the override applied.
_CONFIG_KEYS: Mapping[str, type] = {
    "stop_timeout": int,
    "healthcheck_timeout": float,
    "healthcheck_poll": float,
}


def _config(raw: Any) -> ReconcilerConfig:
    """Build the ReconcilerConfig from the optional bundle ``config`` object.

    A missing object keeps every default, so a bundle from an older CLI stays
    valid. Every value must be a positive number; a bool is refused even
    though Python counts it as an int. ``stop_timeout`` must be a whole number
    because Docker takes it in whole seconds. The check runs at load time,
    before anything is observed or removed, like the healthcheck durations.
    """
    if raw is None:
        return ReconcilerConfig()
    if not isinstance(raw, Mapping):
        raise ValueError(f"config must be an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(_CONFIG_KEYS))
    if unknown:
        raise ValueError(
            f"unknown config key {unknown[0]!r} (known: {', '.join(_CONFIG_KEYS)})"
        )
    values: dict[str, Any] = {}  # each value is checked against _CONFIG_KEYS below
    for key, value in raw.items():
        kind = _CONFIG_KEYS[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"config {key!r} must be a positive number, got {value!r}")
        if kind is int and not isinstance(value, int):
            raise ValueError(f"config {key!r} must be a positive whole number, got {value!r}")
        if not value > 0 or value == float("inf"):
            raise ValueError(f"config {key!r} must be a positive number, got {value!r}")
        values[key] = kind(value)
    return ReconcilerConfig(**values)


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
        config=_config(data.get("config")),
    )
