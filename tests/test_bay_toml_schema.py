"""bay.toml schema v3: fixtures, file-wide rules and the `bay toml validate` command.

The fixtures under tests/fixtures/bay_toml/ come in pairs, `<rule>-valid.toml`
and `<rule>-invalid.toml`, one pair per v3 addition or trap. Each invalid
fixture is pinned to the exact set of violation paths it must produce, so a
fixture that starts failing for a different reason is caught, not counted.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from bay_cli import bay_toml
from bay_cli.cli import app

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "bay_toml"
DOC = ROOT / "docs" / "bay-toml.md"
BLIND_DOC = ROOT / "docs" / "bay-toml-blind-readers.md"

BANNED = re.compile(
    r"\b(accessor(y|ies)|rigs?|regions?|placements?|group_vars|inventor(y|ies)|playbooks?"
    r"|roles?|reconcilers?|consumers?|snapshots?|vaults?|stacks?|hosts?)\b",
    re.IGNORECASE,
)

#: Rule name -> the exact violation paths its invalid fixture produces.
PAIRS: dict[str, set[str]] = {
    "service-owns": {"services.sidecar.secrets"},
    "fleet-secrets": {"fleet_secrets.BW_CLIENTID"},
    "publish": {"needs.pigeon.extensions"},
    "deploy-branch": {"deploy.production.branch"},
    "build-watch": {"build.watch"},
    "deploy-env-image": {"deploy.production.build"},
    "health-password": {"health"},
    "duplicate-domain": {"services.admin.domain"},
    "name-env-suffix": {"name"},
    "unknown-key": {"memmory"},
    "scalar-after-table": {"access.port", "port"},
    "tailnet-mode": {"access.mode"},
    "open-paths": {"access.open[0]"},
    "needs-one-form": {"services.worker.needs"},
    "image-or-build": {"image"},
    "mounts": {"mounts[0]"},
}

#: Invalid fixtures with no valid twin (the twin is the corrected example).
SINGLE_INVALID: dict[str, set[str]] = {
    "deploy-required-invalid": {"deploy"},
}


def _paths(violations: list[bay_toml.Violation]) -> set[str]:
    return {v.path for v in violations}


# ── The two headline fixtures ────────────────────────────────────────────────

def test_corrected_example_passes():
    assert bay_toml.validate_file(FIXTURES / "corrected-example.toml") == []


def test_draft_v2_example_fails():
    violations = bay_toml.validate_file(FIXTURES / "draft-v2-example.toml")
    assert violations, "the old draft example must not validate"
    assert "not valid TOML" in violations[0].message


def test_draft_v2_differs_from_corrected_only_by_the_two_faults():
    draft = (FIXTURES / "draft-v2-example.toml").read_text()
    assert 'image = "ghcr.io/org/app:1.4"' in draft
    assert 'needs = ["postgres", "redis", "bucket"]' in draft
    assert "[build]" in draft and "[needs.postgres]" in draft


# ── Pairs ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", sorted(PAIRS))
def test_valid_fixture_passes(rule):
    assert bay_toml.validate_file(FIXTURES / f"{rule}-valid.toml") == []


@pytest.mark.parametrize("rule", sorted(PAIRS))
def test_invalid_fixture_fails_at_expected_paths(rule):
    violations = bay_toml.validate_file(FIXTURES / f"{rule}-invalid.toml")
    assert _paths(violations) == PAIRS[rule], [str(v) for v in violations]


@pytest.mark.parametrize("stem", sorted(SINGLE_INVALID))
def test_single_invalid_fixture(stem):
    violations = bay_toml.validate_file(FIXTURES / f"{stem}.toml")
    assert _paths(violations) == SINGLE_INVALID[stem]


def test_every_fixture_is_accounted_for():
    known = {"corrected-example", "draft-v2-example", *SINGLE_INVALID}
    for rule in PAIRS:
        known |= {f"{rule}-valid", f"{rule}-invalid"}
    on_disk = {p.stem for p in FIXTURES.glob("*.toml")}
    assert on_disk == known


def test_scalar_after_table_names_the_trap():
    violations = bay_toml.validate_file(FIXTURES / "scalar-after-table-invalid.toml")
    msg = next(v.message for v in violations if v.path == "access.port")
    assert "move it above the first table" in msg


def test_unknown_key_suggests_spelling():
    (v,) = bay_toml.validate_file(FIXTURES / "unknown-key-invalid.toml")
    assert "did you mean memory" in v.message


# ── File-wide rules on small documents ───────────────────────────────────────

BASE: dict[str, Any] = {
    "name": "myapp",
    "fleet": "myfleet",
    "port": 3000,
    "access": {"mode": "public"},
    "deploy": {"production": {"domain": "app.example.com"}},
}


def _doc(**patch: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            _merge(doc[key], value)
        else:
            doc[key] = value
    return doc


def _merge(into: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = v


def test_base_document_is_valid():
    assert bay_toml.validate(_doc()) == []


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        pytest.param(_doc(needs=["myapp"]), {"needs"}, id="self-need"),
        pytest.param(_doc(command="run ${FOO}"), {"command"}, id="templating"),
        pytest.param(_doc(env={"A": "{{ x }}"}), {"env.A"}, id="templating-jinja"),
        pytest.param(
            _doc(deploy={"staging": {"domain": "s.example.com",
                                     "services": {"nope": {"domain": "n.example.com"}}}}),
            {"deploy.staging.services.nope"}, id="override-unknown-service",
        ),
        pytest.param(
            _doc(deploy={"staging": {}, "qa": {}, "production": {}}),
            {"deploy.qa.domain"}, id="two-envs-default-domain",
        ),
        pytest.param(
            _doc(deploy={"staging": {"domain": "app.example.com"}}),
            {"deploy.staging.domain"}, id="same-domain-two-envs",
        ),
        pytest.param(
            _doc(deploy={"production": {"aliases": ["app.example.com"]}}),
            {"deploy.production.aliases[0]"}, id="alias-equals-domain",
        ),
        pytest.param(
            _doc(mounts=[{"path": "/data", "volume": "a"}, {"path": "/data/", "volume": "b"}]),
            {"mounts[1].path"}, id="duplicate-mount-path",
        ),
        pytest.param(
            _doc(mounts=[{"path": "/data"}]), {"mounts[0]"}, id="mount-neither-volume-nor-from",
        ),
        pytest.param(
            _doc(mounts=[{"path": "/c", "from": "c.yaml", "backup": False}]),
            {"mounts[0].backup"}, id="backup-on-from-mount",
        ),
        pytest.param(
            _doc(access={"open": ["/a"], "locked": ["/a"]}),
            {"access.locked"}, id="open-and-locked",
        ),
        pytest.param(
            _doc(image="ghcr.io/o/a:1",
                 deploy={"production": {"build": {"args": {"A": "b"}}}}),
            {"deploy.production.build"}, id="build-args-on-image-project",
        ),
        pytest.param(
            _doc(deploy={"production": {"build": {"dockerfile": "X"}}}),
            {"deploy.production.build.dockerfile"}, id="only-build-args-override",
        ),
        pytest.param(
            _doc(deploy={"production": {"access": {"password": {"users": ["admin"]}}}}),
            {"deploy.production.health"}, id="env-password-needs-health",
        ),
        pytest.param(
            {k: v for k, v in _doc(access={"mode": "internal"},
                                   deploy={"staging": {"domain": "s.example.com",
                                                       "access": {"mode": "public"}}}).items()
             if k != "port"},
            {"deploy.staging.port"}, id="env-mode-needs-port",
        ),
        pytest.param(
            _doc(services={"api": {"port": 1, "path": "/api", "access": {"mode": "internal"}}}),
            {"services.api.access.mode"}, id="routed-service-internal",
        ),
        pytest.param(
            _doc(services={"w": {"access": {"limits": {"rate": "1/s"}}}}),
            {"services.w.access"}, id="access-on-unrouted-service",
        ),
        pytest.param(
            _doc(access={"mode": "internal"}, services={"api": {"port": 1, "path": "/api"}}),
            {"services.api.path"}, id="path-on-internal-project",
        ),
        pytest.param(
            _doc(services={"web": {}}), {"services.web"}, id="service-named-web",
        ),
        pytest.param(
            _doc(deploy={"staging": {"domain": "s.example.com"}},
                 services={"staging-api": {}}),
            {"services.staging-api"}, id="service-starts-with-env",
        ),
        pytest.param(
            _doc(services={"api": {"health": "/h"}}), {"services.api.health"},
            id="service-health-needs-port",
        ),
        pytest.param(
            _doc(services={"api": {"port": 1, "domain": "api.example.com",
                                   "access": {"password": {"users": ["admin"]}}}}),
            {"services.api.health"}, id="service-password-needs-health",
        ),
        pytest.param(
            _doc(jobs=[{"name": "a", "schedule": "0 2 * * *", "command": "x"},
                       {"name": "a", "schedule": "0 3 * * *", "command": "y"}]),
            {"jobs[1].name"}, id="duplicate-job",
        ),
        pytest.param(
            _doc(env={"TOKEN": "x"}, fleet_secrets={"TOKEN": "TOKEN"}),
            {"fleet_secrets.TOKEN"}, id="env-and-fleet-secret",
        ),
        pytest.param(_doc(logs="7m"), {"logs"}, id="logs-unit"),
        pytest.param(_doc(memory="512"), {"memory"}, id="size-unit"),
        pytest.param(_doc(access={"limits": {"rate": "100/h"}}), {"access.limits.rate"},
                     id="rate-unit"),
        pytest.param(_doc(update="false"), {"update"}, id="update-enum"),
        pytest.param(_doc(services={"b": {"port": 1, "expose": "host"}}),
                     {"services.b.expose"}, id="expose-enum"),
        pytest.param({k: v for k, v in _doc().items() if k != "access"}, {"access"},
                     id="access-required"),
        pytest.param(_doc(access={"identity": {}}), {"access.identity.header"},
                     id="identity-header-required"),
    ],
)
def test_file_rule(doc, expected):
    violations = bay_toml.validate(doc)
    assert _paths(violations) == expected, [str(v) for v in violations]


def test_needs_list_and_table_in_services_only():
    doc = _doc(services={"a": {"needs": ["redis"]}, "b": {"needs": {"redis": {}}}})
    assert _paths(bay_toml.validate(doc)) == {"services.b.needs"}


def test_postgres_options_only_on_postgres():
    ok = _doc(needs={"postgres": {"database": "x_prod", "role": "x", "extensions": ["vector"]}})
    assert bay_toml.validate(ok) == []
    bad = _doc(needs={"redis": {"database": "x"}})
    assert _paths(bay_toml.validate(bad)) == {"needs.redis.database"}


# ── Schema hygiene ───────────────────────────────────────────────────────────

def _walk(node: Any, where: str = "#"):
    if isinstance(node, dict):
        yield where, node
        for k, v in node.items():
            yield from _walk(v, f"{where}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{where}/{i}")


def test_schema_is_valid_draft_2020_12():
    schema = bay_toml.load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_every_table_closes_its_keys():
    """Every object schema must say what else is allowed: false, or a value schema."""
    for where, node in _walk(bay_toml.load_schema()):
        types = node.get("type")
        is_object = types == "object" or (isinstance(types, list) and "object" in types)
        if is_object:
            assert "additionalProperties" in node, f"{where} leaves unknown keys open"


def test_x_messages_name_real_keywords():
    """An x-messages entry for a keyword the subschema lacks is dead text."""
    for where, node in _walk(bay_toml.load_schema()):
        for keyword in node.get("x-messages", {}):
            assert keyword in node, f"{where}: x-messages.{keyword} has no {keyword}"


# ── Wording ──────────────────────────────────────────────────────────────────

def _all_messages() -> list[str]:
    msgs = []
    for _, node in _walk(bay_toml.load_schema()):
        msgs.extend(node.get("x-messages", {}).values())
    for f in FIXTURES.glob("*.toml"):
        msgs.extend(v.message for v in bay_toml.validate_file(f))
    return msgs


def test_messages_use_no_banned_words():
    hits = [m for m in _all_messages() if BANNED.search(m)]
    assert hits == []


def _prose(text: str) -> str:
    """Docs prose with code removed: keys like `role` are names, not words."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text)


@pytest.mark.parametrize("doc", [DOC, BLIND_DOC], ids=lambda p: p.name)
def test_docs_use_no_banned_words(doc):
    hits = [m.group(0) for m in BANNED.finditer(_prose(doc.read_text()))]
    assert hits == []


def test_docs_example_is_the_corrected_fixture():
    text = DOC.read_text()
    match = re.search(r"<!-- corrected-example -->\n```toml\n(.*?)```", text, re.DOTALL)
    assert match, "docs/bay-toml.md must carry the corrected example under its marker"
    assert match.group(1) == (FIXTURES / "corrected-example.toml").read_text()


def test_blind_readers_doc_records_a_result():
    assert re.search(r"^Result: ", BLIND_DOC.read_text(), re.MULTILINE)


# ── CLI ──────────────────────────────────────────────────────────────────────

runner = CliRunner()


def test_cli_valid_file_is_silent():
    result = runner.invoke(app, ["toml", "validate", str(FIXTURES / "corrected-example.toml")])
    assert result.exit_code == 0
    assert result.output == ""


def test_cli_invalid_file_prints_one_line_per_violation():
    path = FIXTURES / "scalar-after-table-invalid.toml"
    result = runner.invoke(app, ["toml", "validate", str(path)])
    assert result.exit_code == 1
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("access.port: ")
    assert lines[1].startswith("port: ")


def test_cli_parse_error_names_the_file():
    path = FIXTURES / "draft-v2-example.toml"
    result = runner.invoke(app, ["toml", "validate", str(path)])
    assert result.exit_code == 1
    assert result.output.startswith(f"{path}: the file is not valid TOML")


def test_cli_missing_file():
    result = runner.invoke(app, ["toml", "validate", "/nonexistent/bay.toml"])
    assert result.exit_code == 1
    assert result.output.strip() == "/nonexistent/bay.toml: file not found"


@pytest.mark.parametrize(
    ("fixture", "ok"),
    [("corrected-example.toml", True), ("unknown-key-invalid.toml", False)],
)
def test_cli_json(fixture, ok):
    result = runner.invoke(app, ["toml", "validate", "--json", str(FIXTURES / fixture)])
    assert result.exit_code == (0 if ok else 1)
    payload = json.loads(result.output)
    assert payload["ok"] is ok
    if ok:
        assert payload["violations"] == []
    else:
        assert payload["violations"] == [
            {"path": "memmory", "message": "unknown key; did you mean memory?"}
        ]
