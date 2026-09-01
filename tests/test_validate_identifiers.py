"""`bay validate` must refuse a name it would later paste into SQL or a shell.

Layer two (quoting in the templates) is tested by tests/test_hostile_render.py.
This is layer one: the door. Two doors, in fact — the JSON schema, which is
what `bin/bay validate` and the pre-deploy gate actually run, and
`_validate_identifier_safety`, which restates the same contract in a message
an operator can act on and adds the one rule a regex cannot express: a bare
`/` in `public_routes` is valid syntax and makes an entire `access: vpn`
service public.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import yaml

from bay_cli.commands.validate import ValidationResult, _validate_identifier_safety

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _REPO_ROOT / "src" / "bay_cli" / "schemas" / "services.schema.json"


class _JsonMode:
    """Suppress Rich output while a check runs."""

    def __enter__(self):
        from bay_cli.console.output import set_json_mode

        set_json_mode(True)
        return self

    def __exit__(self, *args):
        from bay_cli.console.output import set_json_mode

        set_json_mode(False)


def _schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _check(data: dict) -> ValidationResult:
    result = ValidationResult()
    with _JsonMode():
        _validate_identifier_safety(data, result)
    return result


def _service(**overrides) -> dict:
    svc = {
        "access": "public",
        "image": "nginx:latest",
        "domains": ["app.example.com"],
        "ports": {"internal": 8080},
    }
    svc.update(overrides)
    return svc


# ── The four cases named in the spec ─────────────────────────────────────


_HOSTILE = {
    "service name": {"services": {"app`id`": _service()}},
    "database name": {
        "services": {
            "app": _service(
                database={"accessory": "postgres", "name": "x; DROP DATABASE y"}
            )
        }
    },
    "database user": {
        "services": {
            "app": _service(
                database={"accessory": "postgres", "user": "x'; DROP ROLE y; --"}
            )
        }
    },
    "env key": {
        "services": {"app": _service(env={"clear": {"FOO BAR": "1"}})}
    },
    "bare public route": {
        "services": {"app": _service(access="vpn", public_routes=["/"])}
    },
    "route with a backtick": {
        "services": {"app": _service(access="vpn", public_routes=["/`id`"])}
    },
    "accessory name": {"accessories": {"pg$(id)": {"image": "postgres:16"}}},
    "secret env key": {
        "services": {"app": _service(env={"secret": ["NOT A NAME"]})}
    },
}


@pytest.mark.parametrize("label", sorted(_HOSTILE))
def test_validate_fails_on_hostile_identifier(label):
    result = _check(_HOSTILE[label])
    assert result.total_issues >= 1, f"{label} was accepted"


def test_bare_slash_message_explains_the_consequence():
    """The whole point of the extra check is the explanation."""
    result = _check(_HOSTILE["bare public route"])
    joined = " ".join(result.failed)
    assert "public" in joined and "access: public" in joined
    assert "app" in joined


def test_a_conforming_config_passes():
    data = {
        "services": {
            "internal-stats": _service(
                access="vpn",
                public_routes=["/webhooks/", "/api/health", "/static/*"],
                vpn_routes=["/admin"],
                env={"clear": {"NODE_ENV": "production"}, "secret": ["DB_PASSWORD"]},
                database={"accessory": "postgres", "name": "stats", "user": "stats"},
            )
        },
        "accessories": {"postgres": {"image": "postgres:16"}},
    }
    result = _check(data)
    assert result.failed == []
    assert result.passed


def test_yaml_anchor_scratch_keys_are_ignored():
    """`_anchors` is a documented reserved key, not a service."""
    data = {"services": {"_anchors": {}, "app": _service()}}
    assert _check(data).failed == []


# ── The schema says the same thing ───────────────────────────────────────


@pytest.mark.parametrize(
    "doc",
    [
        {"services": {"app`id`": _service()}},
        {"accessories": {"pg$(id)": {"image": "postgres:16"}}},
        {
            "services": {
                "app": _service(
                    database={"accessory": "postgres", "name": "x; DROP DATABASE y"}
                )
            }
        },
        {"services": {"app": _service(env={"clear": {"FOO BAR": "1"}})}},
        {"services": {"app": _service(access="vpn", public_routes=["/`id`"])}},
        {"services": {"app": _service(access="vpn", public_routes=["/a\nb"])}},
        {"services": {"app": _service(vpn_routes=['/a";id;"'])}},
    ],
    ids=[
        "service-name",
        "accessory-name",
        "database-name",
        "env-key",
        "route-backtick",
        "route-newline",
        "route-quote",
    ],
)
def test_schema_rejects_hostile_identifiers(doc):
    validator = jsonschema.Draft202012Validator(_schema())
    assert list(validator.iter_errors(doc)), "schema accepted a hostile identifier"


def test_schema_still_accepts_the_shipped_example():
    """The example ships known healthcheck errors; this spec must add none."""
    example = _REPO_ROOT / "example" / "group_vars" / "all" / "services.yml"
    doc = yaml.safe_load(example.read_text())
    validator = jsonschema.Draft202012Validator(_schema())
    paths = {
        tuple(str(p) for p in err.absolute_path)
        for err in validator.iter_errors(doc)
    }
    # Pre-existing and unrelated: healthcheck.path is not in healthcheck_block.
    assert all("healthcheck" in p for p in paths), sorted(paths)


def test_route_pattern_admits_the_shapes_operators_actually_use():
    import re

    pattern = _schema()["$defs"]["service"]["properties"]["public_routes"]["items"][
        "pattern"
    ]
    for good in ("/", "/health", "/api/v1/health", "/webhooks/", "/static/*", "/a-b_c.d"):
        assert re.match(pattern, good), good
    for bad in ("/`id`", "/$(id)", '/a"b', "/a'b", "/a;b", "/a\\b", "/a\nb", "health"):
        assert not re.match(pattern, bad), bad
