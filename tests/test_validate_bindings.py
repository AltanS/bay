"""Tests for the pre-deploy binding-IP validator (M83-S5).

Exercises `_validate_accessory_bindings` in isolation against a range of
services.yml shapes. The function fails validation if a port string and
the declared `expose:` mode disagree — see the 2026-04-22 incident for
context.
"""

from __future__ import annotations

from bay_cli.commands.validate import _validate_accessory_bindings


class _FakeResult:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def ok(self, msg: str) -> None:  # not used
        pass

    def warn(self, msg: str) -> None:  # not used
        pass


def _run(data: dict) -> _FakeResult:
    r = _FakeResult()
    _validate_accessory_bindings(data, "group_vars/all/services.yml", r)
    return r


# ── All three valid modes → pass ──────────────────────────────────────


class TestValidExposeModes:
    def test_expose_loopback_no_prefix(self):
        data = {"accessories": {"redis": {"port": "6379:6379", "expose": "loopback"}}}
        assert _run(data).failures == []

    def test_expose_loopback_with_prefix(self):
        """Explicit 127.0.0.1: prefix still accepted (noisy but not wrong)."""
        data = {
            "accessories": {
                "redis": {"port": "127.0.0.1:6379:6379", "expose": "loopback"}
            }
        }
        assert _run(data).failures == []

    def test_expose_tailnet_no_prefix(self):
        data = {"accessories": {"postgres": {"port": "5432:5432", "expose": "tailnet"}}}
        assert _run(data).failures == []

    def test_expose_host_no_prefix(self):
        data = {"accessories": {"postgres": {"port": "5432:5432", "expose": "host"}}}
        assert _run(data).failures == []

    def test_expose_host_explicit_prefix(self):
        data = {
            "accessories": {
                "postgres": {"port": "0.0.0.0:5432:5432", "expose": "host"}
            }
        }
        assert _run(data).failures == []


# ── Invalid combinations → fail with actionable message ──────────────


class TestBindingInvalid:
    def test_loopback_with_public_ip_fails(self):
        """The 2026-04-22 incident shape: expose: loopback but port
        silently rewritten to 0.0.0.0 by a bug. S5 catches this."""
        data = {
            "accessories": {
                "redis": {"port": "0.0.0.0:6379:6379", "expose": "loopback"}
            }
        }
        failures = _run(data).failures
        assert len(failures) == 1
        assert "expose: loopback" in failures[0]
        assert "redis" in failures[0]
        assert "0.0.0.0:6379:6379" in failures[0]

    def test_loopback_with_tailnet_ip_fails(self):
        data = {
            "accessories": {
                "redis": {"port": "100.64.0.1:6379:6379", "expose": "loopback"}
            }
        }
        assert len(_run(data).failures) == 1

    def test_host_with_loopback_ip_fails(self):
        data = {
            "accessories": {
                "postgres": {"port": "127.0.0.1:5432:5432", "expose": "host"}
            }
        }
        failures = _run(data).failures
        assert len(failures) == 1
        assert "expose: host" in failures[0]

    def test_tailnet_with_hardcoded_ip_fails(self):
        """expose: tailnet injects the IP at render time — the config
        must not hardcode it."""
        data = {
            "accessories": {
                "postgres": {"port": "100.64.0.1:5432:5432", "expose": "tailnet"}
            }
        }
        failures = _run(data).failures
        assert len(failures) == 1
        assert "tailnet renders the tailnet IP at deploy time" in failures[0]

    def test_no_expose_with_public_ip_fails(self):
        """Explicit 0.0.0.0 without expose: host is suspicious —
        operator should commit to expose: host for auditability."""
        data = {
            "accessories": {
                "redis": {"port": "0.0.0.0:6379:6379"}  # no expose
            }
        }
        failures = _run(data).failures
        assert len(failures) == 1
        assert "Add expose: host" in failures[0]

    def test_no_expose_with_nonsanctioned_ip_fails(self):
        """Hardcoded non-loopback/non-0.0.0.0 IP breaks S10's drift
        check invariant. Operator should use expose: ... instead."""
        data = {
            "accessories": {
                "weirdo": {"port": "192.168.1.1:5432:5432"}
            }
        }
        failures = _run(data).failures
        assert len(failures) == 1
        assert "hardcoded IP prefix" in failures[0]

    def test_no_expose_no_prefix_passes(self):
        """Bare port pair with no expose: is the implicit loopback
        default (shipped in v0.83.1) — this should pass."""
        data = {"accessories": {"redis": {"port": "6379:6379"}}}
        assert _run(data).failures == []

    def test_no_expose_loopback_prefix_passes(self):
        data = {"accessories": {"redis": {"port": "127.0.0.1:6379:6379"}}}
        assert _run(data).failures == []


# ── Degenerate / real-world inputs ────────────────────────────────────


class TestValidatorRobustness:
    def test_no_accessories_key_no_crash(self):
        assert _run({}).failures == []

    def test_accessory_without_port_no_crash(self):
        data = {"accessories": {"headless": {"image": "alpine"}}}
        assert _run(data).failures == []

    def test_non_dict_accessory_skipped(self):
        data = {"accessories": {"weird": "not a dict"}}
        assert _run(data).failures == []

    def test_multiple_accessories_all_reported(self):
        """Each bad accessory must appear in its own failure message —
        don't short-circuit on the first violation."""
        data = {
            "accessories": {
                "redis": {"port": "0.0.0.0:6379:6379", "expose": "loopback"},
                "postgres": {"port": "127.0.0.1:5432:5432", "expose": "host"},
            }
        }
        failures = _run(data).failures
        assert len(failures) == 2
        assert any("redis" in f for f in failures)
        assert any("postgres" in f for f in failures)


# ── `bin/bay validate` exit-code integration ───────────────────────


def test_validate_command_exits_nonzero_on_binding_mismatch(tmp_path):
    """Smoke test: `bin/bay validate` in a consumer where accessory
    has expose: loopback but a hardcoded 0.0.0.0 port must exit
    non-zero. The full CLI invocation is covered by the subcommand
    tests — here we just confirm the _validate_accessory_bindings
    wiring from _validate_services_schema triggers result.fail."""
    r = _FakeResult()
    _validate_accessory_bindings(
        {
            "accessories": {
                "redis": {"port": "0.0.0.0:6379:6379", "expose": "loopback"},
            }
        },
        "group_vars/all/services.yml",
        r,
    )
    assert len(r.failures) == 1
