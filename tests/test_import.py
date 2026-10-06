"""`bay import`: today's YAML fleet -> bay.fleet.toml, bay.toml files and lockfiles.

The fixture is tests/fixtures/fleet_legacy: two boxes (eu, na), shared
postgres, a cache and a backup sidecar, and services that hit every grouping
rule (environment suffix with and without a stem, an accepted and a refused
extra service) and the main mapping rows (needs on the same box and across
boxes, a need on a shared container, fleet_secrets aliases, a plain-text
password, templated domains, an adopted config directory, log rotation).

Every test imports a copy in tmp_path. The round-trip test at the bottom runs
the same gate as tests/test_roundtrip_real_fleets.py, so the importer, the
compiler and the comparison have CI coverage without the real fleets.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from bay_cli import compiler, importer, roundtrip
from bay_cli.cli import app
from bay_cli.fleet import load_inputs

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_legacy"
SECRETS = Path("group_vars") / "production" / "secrets.yml"

BANNED = re.compile(
    r"\b(accessor(y|ies)|rigs?|regions?|placements?|group_vars|inventor(y|ies)|playbooks?"
    r"|roles?|reconcilers?|consumers?|snapshots?|vaults?|stacks?|hosts?)\b",
    re.IGNORECASE,
)

#: The differences the fixture keeps on purpose, so the gate is seen to catch them.
FIXTURE_EXCEPTIONS = (
    roundtrip.Exception_("fleet-a", "mailer", "memswap_limit", "swap-off ships in 2.x"),
    roundtrip.Exception_("fleet-a", "blog-prod", "log_retention", "compress has no bay.toml key"),
)

runner = CliRunner()


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    dest = tmp_path / "legacy"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory) -> tuple[importer.ImportResult, Path]:
    out = tmp_path_factory.mktemp("imported") / "fleet"
    result = importer.import_fleet(FIXTURE, "fleet-a")
    result.write(out)
    return result, out


def toml(out: Path, rel: str) -> dict[str, Any]:
    return tomllib.loads((out / rel).read_text())


def lock(out: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((out / "projects" / f"{name}.lock").read_text())
    return data


def _state(root: Path) -> dict[str, tuple[int, int, int]]:
    """Size, mode and mtime of every file, read with stat only (never opened)."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mode, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── read-only, and the files it writes ──────────────────────────────────────


def test_importer_does_not_modify_source(legacy: Path, tmp_path: Path) -> None:
    secrets = legacy / SECRETS
    secrets.chmod(0)  # opening it would raise: the importer must never read it
    try:
        before = _state(legacy)
        out = tmp_path / "out"
        result = runner.invoke(
            app, ["import", "--fleet", str(legacy), "--out", str(out), "--name", "fleet-a"]
        )
        assert result.exit_code == 0, result.output
        assert _state(legacy) == before
        assert (out / "bay.fleet.toml").is_file()
        assert not any(p.name == "secrets.yml" for p in out.rglob("*"))
    finally:
        secrets.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_import_refuses_a_non_empty_or_nested_out(legacy: Path, tmp_path: Path) -> None:
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    refused = runner.invoke(app, ["import", "--fleet", str(legacy), "--out", str(busy)])
    assert refused.exit_code != 0 and not (busy / "bay.fleet.toml").exists()
    nested = runner.invoke(app, ["import", "--fleet", str(legacy), "--out", str(legacy / "new")])
    assert nested.exit_code != 0 and not (legacy / "new").exists()


def test_import_writes_the_fleet_layout(imported: tuple[importer.ImportResult, Path]) -> None:
    result, out = imported
    assert result.projects == [
        "app",
        "app-docs",
        "blog",
        "mailer",
        "news",
        "shop",
        "site",
        "status",
    ]
    assert result.resources == ["cache", "postgres", "shop-backup"]
    for name in result.projects:
        assert (out / "projects" / name / "bay.toml").is_file()
        assert lock(out, name)["commit"] is None and lock(out, name)["local_path"] is None
    assert sorted(str(p.relative_to(out)) for p in (out / "files").rglob("*") if p.is_file()) == [
        "files/legal-site/beta/de/imprint.md",
        "files/legal-site/beta/terms.md",
        "files/status/config.yaml",
    ]


def test_import_compiles_without_unsupported(imported: tuple[importer.ImportResult, Path]) -> None:
    _, out = imported
    assert compiler.compile_fleet(load_inputs(out)).unsupported == []


def test_toml_writer_round_trips(imported: tuple[importer.ImportResult, Path]) -> None:
    result, _ = imported
    for rel, text in result.files.items():
        if rel.endswith(".toml"):
            assert importer.to_toml(tomllib.loads(text)) == text, rel


# ── grouping ────────────────────────────────────────────────────────────────


def test_grouping_decisions_are_reported(imported: tuple[importer.ImportResult, Path]) -> None:
    result, _ = imported
    assert result.groupings == [
        "blog-prod: environment production of project blog (container name adopted)",
        "shop-staging: environment staging of project shop (container name adopted)",
        "app-api: [services.api] of project app",
        "app-docs: kept as its own project, not [services.docs] of app: app would get "
        "DOCS_URL, which it does not read today",
    ]


def test_environment_suffix_becomes_a_deploy_table(
    imported: tuple[importer.ImportResult, Path],
) -> None:
    _, out = imported
    shop = toml(out, "projects/shop/bay.toml")
    assert sorted(shop["deploy"]) == ["production", "staging"]
    staging = shop["deploy"]["staging"]
    assert staging["branch"] == "develop" and staging["domain"] == "staging.shop.example.com"
    assert staging["access"] == {"mode": "tailnet", "locked": []}
    assert shop["env"] == {"NODE_ENV": "production", "REPORT_URL": "http://mailer:3601/api/report"}
    assert staging["env"] == {"LOG_LEVEL": "debug", "APP_URL": "https://staging.shop.example.com"}
    assert shop["deploy"]["production"]["aliases"] == ["www.shop.example.com"]
    assert shop["deploy"]["production"]["redirect"] is False
    blog = toml(out, "projects/blog/bay.toml")
    assert list(blog["deploy"]) == ["production"]


def test_extra_service_takes_the_sibling_url(imported: tuple[importer.ImportResult, Path]) -> None:
    _, out = imported
    app_doc = toml(out, "projects/app/bay.toml")
    assert "env" not in app_doc, "API_URL is injected, not declared"
    api = app_doc["services"]["api"]
    assert api["inherit"] is False and api["domain"] == "api.app.example.com"
    assert api["update"] == "notify", "today's own value, not the project's auto"
    assert api["fleet_secrets"] == {"API_TOKEN": "APP_API_API_TOKEN"}
    assert lock(out, "app")["envs"]["production"]["adopted"]["containers"] == {
        "web": "app",
        "api": "app-api",
    }


# ── mapping rows ────────────────────────────────────────────────────────────


def test_importer_lockfile_adopts_load_bearing_names(
    imported: tuple[importer.ImportResult, Path],
) -> None:
    _, out = imported
    shop = lock(out, "shop")
    assert shop["repo"] == "git@github.com:acme/shop.git"
    prod, staging = shop["envs"]["production"], shop["envs"]["staging"]
    assert prod["box"] == "eu"
    assert prod["adopted"] == {
        "containers": {"web": "shop"},
        "database": "shop_prod",
        "images": {"web": "registry.example.com/fleeta/shop:latest"},
        "role": "shop_app",
        "volumes": {"data": "shop_data"},
    }
    assert staging["adopted"]["containers"] == {"web": "shop-staging"}
    assert staging["adopted"]["volumes"] == {"data": "shop_staging_data"}
    assert (staging["adopted"]["database"], staging["adopted"]["role"]) == (
        "shop_staging",
        "shop_staging",
    )
    assert lock(out, "blog")["envs"]["production"]["adopted"]["containers"] == {"web": "blog-prod"}
    assert lock(out, "blog")["envs"]["production"]["adopted"]["volumes"] == {
        "content": "blog_prod_content"
    }
    assert lock(out, "news")["envs"]["production"]["adopted"]["database"] == "news"
    assert lock(out, "site")["envs"]["production"]["adopted"]["files"] == {
        "legal-site/beta": "legal-site/beta"
    }


def test_needs_and_publish(imported: tuple[importer.ImportResult, Path]) -> None:
    result, out = imported
    assert ("news", "mailer", "MAIL_BASE_URL") in result.needs_pairs, (
        "across boxes, by tailnet address"
    )
    assert ("shop", "mailer", "MAIL_BASE_URL") in result.needs_pairs, "same box, by container name"
    assert ("shop", "cache", "CACHE_HOST_URL") in result.needs_pairs, "a shared container"
    assert toml(out, "projects/mailer/bay.toml")["publish"] is True
    assert toml(out, "projects/mailer/bay.toml")["expose"] == "tailnet"
    assert toml(out, "projects/news/bay.toml")["needs"]["mailer"] == {"env": "MAIL_BASE_URL"}
    assert toml(out, "bay.fleet.toml")["resources"]["cache"]["port"] == 6379
    assert toml(out, "bay.fleet.toml")["boxes"]["eu"] == {
        "env": "production",
        "group": "eu",
        "tailnet_ip": "100.64.0.1",
    }


def test_secrets_and_aliases(imported: tuple[importer.ImportResult, Path]) -> None:
    result, out = imported
    assert result.fleet_secret_aliases == 2
    news = toml(out, "projects/news/bay.toml")
    assert news["secrets"] == ["SESSION_SECRET"]
    assert news["fleet_secrets"] == {"TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN"}
    status = toml(out, "projects/status/bay.toml")
    assert status["access"]["password"] == {"users": ["ops"]}
    assert status["access"]["limits"] == {"rate": "20/s", "burst": 40}
    assert status["health"] == "none"
    assert [m.name for m in result.secret_moves] == ["STATUS_PASSWORD_OPS"]
    assert result.secret_moves[0].source[0] == "literal"
    fleet = toml(out, "bay.fleet.toml")
    assert fleet["repo_tokens"] == {"git@github.com:acme/": "ACME_GIT_TOKEN"}
    assert fleet["webhook"] == {
        "domain": "deploy.eu.fleet-a.example.com",
        "secret": "WEBHOOK_SECRET",
    }
    # The webhook domain differs per box: the default box has none of its own,
    # the other box carries its domain.
    boxes = fleet["boxes"]
    assert "webhook_domain" not in boxes["eu"]
    assert boxes["na"]["webhook_domain"] == "deploy.na.fleet-a.example.com"


def test_report_never_prints_a_plain_value(imported: tuple[importer.ImportResult, Path]) -> None:
    result, out = imported
    text = "\n".join(result.report_lines())
    assert "example-password" not in text
    assert all("example-password" not in p.read_text() for p in out.rglob("*") if p.is_file())


def test_templates_resolve_per_box(imported: tuple[importer.ImportResult, Path]) -> None:
    _, out = imported
    assert (
        toml(out, "projects/status/bay.toml")["deploy"]["production"]["domain"]
        == "status.na.fleet-a.example.com"
    )


def test_flags(imported: tuple[importer.ImportResult, Path]) -> None:
    result, _ = imported
    flags = "\n".join(result.flags)
    assert "services.mailer.memswap_limit" in flags
    assert "services.shop.env.clear.REPORT_URL" in flags
    assert "services.blog-prod.log_retention: compress" in flags
    assert "webhook.domain: differs per box" not in flags
    assert "API_URL" not in flags


def test_report_uses_no_banned_words(imported: tuple[importer.ImportResult, Path]) -> None:
    result, _ = imported
    hits = [line for line in result.report_lines() if BANNED.search(line)]
    assert hits == []


# ── the round trip, on the fixture ──────────────────────────────────────────


@pytest.fixture(scope="module")
def gate(tmp_path_factory: pytest.TempPathFactory) -> roundtrip.GateResult:
    return roundtrip.run_gate(FIXTURE, name="fleet-a", workdir=tmp_path_factory.mktemp("gate"))


def test_roundtrip_fixture_finds_only_the_known_differences(gate: roundtrip.GateResult) -> None:
    assert gate.unsupported == []
    assert {(d.container, tuple(d.keys)) for d in gate.diffs} == {
        ("mailer", ("memswap_limit",)),
        ("blog-prod", ("log_retention",)),
    }, "\n".join(d.diff for d in gate.diffs)
    assert gate.containers >= 15


def test_roundtrip_fixture_with_exceptions_is_identical(gate: roundtrip.GateResult) -> None:
    assert gate.renders is not None
    diffs, _, _, excepted = roundtrip.compare("fleet-a", *gate.renders, FIXTURE_EXCEPTIONS)
    assert diffs == []
    assert sorted(excepted) == [
        ("eu", "blog-prod", "log_retention"),
        ("eu", "mailer", "memswap_limit"),
    ]


def test_roundtrip_gate_catches_an_injected_difference(tmp_path: Path, legacy: Path) -> None:
    """Change today's file after the import: the gate must report that container."""
    services = legacy / "group_vars" / "all" / "services.yml"
    data = yaml.safe_load(services.read_text())
    imported = importer.import_fleet(legacy, "fleet-a")
    out = tmp_path / "fleet"
    imported.write(out)
    compiled = yaml.safe_load(compiler.compile_fleet(load_inputs(out)).body())
    data["services"]["news"]["env"]["clear"]["MAIL_BASE_URL"] = "http://100.64.0.1:3602"
    boxes = roundtrip._box_inputs(importer.load_legacy(legacy), imported)
    names = roundtrip._secret_names(
        [data["services"], data["accessories"], compiled["services"]], services.read_text()
    )
    secrets = {n: roundtrip.placeholder(n) for n in names}
    secrets["STATUS_PASSWORD_OPS"] = "example-password"
    original = roundtrip.render(json.loads(json.dumps(data)), boxes, secrets, tmp_path / "a")
    after = roundtrip.render(compiled, boxes, secrets, tmp_path / "b")
    diffs, _, _, _ = roundtrip.compare("fleet-a", original, after, FIXTURE_EXCEPTIONS)
    assert [(d.box, d.container, d.keys) for d in diffs] == [("na", "news", ["env"])]
    assert "3602" in diffs[0].diff


def test_import_check_prints_the_diff_summary(legacy: Path) -> None:
    before = _state(legacy)
    result = runner.invoke(app, ["import", "--fleet", str(legacy), "--check", "--name", "fleet-a"])
    assert result.exit_code == 1, result.output
    assert "DIFF mailer on box eu: memswap_limit" in result.output
    assert "DIFF blog-prod on box eu: log_retention" in result.output
    assert _state(legacy) == before, "--check writes nothing into the fleet"


# ── environment names follow the box group ──────────────────────────────────


def test_one_group_fleet_sets_primary_env(imported: tuple[importer.ImportResult, Path]) -> None:
    _, out = imported
    # every fixture box is in group production
    assert toml(out, "bay.fleet.toml")["primary_env"] == "production"


def _small_fleet(root: Path, hosts: str, services: dict[str, Any], groups: list[str]) -> Path:
    (root / "group_vars" / "all").mkdir(parents=True)
    (root / "group_vars" / "all" / "main.yml").write_text("---\nstack_name: small\n")
    (root / "group_vars" / "all" / "services.yml").write_text(
        yaml.safe_dump({"services": services}, sort_keys=False)
    )
    for g in groups:
        (root / "group_vars" / g).mkdir(parents=True, exist_ok=True)
        (root / "group_vars" / g / "main.yml").write_text("---\n{}\n")
    (root / "group_vars" / groups[0] / "secrets.yml").write_text("---\nsecrets: {}\n")
    (root / "hosts").mkdir()
    (root / "hosts" / "boxes").write_text(hosts)
    return root


def _svc(port: int, domain: str | None = None, **kw: Any) -> dict[str, Any]:
    return {
        "image": "registry.example.com/small/app:1",
        "domains": [domain or f"app{port}.example.com"],
        "ports": {"internal": port},
        "healthcheck_path": "/health",
        **kw,
    }


def test_single_box_in_group_testing_gives_deploy_testing(tmp_path: Path) -> None:
    legacy = _small_fleet(
        tmp_path / "legacy",
        "[testing]\n192.0.2.20\n\n[other]\n192.0.2.21\n",
        {"app": _svc(3000), "app-staging": _svc(3000, "s.example.com"), "tool": _svc(3002)},
        ["testing"],
    )
    result = importer.import_fleet(legacy, "small")
    out = tmp_path / "fleet"
    result.write(out)
    fleet = toml(out, "bay.fleet.toml")
    assert fleet["primary_env"] == "testing"
    assert fleet["boxes"] == {"testing": {"env": "testing"}}
    assert sorted(toml(out, "projects/app/bay.toml")["deploy"]) == ["staging", "testing"]
    assert list(toml(out, "projects/tool/bay.toml")["deploy"]) == ["testing"]
    assert sorted(lock(out, "app")["envs"]) == ["staging", "testing"]
    assert lock(out, "app")["envs"]["testing"]["adopted"]["containers"] == {"web": "app"}
    compiled = compiler.compile_fleet(load_inputs(out))
    assert sorted(compiled.services) == ["app", "app-staging", "tool"]
    gate = roundtrip.run_gate(legacy, name="small", workdir=tmp_path / "gate")
    assert gate.ok, "\n".join(gate.summary_lines())


def test_multi_group_fleet_names_each_env_after_its_box(tmp_path: Path) -> None:
    hosts = (
        "[eu]\n192.0.2.11\n\n[na]\n192.0.2.12\n\n"
        "[production:children]\neu\n\n[canary:children]\nna\n"
    )
    legacy = _small_fleet(
        tmp_path / "legacy",
        hosts,
        {
            "app": _svc(3000, regions=["eu"]),
            "app-staging": _svc(3000, "s.example.com", regions=["eu"]),
            "tool": _svc(3002, regions=["na"]),
            "edge": _svc(3003, regions=["na"]),
            "edge-staging": _svc(3003, "es.example.com", regions=["na"]),
        },
        ["production", "canary", "eu", "na"],
    )
    result = importer.import_fleet(legacy, "small")
    out = tmp_path / "fleet"
    result.write(out)
    fleet = toml(out, "bay.fleet.toml")
    assert "primary_env" not in fleet  # two groups: the default stays
    assert {b: d["env"] for b, d in fleet["boxes"].items()} == {"eu": "production", "na": "canary"}
    assert sorted(toml(out, "projects/app/bay.toml")["deploy"]) == ["production", "staging"]
    assert list(toml(out, "projects/tool/bay.toml")["deploy"]) == ["canary"]
    assert sorted(toml(out, "projects/edge/bay.toml")["deploy"]) == ["canary", "staging"]
    assert lock(out, "tool")["envs"]["canary"]["adopted"]["containers"] == {"web": "tool"}
    compiled = compiler.compile_fleet(load_inputs(out))
    assert sorted(compiled.services) == ["app", "app-staging", "edge", "edge-staging", "tool"]
    gate = roundtrip.run_gate(legacy, name="small", workdir=tmp_path / "gate")
    assert gate.ok, "\n".join(gate.summary_lines())


def test_env_name_collision_keeps_the_plain_names(tmp_path: Path) -> None:
    legacy = _small_fleet(
        tmp_path / "legacy",
        "[staging]\n192.0.2.30\n",
        {"app": _svc(3000), "app-staging": _svc(3000, "s.example.com")},
        ["staging"],
    )
    result = importer.import_fleet(legacy, "small")
    out = tmp_path / "fleet"
    result.write(out)
    assert sorted(toml(out, "projects/app/bay.toml")["deploy"]) == ["production", "staging"]
    assert any("is also the name of another environment" in f for f in result.flags)
    gate = roundtrip.run_gate(legacy, name="small", workdir=tmp_path / "gate")
    assert gate.ok, "\n".join(gate.summary_lines())
