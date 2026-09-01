"""SDK-backed DockerClient: the only module that imports ``docker``.

Runs on the target host and turns a ContainerSpec into docker API calls. The
executor's logic is unit-tested against a FakeDockerClient; this thin adapter
is validated end-to-end on sandbox (S7). Image pulls are present-aware so a
no-op never contacts the registry.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import docker

from .models import ContainerSpec, ContainerState
from .observe import (
    HASH_LABEL as _HASH_LABEL,
)
from .observe import (
    MANAGED_LABEL,
    STACK_LABEL,
    parse_state,
)


def _ctr_port_key(ctr: str) -> str:
    """Container-port key for the docker SDK ``ports`` mapping.

    Preserve an explicit protocol suffix (e.g. ``3478/udp``); default bare
    ports to ``/tcp``. Without this, ``3478/udp`` became ``3478/udp/tcp`` and
    Docker rejected it with "unknown protocol".
    """
    return ctr if "/" in ctr else f"{ctr}/tcp"


def _ports_to_sdk(ports: Sequence[str]) -> dict[str, object]:
    """'<ip>:<host>:<ctr>[/proto]' / '<host>:<ctr>[/proto]' -> docker SDK ``ports`` mapping."""
    out: dict[str, object] = {}
    for raw in ports:
        parts = str(raw).split(":")
        if len(parts) == 3:
            ip, host, ctr = parts
            out[_ctr_port_key(ctr)] = (ip, host)
        elif len(parts) == 2:
            host, ctr = parts
            out[_ctr_port_key(ctr)] = host
    return out


class SdkDockerClient:
    """Concrete DockerClient backed by ``docker.from_env()``."""

    def __init__(
        self,
        *,
        managed_label: str = MANAGED_LABEL,
        stack_label: str = STACK_LABEL,
        stack: str = "bay",
    ) -> None:
        self._c: Any = docker.from_env()
        self._managed_label = managed_label
        self._stack_label = stack_label
        self._stack = stack

    def observe(self, managed_label: str) -> dict[str, ContainerState]:
        out: dict[str, ContainerState] = {}
        for ctr in self._c.containers.list(all=True):
            state = parse_state(ctr.attrs, managed_label=managed_label, hash_label=_HASH_LABEL)
            out[state.name] = state
        return out

    def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
        labels = dict(spec.labels)
        labels[_HASH_LABEL] = spec.config_hash
        labels[self._managed_label] = "true"
        labels[self._stack_label] = self._stack
        kwargs: dict[str, Any] = {
            "name": name_override or spec.name,
            "image": spec.image,
            "detach": True,
            "restart_policy": {"Name": spec.restart_policy},
            "environment": dict(spec.env),
            "labels": labels,
        }
        if spec.network_mode:
            kwargs["network_mode"] = spec.network_mode
        elif spec.networks:
            kwargs["network"] = spec.networks[0]
        if spec.volumes:
            kwargs["volumes"] = list(spec.volumes)
        if spec.command is not None:
            kwargs["command"] = spec.command
        if spec.entrypoint is not None:
            kwargs["entrypoint"] = spec.entrypoint
        if spec.user:
            kwargs["user"] = spec.user
        if spec.mem_limit:
            kwargs["mem_limit"] = spec.mem_limit
        if spec.ports:
            kwargs["ports"] = _ports_to_sdk(spec.ports)
        if spec.healthcheck:
            kwargs["healthcheck"] = dict(spec.healthcheck)
        if spec.log_driver:
            kwargs["log_config"] = {"type": spec.log_driver, "config": dict(spec.log_options or {})}
        self._c.containers.run(**kwargs)

    def stop(self, name: str, *, timeout: int = 10) -> None:
        self._c.containers.get(name).stop(timeout=timeout)

    def remove(self, name: str) -> None:
        try:
            self._c.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass

    def rename(self, old: str, new: str) -> None:
        self._c.containers.get(old).rename(new)

    def inspect(self, name: str) -> ContainerState:
        try:
            ctr = self._c.containers.get(name)
        except docker.errors.NotFound:
            return ContainerState(name=name, exists=False)
        return parse_state(ctr.attrs, managed_label=self._managed_label, hash_label=_HASH_LABEL)

    def pull(self, image: str) -> None:
        # present-aware: only contact the registry when the image is absent
        try:
            self._c.images.get(image)
        except docker.errors.ImageNotFound:
            self._c.images.pull(image)
