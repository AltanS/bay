"""Shared test helpers for template rendering.

Ansible's Jinja2 config defaults to `trim_blocks=True, lstrip_blocks=False`.
Tests that render .j2 templates with a bare `jinja2.Environment()` miss
whitespace-sensitive rendering bugs that production Ansible would hit.
See the v0.76.0 trim_blocks regression for a concrete example.
"""

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
            "bay_alert_recipient",
            "bay_alert_registry",
            "bay_recipient_target",
            "bay_transform_body",
            "bay_alert_body",
            "bay_alert_content_type",
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
    return env
