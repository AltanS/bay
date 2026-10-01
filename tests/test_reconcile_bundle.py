"""Bundle loading + reconcile entrypoint tests."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bay_reconcile import (  # noqa: E402
    ContainerState,
    ReconcilerConfig,
    load_bundle,
    spec_from_dict,
)
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


class TestBundleConfig:
    """The optional `config` object carries the reconciler's tunables."""

    def test_absent_keeps_the_defaults(self):
        assert load_bundle({"containers": []}).config == ReconcilerConfig()

    def test_present_values_are_used(self):
        b = load_bundle(
            {
                "config": {
                    "stop_timeout": 45,
                    "healthcheck_timeout": 90,
                    "healthcheck_poll": 0.5,
                }
            }
        )
        assert b.config.stop_timeout == 45
        assert b.config.healthcheck_timeout == 90.0
        assert isinstance(b.config.healthcheck_timeout, float)
        assert b.config.healthcheck_poll == 0.5

    def test_a_partial_object_keeps_the_other_defaults(self):
        b = load_bundle({"config": {"stop_timeout": 5}})
        assert b.config == ReconcilerConfig(stop_timeout=5)

    def test_unknown_key_is_an_error(self):
        with pytest.raises(ValueError, match="stop_timout"):
            load_bundle({"config": {"stop_timout": 5}})

    def test_canary_suffix_is_not_a_bundle_key(self):
        with pytest.raises(ValueError, match="canary_suffix"):
            load_bundle({"config": {"canary_suffix": "-x"}})

    @pytest.mark.parametrize("key", ["stop_timeout", "healthcheck_timeout", "healthcheck_poll"])
    @pytest.mark.parametrize("bad", [0, -1, -0.5, True, False, "30", None, float("nan")])
    def test_bad_value_is_an_error_naming_the_key(self, key, bad):
        with pytest.raises(ValueError, match=key):
            load_bundle({"config": {key: bad}})

    def test_stop_timeout_must_be_whole(self):
        with pytest.raises(ValueError, match="stop_timeout"):
            load_bundle({"config": {"stop_timeout": 2.5}})

    def test_config_must_be_an_object(self):
        with pytest.raises(ValueError, match="config"):
            load_bundle({"config": [1, 2]})


class TestMainPassesConfigToExecute:
    def test_execute_receives_the_bundle_config(self, monkeypatch, tmp_path):
        import json

        import bay_reconcile.__main__ as entry
        import bay_reconcile.sdk_client as sdk

        seen = {}

        class _Report:
            ok = True

            def to_dict(self):
                return {"ok": True}

        def fake_execute(the_plan, client, *, config=None, **kw):
            seen["config"] = config
            return _Report()

        monkeypatch.setattr(entry, "execute", fake_execute)
        monkeypatch.setattr(sdk, "SdkDockerClient", lambda **kw: _Fake())
        path = tmp_path / "bundle.json"
        path.write_text(
            json.dumps(
                {
                    "containers": [_entry()],
                    "config": {
                        "stop_timeout": 77,
                        "healthcheck_timeout": 33.0,
                        "healthcheck_poll": 2.0,
                    },
                }
            ),
            encoding="utf-8",
        )
        assert entry.main([str(path)]) == 0
        assert seen["config"] == ReconcilerConfig(
            stop_timeout=77, healthcheck_timeout=33.0, healthcheck_poll=2.0
        )

    def test_a_bad_config_exits_before_docker_is_touched(self, monkeypatch, tmp_path, capsys):
        import json

        import bay_reconcile.__main__ as entry
        import bay_reconcile.sdk_client as sdk

        def boom(**kw):
            raise AssertionError("client built for a bad bundle")

        monkeypatch.setattr(sdk, "SdkDockerClient", boom)
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps({"config": {"stop_timeout": 0}}), encoding="utf-8")
        assert entry.main([str(path)]) == 2
        assert "stop_timeout" in capsys.readouterr().out
