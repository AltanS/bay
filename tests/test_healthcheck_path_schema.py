"""Schema tests for the `healthcheck_path` field on service objects.

Covers:
- Valid path starting with slash is accepted by the schema (structure check)
- Field is optional (not in `required`)
- Pattern `^/.+` rejects a string without a leading slash
- Property type is `string` (rejects non-string values at validation)
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)


def _load_service_props() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text())
    return schema["$defs"]["service"]["properties"]


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


# ── happy-path ────────────────────────────────────────────────────────


def test_valid_path_accepted():
    """Schema declares `healthcheck_path` with a leading-slash pattern,
    confirming that values like `/healthcheck` are structurally valid."""
    props = _load_service_props()
    assert "healthcheck_path" in props, (
        "schema must declare `healthcheck_path` on service"
    )
    pattern = props["healthcheck_path"].get("pattern", "")
    assert pattern.startswith("^/"), (
        f"pattern must enforce a leading slash, got: {pattern!r}"
    )


# ── optional field ────────────────────────────────────────────────────


def test_optional_field_not_in_required():
    """`healthcheck_path` is optional — services without it must still validate."""
    schema = _load_schema()
    required = schema["$defs"]["service"].get("required", [])
    assert "healthcheck_path" not in required, (
        "`healthcheck_path` must not be in the service `required` list; "
        "omitting it must preserve the current `/` behaviour"
    )


# ── rejection tests ───────────────────────────────────────────────────


def test_no_leading_slash_pattern_rejects():
    """Pattern `^/.+` means a bare string like `healthcheck` (no leading slash)
    would be rejected by JSON Schema validation — the `.+` also ensures
    the pattern matches at least one character after the slash."""
    props = _load_service_props()
    pattern = props.get("healthcheck_path", {}).get("pattern", "")
    assert pattern == "^/.+", (
        f"expected exact pattern `^/.+` to reject both no-slash and bare-slash "
        f"values, got: {pattern!r}"
    )


def test_non_string_type_rejected():
    """`healthcheck_path` must be typed `string` — non-string values like
    `true` or `42` are rejected by the type constraint."""
    props = _load_service_props()
    hp = props.get("healthcheck_path", {})
    assert hp.get("type") == "string", (
        f"healthcheck_path must have type `string`, got: {hp.get('type')!r}"
    )
