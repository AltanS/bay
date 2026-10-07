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
    ReconcilerConfig,
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
        self.calls: list[tuple[object, ...]] = []
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
            self.calls.append(("stop", name, timeout))

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

    def create_names(self) -> list[object]:
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


class TestGracefulStop:
    """A container that may run the real workload is stopped before removal.

    ``remove`` is a force remove, and Docker SIGKILLs the process. A recreate
    did that to a running postgres on 2026-10-01; Redis or Valkey would have
    lost every write since its last snapshot.
    """

    CFG = ReconcilerConfig(stop_timeout=17)

    def test_recreate_pulls_then_stops_old_then_removes_then_creates(self) -> None:
        client = FakeDockerClient({"web": ContainerState("web", exists=True, config_hash="old")})
        report = execute(
            Plan((Recreate(_spec("web", config_hash="new"), "changed"),)), client, config=self.CFG
        )
        assert report.ok
        # The pull comes first so the download does not lengthen the downtime.
        assert client.calls == [
            ("pull", "web:latest"),
            ("stop", "web", 17),
            ("remove", "web"),
            ("create", "web"),
        ]

    def test_remove_orphan_stops_then_removes(self) -> None:
        client = FakeDockerClient({"old": ContainerState("old", exists=True, managed=True)})
        report = execute(Plan((Remove("old", "orphan"),)), client, config=self.CFG)
        assert report.ok
        assert client.calls == [("stop", "old", 17), ("remove", "old")]

    def test_default_stop_timeout_reaches_the_stop(self) -> None:
        client = FakeDockerClient({"old": ContainerState("old", exists=True, managed=True)})
        execute(Plan((Remove("old", "orphan"),)), client)
        assert ("stop", "old", ReconcilerConfig().stop_timeout) in client.calls

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


# ── release (bay.toml `release`, M118/04) ─────────────────────────────────────


class _ReleaseClient(FakeDockerClient):
    """A fake that records ``run_release`` and answers with a given exit code."""

    def __init__(
        self, initial: dict[str, ContainerState] | None = None, *, code: int | None = 0
    ) -> None:
        super().__init__(initial)
        self.code = code
        self.release_timeouts: list[float] = []

    def run_release(self, spec: ContainerSpec, *, timeout: float) -> tuple[int | None, str]:
        with self._lock:
            self.calls.append(("release", spec.name, spec.image, spec.release))
            self.release_timeouts.append(timeout)
        return self.code, "migrations: 3 applied" if self.code == 0 else "relation users missing"


class TestRelease:
    def test_reconcile_runs_release_before_recreate(self) -> None:
        """The release runs after the pull and before the old container stops.

        A failed release fails the action before any stop: the old container
        keeps serving, and the receipt of the pass says ``failed``.
        """
        from bay_reconcile.receipt import build_receipt

        old = {"web": ContainerState("web", exists=True, config_hash="old")}
        spec = _spec("web", config_hash="new", release="bin/migrate")
        cfg = ReconcilerConfig(stop_timeout=5, release_timeout=42)

        good = _ReleaseClient(dict(old))
        report = execute(Plan((Recreate(spec, "changed"),)), good, config=cfg)
        assert report.ok
        assert good.calls == [
            ("pull", "web:latest"),
            ("release", "web", "web:latest", "bin/migrate"),
            ("stop", "web", 5),
            ("remove", "web"),
            ("create", "web"),
        ]
        assert good.release_timeouts == [42]

        bad = _ReleaseClient(dict(old), code=3)
        report = execute(Plan((Recreate(spec, "changed"),)), bad, config=cfg)
        assert not report.ok
        assert [c[0] for c in bad.calls] == ["pull", "release"]
        assert bad.observe(MANAGED)["web"].config_hash == "old"  # the old one still runs
        detail = report.failed[0].detail
        assert detail.startswith("ReleaseFailed") and "exited 3" in detail
        assert "relation users missing" in detail
        receipt = build_receipt(
            meta={"env": "production", "box": "eu-1", "reconcile_rc": 1},
            bundle={"containers": [{"name": "web", "image": "web:latest", "config_hash": "new"}]},
            report=report.to_dict(),
        )
        assert receipt["result"] == "failed"

        slow = _ReleaseClient(dict(old), code=None)
        report = execute(Plan((Recreate(spec, "changed"),)), slow, config=cfg)
        assert not report.ok and "timed out after 42s" in report.failed[0].detail
        assert [c[0] for c in slow.calls] == ["pull", "release"]

    def test_release_runs_before_create_and_canary(self) -> None:
        from bay_reconcile import CanarySwap

        spec = _spec("web", release="bin/migrate")
        client = _ReleaseClient()
        assert execute(Plan((Create(spec),)), client).ok
        assert [c[0] for c in client.calls] == ["pull", "release", "create"]

        old = {"web": ContainerState("web", exists=True, config_hash="old", status="running")}
        canary = _ReleaseClient(dict(old), code=1)
        swap = CanarySwap(_spec("web", release="x", zero_downtime=True), "c")
        report = execute(Plan((swap,)), canary)
        # A failed release never reaches the canary rescue, which would recreate.
        assert not report.ok
        assert [c[0] for c in canary.calls] == ["pull", "release"]

    def test_spec_without_release_runs_none(self) -> None:
        client = _ReleaseClient({"web": ContainerState("web", exists=True, config_hash="old")})
        execute(Plan((Recreate(_spec("web", config_hash="new"), "changed"),)), client)
        assert "release" not in [c[0] for c in client.calls]

    def test_bundle_carries_release_and_timeout(self) -> None:
        import pytest

        from bay_reconcile.bundle import load_bundle

        b = load_bundle({
            "containers": [{"name": "web", "image": "w", "type": "service", "config_hash": "h",
                            "release": "bin/migrate"}],
            "config": {"release_timeout": 30},
        })
        assert b.containers[0].release == "bin/migrate"
        assert b.config.release_timeout == 30.0
        with pytest.raises(ValueError, match="release must be a non-empty string"):
            load_bundle({"containers": [{"name": "web", "image": "w", "type": "service",
                                         "config_hash": "h", "release": " "}]})

    def test_release_is_not_hashed(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent.parent / "filter_plugins"))
        from bay_filters import bay_spec_hash

        base = {"name": "web", "image": "w", "labels": {}}
        assert bay_spec_hash(base) == bay_spec_hash({**base, "release": "bin/migrate"})
