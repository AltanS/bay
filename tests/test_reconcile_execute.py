"""Executor tests against an in-memory FakeDockerClient.

No docker daemon: exercises execute() — phase ordering, idempotency, and the
observability contract (every action recorded, failures captured not raised).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import (  # noqa: E402
    ContainerSpec,
    ContainerState,
    Create,
    NoOp,
    Plan,
    Recreate,
    Remove,
    execute,
    plan,
)
from bay_reconcile.observe import desired_port_tuples  # noqa: E402

MANAGED = "bay.managed"


class FakeDockerClient:
    def __init__(self, initial: dict[str, ContainerState] | None = None) -> None:
        self._state: dict[str, ContainerState] = dict(initial or {})
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    def observe(self, managed_label: str) -> dict[str, ContainerState]:
        with self._lock:
            return dict(self._state)

    def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
        name = name_override or spec.name
        with self._lock:
            self.calls.append(("create", name))
            self._state[name] = ContainerState(
                name=name,
                exists=True,
                image=spec.image,
                config_hash=spec.config_hash,
                status="running",
                health="healthy",
                managed=True,
                port_bindings=desired_port_tuples(spec.ports),
            )

    def stop(self, name: str, *, timeout: int = 10) -> None:
        with self._lock:
            self.calls.append(("stop", name))

    def remove(self, name: str) -> None:
        with self._lock:
            self.calls.append(("remove", name))
            self._state.pop(name, None)

    def rename(self, old: str, new: str) -> None:
        with self._lock:
            self.calls.append(("rename", old, new))
            if old in self._state:
                self._state[new] = self._state.pop(old)

    def inspect(self, name: str) -> ContainerState:
        with self._lock:
            return self._state.get(name) or ContainerState(name=name, exists=False)

    def pull(self, image: str) -> None:
        with self._lock:
            self.calls.append(("pull", image))

    def create_names(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "create"]


def _spec(name: str, *, config_hash: str = "h1", **kw: object) -> ContainerSpec:
    base: dict[str, object] = {
        "name": name,
        "image": f"{name}:latest",
        "type": "service",
        "config_hash": config_hash,
    }
    base.update(kw)
    return ContainerSpec(**base)  # type: ignore[arg-type]


class TestExecute:
    def test_create_missing(self) -> None:
        client = FakeDockerClient()
        report = execute(Plan((Create(_spec("web")),)), client)
        assert report.ok
        assert "web" in client.observe(MANAGED)
        assert ("create", "web") in client.calls

    def test_noop_makes_no_docker_calls(self) -> None:
        client = FakeDockerClient()
        report = execute(Plan((NoOp("web"),)), client)
        assert client.calls == []
        assert report.results[0].status == "skipped"

    def test_recreate_removes_then_creates(self) -> None:
        client = FakeDockerClient({"web": ContainerState("web", exists=True, config_hash="old")})
        execute(Plan((Recreate(_spec("web", config_hash="new"), "changed"),)), client)
        ops = [c[0] for c in client.calls]
        assert ops.index("remove") < ops.index("create")
        assert client.observe(MANAGED)["web"].config_hash == "new"

    def test_remove_orphan(self) -> None:
        client = FakeDockerClient({"old": ContainerState("old", exists=True, managed=True)})
        execute(Plan((Remove("old", "orphan"),)), client)
        assert "old" not in client.observe(MANAGED)

    def test_accessory_runs_before_service(self) -> None:
        client = FakeDockerClient()
        p = Plan((Create(_spec("web", type="service")), Create(_spec("db", type="accessory"))))
        execute(p, client)
        names = client.create_names()
        assert names.index("db") < names.index("web")

    def test_failure_is_recorded_not_raised(self) -> None:
        class Boom(FakeDockerClient):
            def create(self, spec: ContainerSpec, *, name_override: str | None = None) -> None:
                if spec.name == "bad":
                    raise RuntimeError("image not found")
                super().create(spec, name_override=name_override)

        client = Boom()
        report = execute(Plan((Create(_spec("good")), Create(_spec("bad")))), client)
        assert not report.ok
        assert len(report.failed) == 1
        assert report.failed[0].detail.startswith("RuntimeError")
        assert "good" in client.observe(MANAGED)  # the healthy one still landed

    def test_report_to_dict_shape(self) -> None:
        client = FakeDockerClient()
        report = execute(Plan((Create(_spec("web")), NoOp("cache"))), client)
        d = report.to_dict()
        assert d["ok"] is True
        kinds = {(r["kind"], r["name"], r["status"]) for r in d["results"]}  # type: ignore[attr-defined]
        assert ("Create", "web", "done") in kinds
        assert ("NoOp", "cache", "skipped") in kinds


class TestIdempotency:
    def test_plan_execute_observe_is_noop_second_time(self) -> None:
        client = FakeDockerClient()
        specs = [_spec("web", config_hash="h1"), _spec("db", type="accessory", config_hash="h2")]

        first = plan(specs, client.observe(MANAGED))
        assert not first.is_noop
        execute(first, client)

        second = plan(specs, client.observe(MANAGED))
        assert second.is_noop
