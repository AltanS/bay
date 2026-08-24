"""Drift-guard test for the `backup.method` enum (GitHub issue #25).

The schema's `$defs.backup_block.properties.method.enum` must always match
the set of `method == '<value>'` branches actually implemented in
`roles/backup/templates/backup.sh.j2`. Historically these drifted: the
schema allowed `restic`/`files`, neither of which has a branch in the
template (selecting either yields no backup command and a later
`restic stats latest` failure), while the template's real branches
(`pg_dump`, `mysql`, `redis`, `file`) were only partially represented.

This test parses both sides independently and asserts they're the same
set, so a future addition to only one side fails CI with a clear message
instead of silently drifting again.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "bay_cli"
    / "schemas"
    / "services.schema.json"
)

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "roles"
    / "backup"
    / "templates"
    / "backup.sh.j2"
)

# Matches `{% if method == 'pg_dump' %}` and `{% elif method == 'file' %}`
# (single or double quotes, either keyword).
_METHOD_BRANCH_RE = re.compile(
    r"{%-?\s*(?:if|elif)\s+method\s*==\s*['\"](\w+)['\"]\s*-?%}"
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _schema_method_enum() -> set[str]:
    schema = _load_schema()
    return set(schema["$defs"]["backup_block"]["properties"]["method"]["enum"])


def _template_method_branches() -> set[str]:
    text = TEMPLATE_PATH.read_text()
    return set(_METHOD_BRANCH_RE.findall(text))


# ── drift guard ──────────────────────────────────────────────────────────


def test_schema_enum_matches_template_branches():
    """The schema's `method` enum must exactly match the implemented
    `{% if/elif method == '...' %}` branches in backup.sh.j2 — no more
    (unimplemented methods that silently no-op), no less (implemented
    methods the schema rejects)."""
    enum_values = _schema_method_enum()
    branch_values = _template_method_branches()

    assert branch_values, (
        "no `method == '...'` branches found in backup.sh.j2 — "
        "regex may be out of sync with the template"
    )
    assert enum_values == branch_values, (
        f"schema `method` enum {sorted(enum_values)} does not match "
        f"backup.sh.j2 branches {sorted(branch_values)}; update "
        "src/bay_cli/schemas/services.schema.json and/or "
        "roles/backup/templates/backup.sh.j2 so they agree "
        "(see GitHub issue #25)"
    )


def test_no_stale_methods_in_enum():
    """`restic` and `files` were never implemented as methods (they're the
    restic ENGINE, not a method value) and must not reappear in the enum."""
    enum_values = _schema_method_enum()
    assert "restic" not in enum_values
    assert "files" not in enum_values


# ── jsonschema validation ─────────────────────────────────────────────────


def _accessory_with_method(method: str) -> dict:
    return {
        "accessories": {
            "db": {
                "image": "postgres:16",
                "backup": {
                    "method": method,
                    "schedule": "0 4 * * *",
                    "retain": 30,
                },
            }
        },
    }


def test_valid_file_method_accepted():
    """`method: file` is a real, implemented method and must validate."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(_accessory_with_method("file")))
    assert errors == [], f"Unexpected errors: {errors}"


def test_stale_method_files_rejected():
    """`method: files` (plural — never implemented) must be rejected."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(_accessory_with_method("files")))
    assert len(errors) > 0


def test_stale_method_restic_rejected():
    """`method: restic` (the engine, not a method) must be rejected."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(_accessory_with_method("restic")))
    assert len(errors) > 0


def test_file_method_with_source_path_accepted():
    """`source_path` is the key backup.sh.j2's `file` branch and restore.yml
    actually consume (docs/backups.md documents it) — with
    `additionalProperties: false` on backup_block it must be declared in the
    schema or every documented file-backup config is rejected."""
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    doc = _accessory_with_method("file")
    doc["accessories"]["db"]["backup"]["source_path"] = "/var/lib/app"
    errors = list(validator.iter_errors(doc))
    assert errors == [], f"Unexpected errors: {errors}"
