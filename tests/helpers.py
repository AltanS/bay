"""Shared test helpers for template rendering.

Ansible's Jinja2 config defaults to `trim_blocks=True, lstrip_blocks=False`.
Tests that render .j2 templates with a bare `jinja2.Environment()` miss
whitespace-sensitive rendering bugs that production Ansible would hit.
See the v0.76.0 trim_blocks regression for a concrete example.
"""

import json
import shlex
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _alert_filters() -> dict:
    """The bay_alert_* filters the alert snippet needs at render time.

    Registered individually rather than by importing bay_filters.FilterModule:
    that pulls in Ansible's own filter set, whose `default` shadows Jinja's and
    returns empty for every `| default(...)` in the templates under test.
    """
    import importlib.util

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "bay_filters_for_tests", root / "filter_plugins" / "bay_filters.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        name: getattr(module, name)
        for name in (
            "bay_alert_recipients",
            "bay_alert_ids_for",
            "bay_alert_routing",
            "bay_env_value",
            "bay_alert_recipient",
            "bay_alert_registry",
            "bay_recipient_target",
            "bay_transform_body",
            "bay_alert_body",
            "bay_alert_content_type",
            "bay_env_name",
            "bay_alert_env_value",
            # rebuild.sh and the webhook receiver config: config-only pushes
            # of projects that share one app repo.
            "bay_shared_toml_paths",
            # no-input-change pushes: the build context as an input set.
            "bay_build_context",
            # provision-db.sql.j2: the role password verifier to compare.
            "bay_db_password_verifier",
        )
    }


def make_ansible_env(
    template_dir: Path | str,
    *,
    keep_trailing_newline: bool = True,
) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=keep_trailing_newline,
    )
    env.filters.update(_alert_filters())
    # Ansible ships these two; a bare Jinja Environment does not. Without them
    # every `| quote` in a template raises TemplateAssertionError at render
    # time and a test that only asserts "no unsafe payload" passes vacuously.
    env.filters["quote"] = shlex.quote
    env.filters["to_json"] = json.dumps
    return env


def typer_ctx(cx):
    """A `typer.Context` carrying a ready-made bay Context, for calling a command directly."""
    import click
    import typer

    return typer.Context(click.Command("bay"), obj=cx)


def patch_fleet(root: Path, framework: Path | None = None):
    """Make ``Context.resolve`` (what every command calls) return this fleet.

    Use as a context manager around a CLI invocation. The framework root
    defaults to ``<root>/.bay``, a path that only needs to exist as a name.
    """
    from unittest.mock import patch

    from bay_cli.context import Context

    cx = Context.for_fleet_root(root, framework if framework is not None else root / ".bay")
    return patch.object(Context, "resolve", classmethod(lambda cls, fleet=None: cx))
