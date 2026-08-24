"""Schema + validate.yml tests for `log_retention`.

Two surfaces are exercised:

1. **JSON schema** — `log_retention` block on service/accessory objects.
   Uses `Draft202012Validator` so the test name matches the S2 checklist
   (`days_zero`, `invalid_mode`, `invalid_size`, `valid`).

2. **validate.yml logic** — driver-compatibility and disk-budget checks
   are Ansible tasks, but the Jinja expressions that drive the `when:`
   gates are exercised here through pure-Python helpers that mirror
   them exactly. If the validate.yml expressions ever diverge from
   these helpers, a production deploy would pass a test that should
   fail — keep them in sync.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)

VALIDATE_YML_PATH = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "tasks"
    / "validate.yml"
)

DEFAULTS_PATH = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "deploy_stack"
    / "defaults"
    / "main.yml"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _service_with(retention: dict) -> dict:
    """Wrap a log_retention block in a minimal-valid service payload."""
    return {
        "services": {
            "web": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["web.example.com"],
                "ports": {"internal": 8080},
                "log_retention": retention,
            }
        }
    }


def _accessory_with(retention: dict) -> dict:
    return {
        "accessories": {
            "postgres": {
                "image": "postgres:17",
                "port": "127.0.0.1:5432:5432",
                "log_retention": retention,
            }
        }
    }


# ── schema declaration ────────────────────────────────────────────────


def test_log_retention_declared_on_service():
    schema = _load_schema()
    assert "log_retention" in schema["$defs"]["service"]["properties"]


def test_log_retention_declared_on_accessory():
    schema = _load_schema()
    assert "log_retention" in schema["$defs"]["accessory"]["properties"]


def test_log_retention_def_exists():
    schema = _load_schema()
    assert "log_retention" in schema["$defs"]
    block = schema["$defs"]["log_retention"]
    assert block["type"] == "object"
    assert block["additionalProperties"] is False
    props = block["properties"]
    assert set(props.keys()) == {"days", "max_total_size", "compress", "mode"}


def test_schema_missing_from_service_is_valid():
    """A service without any log_retention block must still pass validation.
    The feature is opt-in — S8 calls out this case explicitly so nobody
    accidentally flips it to required.
    """
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = {
        "services": {
            "web": {
                "image": "nginx:latest",
                "access": "public",
                "domains": ["web.example.com"],
                "ports": {"internal": 8080},
            }
        }
    }
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert errors == [], f"unexpected errors: {errors}"


# ── happy-path ────────────────────────────────────────────────────────


def test_valid_log_retention_block_accepts():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({
        "days": 7,
        "max_total_size": "2g",
        "compress": True,
        "mode": "normal",
    })
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert errors == [], f"unexpected errors: {errors}"


def test_valid_sensitive_mode_accepts():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"days": 30, "mode": "sensitive"})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert errors == [], f"unexpected errors: {errors}"


def test_valid_on_accessory_accepts():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _accessory_with({"days": 14, "max_total_size": "500m"})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert errors == [], f"unexpected errors: {errors}"


# ── rejection tests (names match S2 checklist -k filters) ────────────


def test_days_zero_rejected():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"days": 0})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert any("0" in e.message or "minimum" in e.message for e in errors), (
        f"expected minimum:1 rejection, got: {[e.message for e in errors]}"
    )


def test_days_negative_rejected():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"days": -1})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert len(errors) > 0


def test_invalid_mode_rejected():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"mode": "paranoid"})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert len(errors) > 0, "mode must be in enum [normal, sensitive]"


def test_invalid_size_rejected():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"max_total_size": "two gigs"})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert len(errors) > 0, "max_total_size pattern must reject free text"


def test_valid_size_suffixes_accepted():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    for size in ("500m", "500M", "2g", "2G", "1024k", "1024K", "1024"):
        data = _service_with({"max_total_size": size})
        errors = list(Draft202012Validator(schema).iter_errors(data))
        assert errors == [], f"{size!r} should be accepted, got: {errors}"


def test_unknown_key_rejected():
    from jsonschema import Draft202012Validator

    schema = _load_schema()
    data = _service_with({"days": 7, "encrypt": True})
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert any("encrypt" in e.message or "additional" in e.message.lower() for e in errors), (
        f"unknown key must be rejected, got: {[e.message for e in errors]}"
    )


# ── validate.yml surface — driver_none compatibility ─────────────────
#
# The validate.yml `Validate log_retention driver compatibility` task
# resolves the effective log driver from `log_rotation.driver` or
# `log_rotation_defaults.driver`, then fails when that driver is not in
# {json-file, local}. The logic below mirrors the Jinja expression.


def _resolved_driver(container: dict, rotation_defaults: dict) -> str:
    rotation = container.get("log_rotation")
    if isinstance(rotation, dict) and rotation.get("driver") is not None:
        return rotation["driver"]
    return rotation_defaults["driver"]


def _driver_compatible(driver: str) -> bool:
    return driver in ("json-file", "local")


def test_driver_none_flagged_as_incompatible():
    """A service with `log_rotation.driver: none` + `log_retention` is caught
    by validate.yml. Archival cannot read from `docker logs` when the driver
    discards output."""
    container = {
        "log_rotation": {"driver": "none"},
        "log_retention": {"days": 7},
    }
    defaults = {"driver": "json-file"}
    driver = _resolved_driver(container, defaults)
    assert driver == "none"
    assert not _driver_compatible(driver)


def test_driver_none_inherited_from_defaults_flagged():
    """If `log_rotation_defaults.driver` is switched to `none` at the
    consumer level, any service with `log_retention` and no local
    override inherits it and must still be flagged."""
    container = {"log_retention": {"days": 7}}
    defaults = {"driver": "none"}
    driver = _resolved_driver(container, defaults)
    assert driver == "none"
    assert not _driver_compatible(driver)


def test_driver_json_file_passes():
    container = {"log_retention": {"days": 7}}
    defaults = {"driver": "json-file"}
    assert _driver_compatible(_resolved_driver(container, defaults))


def test_driver_local_passes():
    container = {
        "log_rotation": {"driver": "local"},
        "log_retention": {"days": 7},
    }
    defaults = {"driver": "json-file"}
    assert _driver_compatible(_resolved_driver(container, defaults))


# ── validate.yml surface — budget check ──────────────────────────────


_SIZE_RE = re.compile(r"^(?P<num>[0-9]+)(?P<unit>[kKmMgG]?)$")
_UNIT_MULTIPLIER = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _human_to_bytes(size: str) -> int:
    """Mirror of Ansible's `human_to_bytes` for the patterns the schema allows."""
    m = _SIZE_RE.match(size)
    assert m, f"invalid size literal: {size!r}"
    return int(m["num"]) * _UNIT_MULTIPLIER[m["unit"].lower()]


def _over_budget(
    containers: dict,
    disk_bytes: int,
    budget_fraction: float,
) -> list[str]:
    """Mirror of the 'Assert log_retention disk budget fits' task. Returns
    the list of container names whose `max_total_size` pushes the total
    over `disk_bytes * budget_fraction`. Empty list = within budget."""
    retention_users = [
        (name, cfg["log_retention"])
        for name, cfg in containers.items()
        if isinstance(cfg.get("log_retention"), dict)
        and "max_total_size" in cfg["log_retention"]
    ]
    total = sum(_human_to_bytes(cfg["max_total_size"]) for _, cfg in retention_users)
    ceiling = int(disk_bytes * budget_fraction)
    if total > ceiling:
        return [name for name, _ in retention_users]
    return []


def test_budget_fits_within_fraction():
    """Sum 1.5g < 30% of 10g (3g) — no containers flagged."""
    containers = {
        "web": {"log_retention": {"days": 7, "max_total_size": "1g"}},
        "api": {"log_retention": {"days": 7, "max_total_size": "500m"}},
    }
    assert _over_budget(containers, 10 * 1024**3, 0.30) == []


def test_budget_exceeds_fraction():
    """Sum 5g > 30% of 10g (3g) — all retention users flagged."""
    containers = {
        "web": {"log_retention": {"days": 7, "max_total_size": "3g"}},
        "api": {"log_retention": {"days": 7, "max_total_size": "2g"}},
    }
    flagged = _over_budget(containers, 10 * 1024**3, 0.30)
    assert set(flagged) == {"web", "api"}


def test_budget_ignores_containers_without_max_total_size():
    """A container with log_retention but no max_total_size is not counted
    (it's opting into archival without a ceiling — S4 will still prune by
    days). The budget check only sees explicitly-sized containers."""
    containers = {
        "web": {"log_retention": {"days": 7}},
        "api": {"log_retention": {"days": 7, "max_total_size": "500m"}},
    }
    assert _over_budget(containers, 10 * 1024**3, 0.30) == []


def test_budget_at_exact_fraction_passes():
    """A sum equal to the ceiling should NOT trip the > comparison."""
    containers = {
        "web": {"log_retention": {"days": 7, "max_total_size": "3g"}},
    }
    # 3g exactly = 30% of 10g
    assert _over_budget(containers, 10 * 1024**3, 0.30) == []


# ── validate.yml file-level assertions ───────────────────────────────


def test_validate_yml_has_log_retention_block():
    src = VALIDATE_YML_PATH.read_text()
    assert "log_retention schema validation" in src, (
        "validate.yml must have a log_retention section with a header comment"
    )
    # Every fail task should exist
    assert "Validate log_retention shape" in src
    assert "Validate log_retention keys are recognized" in src
    assert "Validate log_retention days" in src
    assert "Validate log_retention max_total_size" in src
    assert "Validate log_retention mode" in src
    assert "Validate log_retention driver compatibility" in src
    assert "Assert log_retention disk budget" in src


def test_validate_yml_references_json_file_and_local():
    src = VALIDATE_YML_PATH.read_text()
    assert "'json-file', 'local'" in src, (
        "driver compatibility assertion must list exactly the two supported drivers"
    )


def test_defaults_declares_budget_fraction():
    src = DEFAULTS_PATH.read_text()
    assert "log_retention_budget_fraction: 0.30" in src
    assert "log_retention_disk_bytes:" in src
