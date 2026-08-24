"""Bundle loading + reconcile entrypoint tests."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import ContainerState, load_bundle, spec_from_dict  # noqa: E402
from bay_reconcile.__main__ import reconcile  # noqa: E402


class _Fake:
    def __init__(self, initial=None):
        self._state = dict(initial or {})
        self.calls = []
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
                health="healthy",
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
            if old in self._state:
                self._state[new] = self._state.pop(old)

    def inspect(self, name):
        with self._lock:
            return self._state.get(name) or ContainerState(name=name, exists=False)

    def pull(self, image):
        with self._lock:
            self.calls.append(("pull", image))


def _entry(**kw):
    base = {"name": "web", "image": "web:latest", "type": "service", "config_hash": "h1"}
    base.update(kw)
    return base


class TestSpecFromDict:
    def test_minimal_defaults(self):
        s = spec_from_dict(_entry())
        assert s.name == "web"
        assert s.config_hash == "h1"
        assert s.restart_policy == "unless-stopped"
        assert s.env == {} and s.volumes == () and not s.build

    def test_full_round_trip(self):
        s = spec_from_dict(
            _entry(
                env={"A": "1"},
                volumes=["/v:/v"],
                ports=["80:80"],
                zero_downtime=True,
                build=True,
                network_mode="host",
                mem_limit="256m",
            )
        )
        assert s.env == {"A": "1"}
        assert s.volumes == ("/v:/v",)
        assert s.ports == ("80:80",)
        assert s.zero_downtime and s.build
        assert s.network_mode == "host" and s.mem_limit == "256m"

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="invalid container type"):
            spec_from_dict(_entry(type="bogus"))


class TestLoadBundle:
    def test_defaults(self):
        b = load_bundle({"containers": [_entry()]})
        assert b.stack == "bay"
        assert b.managed_label == "bay.managed"
        assert len(b.containers) == 1

    def test_explicit_fields(self):
        b = load_bundle({"stack": "sandbox", "managed_label": "x.managed", "containers": []})
        assert b.stack == "sandbox"
        assert b.managed_label == "x.managed"
        assert b.containers == ()


class TestReconcileEntrypoint:
    def test_creates_missing(self):
        bundle = load_bundle({"stack": "sandbox", "containers": [_entry()]})
        client = _Fake()
        code, out = reconcile(bundle, client)
        assert code == 0
        assert out["ok"] is True
        assert ("create", "web") in client.calls
        assert out["plan"]["Create"] == 1

    def test_noop_when_matching(self):
        bundle = load_bundle({"containers": [_entry(config_hash="h1")]})
        client = _Fake({"web": ContainerState("web", exists=True, config_hash="h1", managed=True)})
        code, out = reconcile(bundle, client)
        assert code == 0
        assert out["plan"].get("NoOp") == 1
        assert client.calls == []  # nothing executed on a no-op

    def test_plan_only_does_not_mutate(self):
        bundle = load_bundle({"containers": [_entry()]})
        client = _Fake()
        code, out = reconcile(bundle, client, plan_only=True)
        assert code == 0
        assert out["plan_only"] is True
        assert client.calls == []
