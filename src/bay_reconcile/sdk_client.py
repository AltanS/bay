"""SDK-backed DockerClient: the only module that imports ``docker``.

Runs on the target host and turns a ContainerSpec into docker API calls. The
executor's logic is unit-tested against a FakeDockerClient; this thin adapter
is validated end-to-end on sandbox (S7). Image pulls are present-aware so a
no-op never contacts the registry.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
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


def _duration_ns(value: object) -> int:
    """A Go duration string or a number of nanoseconds -> integer nanoseconds.

    The docker SDK's Healthcheck wants ``interval``, ``timeout`` and
    ``start_period`` as integer nanoseconds. A services.yml healthcheck writes
    them in compose syntax (``5s``), and the daemon answers a string with
    "cannot unmarshal string into Go struct field
    HealthcheckConfig.Config.Healthcheck.Interval of type time.Duration".
    Numbers pass through untouched, so an inventory that already writes
    nanoseconds keeps working.
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


def _healthcheck_to_sdk(hc: Mapping[str, object]) -> dict[str, Any]:
    """A spec healthcheck block -> the docker SDK ``healthcheck`` argument.

    Only the three duration fields are rewritten; ``test`` and ``retries``
    are handed over as they were written. A duration that does not parse
    raises ValueError here, at plan time, naming the key and the value, which
    is far cheaper than a daemon 400 after the old container is already gone.
    """
    out: dict[str, Any] = dict(hc)
    for key in _DURATION_KEYS:
        if key in out and out[key] is not None:
            try:
                out[key] = _duration_ns(out[key])
            except ValueError as exc:
                raise ValueError(f"healthcheck {key}: {exc}") from exc
    if "retries" in out and out["retries"] is not None:
        out["retries"] = int(out["retries"])  # type: ignore[arg-type]
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
        local_ids: dict[str, str | None] = {}
        for ctr in self._c.containers.list(all=True):
            reference = (ctr.attrs.get("Config") or {}).get("Image")
            state = parse_state(
                ctr.attrs,
                managed_label=managed_label,
                hash_label=_HASH_LABEL,
                local_image_id=self._local_image_id(reference, local_ids),
            )
            out[state.name] = state
        return out

    def _local_image_id(self, reference: object, cache: dict[str, str | None]) -> str | None:
        """The id ``reference`` resolves to in the LOCAL image store, or None.

        One lookup per distinct reference per observe pass, cached, because a
        fleet repeats the same image across containers. None means the daemon
        has no such image, which the planner reads as no evidence of change
        rather than as a reason to redeploy.
        """
        ref = str(reference or "").strip()
        if not ref:
            return None
        if ref not in cache:
            cache[ref] = self._image_id(ref)
        return cache[ref]

    def _image_id(self, reference: str) -> str | None:
        try:
            image = self._c.images.get(reference)
        except (docker.errors.ImageNotFound, docker.errors.APIError):
            # Never pulled, or the daemon could not answer. Both are "unknown".
            return None
        return str(getattr(image, "id", "") or "") or None

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
            kwargs["healthcheck"] = _healthcheck_to_sdk(spec.healthcheck)
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
        return parse_state(
            ctr.attrs,
            managed_label=self._managed_label,
            hash_label=_HASH_LABEL,
            local_image_id=self._local_image_id((ctr.attrs.get("Config") or {}).get("Image"), {}),
        )

    def pull(self, image: str) -> None:
        # present-aware: only contact the registry when the image is absent
        try:
            self._c.images.get(image)
        except docker.errors.ImageNotFound:
            self._c.images.pull(image)
