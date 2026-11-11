"""`bay validate` warns about wide `tagOwners` in headscale_acl_policy.

A tag's owners can enrol a node with that tag, and the node gets every grant
of the tag. More than one owner, a group, `autogroup:*` or `*` is a wide set.
That is a warning, never a failure: a wide set can be on purpose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bay_cli.commands.validate import ValidationResult, _validate_acl_tag_owners, run_validation
from bay_cli.console.output import set_json_mode


def _run(tag_owners, *, key="headscale_acl_policy") -> ValidationResult:
    result = ValidationResult()
    policy = {"tagOwners": tag_owners, "acls": []} if tag_owners is not None else {"acls": []}
    set_json_mode(True)
    try:
        _validate_acl_tag_owners({"group_vars/all/headscale_acl.yml": {key: policy}}, result)
    finally:
        set_json_mode(False)
    return result


def test_one_user_per_tag_is_clean():
    result = _run({"tag:agent": ["alice@"], "tag:prod-app": ["alice@"]})
    assert result.warnings == []
    assert result.failed == []
    assert any("2 tag(s)" in p for p in result.passed)


@pytest.mark.parametrize(
    ("owners", "needle"),
    [
        (["alice@", "bob@"], "2 owners (alice@, bob@)"),
        (["group:ops"], "'group:ops' is a group"),
        (["autogroup:member"], "'autogroup:member' is a built-in group"),
        (["autogroup:admin"], "built-in group"),
        (["*"], "'*'"),
    ],
)
def test_a_wide_owner_set_warns_and_names_the_tag(owners, needle):
    result = _run({"tag:agent": owners, "tag:ok": ["alice@"]})
    assert result.failed == []
    assert len(result.warnings) == 1
    message = result.warnings[0]
    assert "tag:agent" in message
    assert needle in message
    assert "tag:ok" not in message
    assert "every owner can enrol a node with that tag and so gets every grant of the tag" in (
        " ".join(message.split()).lower()
    )


def test_each_wide_tag_gets_its_own_warning():
    result = _run({"tag:a": ["x@", "y@"], "tag:b": ["group:g"], "tag:c": ["z@"]})
    assert len(result.warnings) == 2
    assert result.failed == []


def test_a_single_string_owner_is_accepted():
    assert _run({"tag:agent": "alice@"}).warnings == []
    assert len(_run({"tag:agent": "*"}).warnings) == 1


def test_no_policy_or_no_tag_owners_stays_silent():
    for tag_owners in (None, {}):
        result = _run(tag_owners)
        assert result.warnings == [] and result.passed == [] and result.failed == []
    result = ValidationResult()
    _validate_acl_tag_owners({"group_vars/all/x.yml": {"other": 1}}, result)
    assert result.warnings == [] and result.passed == []


def test_run_validation_reaches_the_check_and_does_not_fail(tmp_path: Path):
    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)
    (gv / "services.yml").write_text(
        "---\nservices:\n  app:\n    image: nginx:latest\n"
        "    access: public\n    domains:\n      - app.example.com\n"
        "    ports:\n      internal: 8080\naccessories: {}\n"
    )
    (gv / "main.yml").write_text("---\ndomain_base: example.com\nletsencrypt_email: ops@example.com\n")
    (gv / "headscale_acl.yml").write_text(
        "---\nheadscale_acl_policy:\n  tagOwners:\n    \"tag:agent\": [\"alice@\", \"bob@\"]\n"
        "  acls: []\n"
    )
    (tmp_path / "hosts").mkdir()
    (tmp_path / "hosts" / "production").write_text("[production]\n10.0.0.1\n")
    set_json_mode(True)
    try:
        result = run_validation(tmp_path, "production", show_banner=False)
    finally:
        set_json_mode(False)
    assert result.total_issues == 0
    assert any("tag:agent" in w and "owners" in w for w in result.warnings)
