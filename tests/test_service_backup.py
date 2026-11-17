"""[services.<x>.backup] (2.24.0): a database backup of a project's internal service.

The table compiles to the accessory's `backup:` block, which the backup role
reads (roles/backup/tasks/resolve.yml). Only an internal service compiles to
an accessory, so a routed service with a backup is refused. A change of only
the backup block is a `backup` plan step, never a container update.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from bay_cli import bay_toml, compiler
from bay_cli import plan as planmod
from bay_cli.fleet import load_inputs

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "fleet_min"
SHOP = Path("checkouts") / "shop" / "bay.toml"


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    dest = tmp_path / "fleet"
    shutil.copytree(FIXTURE, dest)
    return dest


def edit(root: Path, rel: Path, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text()
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1))


def compiled(root: Path) -> dict:
    result = compiler.compile_fleet(
        load_inputs(root, checkouts={"shop": root / "checkouts" / "shop"})
    )
    return yaml.safe_load(result.body())


def _with_backup(root: Path, table: str) -> None:
    edit(root, SHOP, '[services.warmer]\n', f"[services.warmer.backup]\n{table}\n[services.warmer]\n")


def test_the_table_compiles_to_the_accessory_backup(fleet):
    _with_backup(fleet, 'method = "pg_dump"\ndatabases = ["shop", "shop_jobs"]\nmax_age_hours = 26\n')
    out = compiled(fleet)
    entry = out["accessories"]["shop-warmer"]["backup"]
    # The fleet [defaults.backup] (keep = "30d", hour = 3) applies, in UTC.
    assert entry == {
        "method": "pg_dump",
        "format": "custom",
        "schedule": "0 3 * * *",
        "utc": True,
        "retain": 30,
        "databases": ["shop", "shop_jobs"],
        "max_age_hours": 26,
        "clients": ["shop", "shop-api"],
    }


def test_the_table_wins_over_the_project_backup_policy(fleet):
    _with_backup(fleet, 'method = "pg_dump"\nhour = 1\nkeep = "2w"\nformat = "plain"\n')
    edit(fleet, SHOP, "[deploy.production]", '[backup]\nkeep = "7d"\nhour = 22\n\n[deploy.production]')
    entry = compiled(fleet)["accessories"]["shop-warmer"]["backup"]
    assert entry["schedule"] == "0 1 * * *"
    assert entry["retain"] == 14
    assert entry["format"] == "plain"


def test_no_table_no_backup(fleet):
    assert "backup" not in compiled(fleet)["accessories"]["shop-warmer"]


def _doc(service: dict) -> dict:
    return {"name": "shop", "fleet": "f", "image": "x:1", "services": {"db": service},
            "deploy": {"production": {"box": "b"}}}


def _paths(doc: dict) -> list[str]:
    return [f"{v.path}: {v.message}" for v in bay_toml.validate(doc)]


def test_validator_accepts_an_internal_service_backup():
    doc = _doc({"image": "postgres:17", "backup": {"method": "pg_dump", "databases": ["app"]}})
    assert not [p for p in _paths(doc) if "backup" in p]


@pytest.mark.parametrize("key, value", [("domain", "db.example.com"), ("path", "/db")])
def test_validator_refuses_a_routed_service_backup(key, value):
    doc = _doc({"image": "postgres:17", key: value, "port": 5432, "backup": {"method": "pg_dump"}})
    assert any("services.db.backup" in p and "routed" in p for p in _paths(doc))


@pytest.mark.parametrize("table, word", [
    ({"method": "mysql"}, "pg_dump"),
    ({}, "method"),
    ({"method": "pg_dump", "format": "tar"}, "custom"),
    ({"method": "pg_dump", "databases": ["Bad-Name"]}, "lowercase"),
    ({"method": "pg_dump", "max_age_hours": 0}, "max_age_hours"),
    ({"method": "pg_dump", "when": "now"}, "when"),
])
def test_validator_refuses_a_bad_table(table, word):
    doc = _doc({"image": "postgres:17", "backup": table})
    assert any("backup" in p and word in p for p in _paths(doc)), _paths(doc)


# ── plan ────────────────────────────────────────────────────────────────────


def _services(backup: dict | None) -> dict:
    acc = {"image": "postgres:17", "memory": "1g"}
    if backup is not None:
        acc["backup"] = backup
    return {"services": {"shop": {"image": "shop:1"}}, "accessories": {"shop-postgres": acc}}


def _steps(old, new):
    return planmod.diff_steps(
        _services(old), _services(new), project="shop",
        mine={"shop", "shop-postgres"}, resources=set(), running={"shop", "shop-postgres"},
    )


def test_a_new_database_backup_is_a_backup_step_not_a_container_update():
    steps = _steps(None, {"method": "pg_dump", "format": "custom"})
    assert [(s["kind"], s["action"], s["risk"]) for s in steps] == [("backup", "update", "safe")]
    assert "new database backup of shop-postgres (pg_dump)" in steps[0]["reason"]
    assert steps[0]["project"] == "shop"


def test_a_changed_and_a_removed_database_backup():
    changed = _steps({"method": "pg_dump", "schedule": "0 3 * * *"},
                     {"method": "pg_dump", "schedule": "0 1 * * *"})
    assert [s["kind"] for s in changed] == ["backup"]
    assert "schedule" in changed[0]["reason"]
    removed = _steps({"method": "pg_dump"}, None)
    assert [s["kind"] for s in removed] == ["backup"]
    assert "removes its timer" in removed[0]["reason"]


def test_a_backup_change_with_another_change_still_updates_the_container():
    old, new = _services({"method": "pg_dump"}), _services(None)
    new["accessories"]["shop-postgres"]["memory"] = "2g"
    steps = planmod.diff_steps(old, new, project="shop", mine={"shop", "shop-postgres"},
                               resources=set(), running={"shop-postgres"})
    assert sorted(s["kind"] for s in steps) == ["backup", "container"]
