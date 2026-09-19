"""Zero-downtime canary swap tests against an in-memory fake.

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


class _SlowCanaryDocker(FakeDocker):
    """Holds each canary briefly and counts how many are alive at once."""

    def __init__(self, initial=None):
        super().__init__(initial)
        self.live_canaries = 0
        self.max_live_canaries = 0

    def create(self, spec, *, name_override=None):
        super().create(spec, name_override=name_override)
        if name_override and name_override.endswith("-new"):
            with self._lock:
                self.live_canaries += 1
                self.max_live_canaries = max(self.max_live_canaries, self.live_canaries)

    def inspect(self, name):
        if name.endswith("-new"):
            import time

            time.sleep(0.05)
        return super().inspect(name)

    def rename(self, old, new):
        super().rename(old, new)
        if old.endswith("-new"):
            with self._lock:
                self.live_canaries -= 1


def _running_old(name):
    return ContainerState(
        name=name, exists=True, image=f"{name}:old", config_hash="old",
        status="running", health="healthy", managed=True,
    )


def _named_svc(name):
    return ContainerSpec(
        name=name, image=f"{name}:latest", type="service", config_hash="new", zero_downtime=True
    )


class TestCanariesRunOneAtATime:
    def test_three_canaries_never_overlap(self) -> None:
        names = ["web", "api", "worker"]
        client = _SlowCanaryDocker({n: _running_old(n) for n in names})
        swaps = tuple(CanarySwap(_named_svc(n), "changed") for n in names)
        report = execute(Plan(swaps), client, config=FAST)

        assert [r.status for r in report.results] == ["done", "done", "done"]
        # The old executor ran all three in one pool: every canary was created
        # before the first one was renamed into place, and this read 3.
        assert client.max_live_canaries == 1

    def test_results_keep_plan_order(self) -> None:
        from bay_reconcile import Recreate

        client = _SlowCanaryDocker({n: _running_old(n) for n in ("a", "b", "c")})
        actions = (
            CanarySwap(_named_svc("a"), "changed"),
            Recreate(_named_svc("b"), "changed"),
            CanarySwap(_named_svc("c"), "changed"),
        )
        report = execute(Plan(actions), client, config=FAST)

        assert [r.action.spec.name for r in report.results] == ["a", "b", "c"]
