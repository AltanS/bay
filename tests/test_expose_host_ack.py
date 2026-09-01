"""`expose: host` requires an explicit `expose_host_ack: true`.

A host-published port renders `0.0.0.0:<port>`, which Docker DNATs in
PREROUTING. It therefore never reaches the nftables `input` chain, where
the CrowdSec bouncer set lives, and Bay deliberately manages no
DOCKER-USER chain. The bypass is legitimate for some workloads but must
be a recorded act, so `_validate_expose_host_ack` fails without the
sibling boolean.

Covers both shapes (accessory `expose:` and service `ports.expose:`),
both directions (missing ack fails, present ack passes), and that no
other expose mode needs an ack. Also asserts the schema accepts the new
key in both objects — both are `additionalProperties: false`.
"""

from __future__ import annotations

from bay_cli.commands.validate import _load_schema, _validate_expose_host_ack


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
    _validate_expose_host_ack(data, "group_vars/all/services.yml", r)
    return r


# ── accessory shape ────────────────────────────────────────────────────


class TestAccessory:
    def test_expose_host_without_ack_fails(self):
        data = {"accessories": {"postgres": {"port": "5432:5432", "expose": "host"}}}
        failures = _run(data).failures
        assert len(failures) == 1
        assert "postgres" in failures[0]
        assert "expose_host_ack" in failures[0]

    def test_failure_names_the_bypass(self):
        data = {"accessories": {"postgres": {"port": "5432:5432", "expose": "host"}}}
        msg = _run(data).failures[0]
        assert "nftables" in msg
        assert "CrowdSec" in msg

    def test_expose_host_with_ack_passes(self):
        data = {
            "accessories": {
                "postgres": {
                    "port": "5432:5432",
                    "expose": "host",
                    "expose_host_ack": True,
                }
            }
        }
        assert _run(data).failures == []

    def test_ack_false_still_fails(self):
        data = {
            "accessories": {
                "postgres": {
                    "port": "5432:5432",
                    "expose": "host",
                    "expose_host_ack": False,
                }
            }
        }
        assert len(_run(data).failures) == 1

    def test_other_expose_modes_need_no_ack(self):
        for mode in ("loopback", "gateway", "tailnet"):
            data = {"accessories": {"redis": {"port": "6379:6379", "expose": mode}}}
            assert _run(data).failures == [], mode

    def test_no_expose_needs_no_ack(self):
        data = {"accessories": {"redis": {"port": "6379:6379"}}}
        assert _run(data).failures == []


# ── service ports shape ────────────────────────────────────────────────


class TestServicePorts:
    def test_ports_expose_host_without_ack_fails(self):
        data = {"services": {"api": {"ports": {"internal": 8080, "expose": "host"}}}}
        failures = _run(data).failures
        assert len(failures) == 1
        assert "api" in failures[0]
        assert "ports.expose_host_ack" in failures[0]

    def test_ports_expose_host_with_ack_passes(self):
        data = {
            "services": {
                "api": {
                    "ports": {
                        "internal": 8080,
                        "expose": "host",
                        "expose_host_ack": True,
                    }
                }
            }
        }
        assert _run(data).failures == []

    def test_ports_expose_loopback_needs_no_ack(self):
        data = {"services": {"api": {"ports": {"internal": 8080, "expose": "loopback"}}}}
        assert _run(data).failures == []

    def test_service_without_ports_is_ignored(self):
        data = {"services": {"api": {"image": "nginx"}}}
        assert _run(data).failures == []


class TestBothAtOnce:
    def test_two_offenders_two_failures(self):
        data = {
            "accessories": {"postgres": {"port": "5432:5432", "expose": "host"}},
            "services": {"api": {"ports": {"internal": 8080, "expose": "host"}}},
        }
        assert len(_run(data).failures) == 2


# ── schema accepts the key (both objects are additionalProperties: false) ──


class TestSchemaAcceptsAck:
    def _validate(self, doc: dict) -> list:
        from jsonschema import Draft202012Validator

        return list(Draft202012Validator(_load_schema()).iter_errors(doc))

    def test_accessory_object_accepts_expose_host_ack(self):
        doc = {
            "services": {},
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "port": "5432:5432",
                    "expose": "host",
                    "expose_host_ack": True,
                }
            },
        }
        assert self._validate(doc) == []

    def test_ports_block_accepts_expose_host_ack(self):
        doc = {
            "services": {
                "api": {
                    "image": "nginx",
                    "domains": ["api.example.com"],
                    "access": "public",
                    "ports": {
                        "internal": 8080,
                        "expose": "host",
                        "expose_host_ack": True,
                    },
                }
            }
        }
        assert self._validate(doc) == []

    def test_ack_must_be_boolean(self):
        doc = {
            "services": {},
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "port": "5432:5432",
                    "expose": "host",
                    "expose_host_ack": "yes",
                }
            },
        }
        assert self._validate(doc) != []
