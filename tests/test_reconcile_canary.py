"""Zero-downtime canary swap tests (M85-S5) against an in-memory fake.

Covers the success sequence (create canary -> health-gate -> stop/remove old ->
rename), the no-healthcheck pass, the crash-loop rescue (fall back to standard
recreate), and transient-unhealthy tolerance (Docker-29 brief unhealthy).
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import (  # noqa: E402
    CanarySwap,
    ContainerSpec,
    ContainerState,
    Plan,
    ReconcilerConfig,
    execute,
)

FAST = ReconcilerConfig(
    canary_suffix="-new", healthcheck_timeout=0.05, healthcheck_poll=0.001, stop_timeout=1
)


class FakeDocker:
    def __init__(self, initial=None, *, health="healthy"):
        self._state = dict(initial or {})
        self.calls = []
        self._health = health
        self._lock = threading.Lock()

    def observe(self, managed_label):
        with self._lock:
            return dict(self._state)

    def create(self, spec, *, name_override=None):
        name = name_override or spec.name
        with self._lock:
            self.calls.append(("create", name))
            self._state[name] = ContainerState(
                name=name,
                exists=True,
                image=spec.image,
                config_hash=spec.config_hash,
                status="running",
                health=self._health,
                managed=True,
            )

    def stop(self, name, *, timeout=10):
        with self._lock:
            self.calls.append(("stop", name))

    def remove(self, name):
        with self._lock:
            self.calls.append(("remove", name))
            self._state.pop(name, None)

    def rename(self, old, new):
        with self._lock:
            self.calls.append(("rename", old, new))
            if old in self._state:
                self._state[new] = self._state.pop(old)

    def inspect(self, name):
        with self._lock:
            return self._state.get(name) or ContainerState(name=name, exists=False)

    def pull(self, image):
        with self._lock:
            self.calls.append(("pull", image))

    def ops(self):
        return [c[0] for c in self.calls]


def _svc(config_hash="new"):
    return ContainerSpec(
        name="web", image="web:latest", type="service", config_hash=config_hash, zero_downtime=True
    )


def _run(client):
    return execute(Plan((CanarySwap(_svc(), "changed"),)), client, config=FAST)


class TestCanary:
    def test_healthy_swap_sequence(self):
        client = FakeDocker(
            {"web": ContainerState("web", exists=True, config_hash="old", health="healthy")}
        )
        report = _run(client)
        ops = client.ops()
        assert report.ok
        assert ("create", "web-new") in client.calls
        assert ops.index("create") < ops.index("stop") < ops.index("remove") < ops.index("rename")
        state = client.observe("bay.managed")
        assert "web-new" not in state
        assert state["web"].config_hash == "new"

    def test_no_healthcheck_passes(self):
        client = FakeDocker(
            {"web": ContainerState("web", exists=True, config_hash="old")}, health=None
        )
        _run(client)
        assert ("rename", "web-new", "web") in client.calls

    def test_crash_loop_falls_back_to_recreate(self):
        class Crash(FakeDocker):
            def inspect(self, name):
                return ContainerState(name=name, exists=True, status="running", restart_count=3)

        client = Crash({"web": ContainerState("web", exists=True, config_hash="old")})
        report = _run(client)
        assert report.ok  # fallback recreate succeeds
        assert "canary fallback" in report.results[0].detail
        assert "rename" not in client.ops()  # never swapped
        state = client.observe("bay.managed")
        assert "web-new" not in state
        assert state["web"].config_hash == "new"

    def test_transient_unhealthy_then_healthy_swaps(self):
        class Flaky(FakeDocker):
            def __init__(self, initial=None):
                super().__init__(initial)
                self._reads = 0

            def inspect(self, name):
                with self._lock:
                    self._reads += 1
                    health = "unhealthy" if self._reads < 3 else "healthy"
                    return ContainerState(name=name, exists=True, status="running", health=health)

        client = Flaky({"web": ContainerState("web", exists=True, config_hash="old")})
        _run(client)
        assert ("rename", "web-new", "web") in client.calls
