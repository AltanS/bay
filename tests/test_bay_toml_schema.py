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
    "postgres-adopted": {"needs.postgres.database", "needs.postgres.role"},
    "job-memory": {"jobs[0].memory"},
    "service-runtime": {
        "services.worker.replicas",
        "services.worker.zero_downtime",
        "services.worker.update",
        "services.worker.logs",
    },
    "identity-public": {"access.identity.header"},
    "sibling-url": {"services.api"},
}

#: Invalid fixtures with no valid twin (the twin is the corrected example).
SINGLE_INVALID: dict[str, set[str]] = {
    "deploy-required-invalid": {"deploy"},
}

#: Valid fixtures that document a behavior and have no invalid twin.
SINGLE_VALID = {"shared-volume-valid"}


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


@pytest.mark.parametrize("stem", sorted(SINGLE_VALID))
def test_single_valid_fixture(stem):
    assert bay_toml.validate_file(FIXTURES / f"{stem}.toml") == []


@pytest.mark.parametrize("stem", sorted(SINGLE_INVALID))
def test_single_invalid_fixture(stem):
    violations = bay_toml.validate_file(FIXTURES / f"{stem}.toml")
    assert _paths(violations) == SINGLE_INVALID[stem]


def test_every_fixture_is_accounted_for():
    known = {"corrected-example", "draft-v2-example", *SINGLE_INVALID, *SINGLE_VALID}
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


def test_build_repo_is_a_non_empty_string():
    doc = _doc()
    doc.pop("image", None)
    assert bay_toml.validate({**doc, "build": {"repo": "https://github.com/acme/app.git"}}) == []
    for bad in ("", 3):
        found = bay_toml.validate({**doc, "build": {"repo": bad}})
        assert {v.path for v in found} == {"build.repo"}, found


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
        pytest.param(_doc(services={"b": {"port": 1, "expose": "0.0.0.0"}}),
                     {"services.b.expose"}, id="expose-never-all-interfaces"),
        pytest.param(_doc(services={"b": {"port": 1, "expose": 8080}}),
                     {"services.b.expose"}, id="expose-names-no-box-port"),
        pytest.param(_doc(expose="host"), {"expose"}, id="top-expose-enum"),
        pytest.param({**{k: v for k, v in _doc().items() if k != "port"},
                      "access": {"mode": "internal"}, "expose": "loopback"},
                     {"port"}, id="top-expose-needs-port"),
        pytest.param(_doc(log_rotation={"max_size": "10", "max_file": 2}),
                     {"log_rotation.max_size"}, id="log-rotation-size"),
        pytest.param(_doc(services={"w": {"log_rotation": {"max_size": "10m"}}}),
                     {"services.w.log_rotation.max_file"}, id="log-rotation-needs-max-file"),
        pytest.param(_doc(services={"job-cleanup": {}}), {"services.job-cleanup"},
                     id="service-starts-with-job"),
        pytest.param(_doc(mounts=[{"path": "/d", "volume": "production-data"}]),
                     {"mounts[0].volume"}, id="volume-starts-with-env"),
        pytest.param(_doc(services={"w": {"mounts": [{"path": "/d", "volume": "production-d"}]}}),
                     {"services.w.mounts[0].volume"}, id="service-volume-starts-with-env"),
        pytest.param(_doc(jobs=[{"name": "a", "schedule": "0 2 * * *", "command": "x",
                                 "memory": "0m"}]),
                     {"jobs[0].memory"}, id="job-memory-size"),
        pytest.param(_doc(services={"w": {"replicas": 0}}), {"services.w.replicas"},
                     id="service-replicas-minimum"),
        pytest.param({k: v for k, v in _doc().items() if k != "access"}, {"access"},
                     id="access-required"),
        pytest.param(_doc(access={"identity": {}}), {"access.identity.header"},
                     id="identity-header-required"),
    ],
)
def test_file_rule(doc, expected):
    violations = bay_toml.validate(doc)
    assert _paths(violations) == expected, [str(v) for v in violations]


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        pytest.param(
            _doc(secrets=["API_URL"], services={"api": {"port": 4000, "path": "/api"}}),
            {"services.api"}, id="sibling-var-in-secrets",
        ),
        pytest.param(
            _doc(fleet_secrets={"API_URL": "SHARED"},
                 services={"api": {"port": 4000, "path": "/api"}}),
            {"services.api"}, id="sibling-var-in-fleet-secrets",
        ),
        pytest.param(
            _doc(needs={"api": {}}, services={"api": {"port": 4000, "path": "/api"}}),
            {"services.api"}, id="sibling-var-is-a-need-name",
        ),
        pytest.param(
            _doc(needs={"postgres": {"env": "API_URL"}},
                 services={"api": {"port": 4000, "path": "/api"}}),
            {"services.api"}, id="sibling-var-is-a-need-alias",
        ),
        pytest.param(
            _doc(services={"worker": {"env": {"WEB_URL": "x"}}}),
            {"services.worker"}, id="web-url-in-a-service",
        ),
        pytest.param(
            _doc(needs={"api": {"env": "API_BASE"}},
                 services={"api": {"port": 4000, "path": "/api"}}),
            set(), id="need-env-replaces-the-need-name",
        ),
        pytest.param(
            _doc(deploy={"production": {"env": {"API_URL": "x"}}},
                 services={"api": {"port": 4000, "path": "/api"}}),
            {"services.api"}, id="sibling-var-in-deploy-env",
        ),
        pytest.param(
            _doc(services={"api": {"port": 4000, "path": "/api", "env": {"API_URL": "x"}}}),
            set(), id="own-var-is-not-injected-into-itself",
        ),
        pytest.param(
            _doc(services={"api": {"port": 4000, "path": "/api"},
                           "side": {"inherit": False, "image": "x:1",
                                    "env": {"API_URL": "x"}}}),
            set(), id="inherit-false-receives-no-sibling-vars",
        ),
    ],
)
def test_sibling_url_collisions(doc, expected):
    violations = bay_toml.validate(doc)
    assert _paths(violations) == expected, [str(v) for v in violations]


def test_needs_list_and_table_in_services_only():
    doc = _doc(services={"a": {"needs": ["redis"]}, "b": {"needs": {"redis": {}}}})
    assert _paths(bay_toml.validate(doc)) == {"services.b.needs"}


def test_postgres_options_only_on_postgres():
    ok = _doc(needs={"postgres": {"env": "GF_DATABASE_URL", "extensions": ["vector"]}})
    assert bay_toml.validate(ok) == []
    bad = _doc(needs={"redis": {"extensions": ["x"]}})
    assert _paths(bay_toml.validate(bad)) == {"needs.redis.extensions"}


@pytest.mark.parametrize("key", ["database", "role"])
def test_postgres_never_names_adopted_data(key):
    """Adopted names are per environment and live in the lockfile, so bay.toml rejects them."""
    doc = _doc(needs={"postgres": {key: "myapp_prod"}})
    (v,) = bay_toml.validate(doc)
    assert v.path == f"needs.postgres.{key}"
    assert "lockfile" in v.message


def test_service_may_set_runtime_keys():
    doc = _doc(services={"w": {"replicas": 3, "zero_downtime": True, "update": "auto",
                               "logs": "off"}})
    assert bay_toml.validate(doc) == []


def test_job_may_set_memory():
    job = {"name": "a", "schedule": "0 2 * * *", "command": "x", "memory": "1g"}
    assert bay_toml.validate(_doc(jobs=[job])) == []


def test_identity_is_valid_in_public_mode():
    doc = _doc(access={"mode": "public", "identity": {"header": "X-Tailnet-Device"}})
    assert bay_toml.validate(doc) == []


def test_track_schema_and_default():
    """Spec M117/05: `[deploy.<env>] track` is branch or pin, branch when unset."""
    assert bay_toml.track(_doc(), "production") == "branch" == bay_toml.DEFAULT_TRACK
    for mode in ("branch", "pin"):
        doc = _doc(deploy={"production": {"track": mode}})
        assert bay_toml.validate(doc) == []
        assert bay_toml.track(doc, "production") == mode
    assert bay_toml.track(_doc(), "staging") == "branch", "an unknown env is branch too"
    bad = _doc(deploy={"production": {"track": "nightly"}})
    assert _paths(bay_toml.validate(bad)) == {"deploy.production.track"}
    assert _paths(bay_toml.validate(_doc(deploy={"production": {"track": True}}))) == {
        "deploy.production.track"
    }


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
    text = BLIND_DOC.read_text()
    assert re.search(r"^Result: ", text, re.MULTILINE)
    assert "Result: pending" not in text


#: One phrase per ruling from the blind-reader run. Each must stay in the reference doc.
RULING_PHRASES = [
    "bay.toml never names an adopted database, user or volume",
    "Bay probes the health path directly on the container, not through the route",
    "bay.toml never names a box port",
    "never\n  `0.0.0.0`",
    "`WEB_URL`",
    "A service with `inherit = false` receives no such variable",
    "`locked` paths also need the password",
    "A job gets the image, env, secrets, `fleet_secrets`, needs and mounts",
    "A job may set `memory`",
    "A service inherits `update` and `logs` from the project",
    "`[access.identity]` is valid in `public` mode",
    "strips\n  the header on all others",
    "An alias redirect is HTTP 308 and keeps the path and the query",
    "Bay issues a certificate for every alias",
    "`<name>-<env>-<volume>`",
    "removes the new container and starts the\n  previous container again",
    "`fleet_secrets` values are shared across environments by default",
    "An `open` path stays open in every environment",
    "A service with an inline `build` inherits nothing from the project `[build]`",
    "A service that inherits the image shares the one build",
    "A failed `release` aborts the deploy before traffic moves",
    "inside a one-shot container of the new\n  image",
    "`health` for a service with a `port` defaults to `/`",
]


@pytest.mark.parametrize("phrase", RULING_PHRASES)
def test_doc_states_each_ruling(phrase):
    assert phrase in DOC.read_text()


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


def test_validate_rejects_access_on_internal():
    """``access`` keys other than ``mode`` on an internal main container are errors (M118/04)."""
    password = {"users": ["admin"]}
    # The top-level mode is internal: every other top-level key is named.
    doc = _doc(access={"mode": "internal", "password": password, "open": ["/hook"]})
    doc.pop("port")
    found = {v.path: v.message for v in bay_toml.validate(doc)}
    assert set(found) >= {"access.password", "access.open"}
    assert "internal" in found["access.password"] and "deploy.production" in found["access.password"]
    # Internal in one env only: the top-level key and the env key are named once each.
    doc = _doc(
        access={"mode": "public", "limits": {"rate": "10/s"}},
        deploy={"staging": {"domain": "s.example.com",
                            "access": {"mode": "internal", "locked": ["/admin"]}}},
    )
    paths = _paths(bay_toml.validate(doc))
    assert {"access.limits", "deploy.staging.access.locked"} <= paths
    msg = {v.path: v.message for v in bay_toml.validate(doc)}["access.limits"]
    assert "deploy.staging.access.mode is internal" in msg
    # Only mode, or a routed main container: nothing to report.
    doc = _doc(access={"mode": "internal"})
    doc.pop("port")
    assert not any(p.startswith(("access.", "deploy.production.access"))
                   for p in _paths(bay_toml.validate(doc)))
    assert bay_toml.validate(_doc(access={"mode": "public", "password": password},
                                  health="/hz")) == []


# ── release with update = "auto" ────────────────────────────────────────────

def _release_doc(**top: Any) -> dict[str, Any]:
    """A valid file with a release; ``top`` adds or overrides top-level keys."""
    doc: dict[str, Any] = {
        "name": "shop", "fleet": "main", "image": "shop:1", "port": 3000,
        "release": "bin/migrate", "access": {"mode": "public"},
        "deploy": {"production": {"domain": "a.example.com"}},
    }
    doc.update(top)
    return doc


def test_release_with_auto_update_is_an_error() -> None:
    assert not bay_toml.validate(_release_doc())  # the default update is notify
    for update in ("notify", "off"):
        assert not bay_toml.validate(_release_doc(update=update))
    found = bay_toml.validate(_release_doc(update="auto"))
    assert [v.path for v in found] == ["update"]
    assert "without running the release" in found[0].message
    # No release, no problem.
    no_release = _release_doc(update="auto")
    del no_release["release"]
    assert not bay_toml.validate(no_release)


def test_release_and_auto_update_are_checked_per_environment() -> None:
    envs = {"production": {"domain": "a.example.com", "update": "off"},
            "staging": {"domain": "b.example.com"}}
    # The top level says auto; production turns it off, staging keeps it: an error for staging only.
    found = bay_toml.validate(_release_doc(update="auto", deploy=envs))
    assert [v.path for v in found] == ["update"]
    assert "staging" in found[0].message and "production" not in found[0].message
    # An environment sets both.
    doc = _release_doc(deploy={"production": {
        "domain": "a.example.com", "update": "auto", "release": "bin/migrate"}})
    del doc["release"]
    assert [v.path for v in bay_toml.validate(doc)] == ["deploy.production.update"]
    # A service's own update does not count: only the main container has a release.
    doc = _release_doc(services={"worker": {"command": "run", "update": "auto"}})
    assert not bay_toml.validate(doc)


# ── path routes ─────────────────────────────────────────────────────────────

def _path_doc(**paths: str) -> dict[str, Any]:
    doc = _release_doc()
    del doc["release"]
    doc["services"] = {name: {"command": "run", "port": 3001, "path": path}
                       for name, path in paths.items()}
    return doc


def test_a_root_path_is_an_error() -> None:
    assert not bay_toml.validate(_path_doc(api="/api"))
    found = bay_toml.validate(_path_doc(api="/"))
    assert [v.path for v in found] == ["services.api.path"]
    assert "every request" in found[0].message
    assert [v.path for v in bay_toml.validate(_path_doc(api="//"))] == ["services.api.path"]


def test_paths_that_differ_by_a_trailing_slash_collide() -> None:
    found = bay_toml.validate(_path_doc(api="/api", api2="/api/"))
    assert [v.path for v in found] == ["services.api2.path"]
    assert "same route as services.api.path" in found[0].message
    assert not bay_toml.validate(_path_doc(api="/api", apiary="/apiary/"))


# ── command forms and the job timeout ───────────────────────────────────────

def test_release_and_job_command_accept_a_string_or_a_list():
    job = {"name": "a", "schedule": "0 2 * * *"}
    for value in ("bin/migrate up", ["bin/migrate", "up"], ["bin/migrate"], ["bin/x", ""]):
        assert bay_toml.validate(_doc(release=value)) == [], value
        assert bay_toml.validate(_doc(jobs=[{**job, "command": value}])) == [], value


@pytest.mark.parametrize("value", ["", [], [""], ["", "x"], [1], ["x", 2], {"a": "b"}, 7])
def test_release_and_job_command_reject_other_shapes(value):
    job = {"name": "a", "schedule": "0 2 * * *"}
    assert _paths(bay_toml.validate(_doc(release=value))) == {"release"}, value
    assert _paths(bay_toml.validate(_doc(jobs=[{**job, "command": value}]))) == {"jobs[0].command"}, value


def test_job_timeout_is_a_positive_integer():
    job = {"name": "a", "schedule": "0 2 * * *", "command": "x"}
    assert bay_toml.validate(_doc(jobs=[{**job, "timeout": 90}])) == []
    for bad in (0, -5, "90", 1.5):
        assert _paths(bay_toml.validate(_doc(jobs=[{**job, "timeout": bad}]))) == {"jobs[0].timeout"}, bad
