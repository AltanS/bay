"""Shared test helpers for template rendering.

Ansible's Jinja2 config defaults to `trim_blocks=True, lstrip_blocks=False`.
Tests that render .j2 templates with a bare `jinja2.Environment()` miss
whitespace-sensitive rendering bugs that production Ansible would hit.
See the v0.76.0 trim_blocks regression for a concrete example.
"""

import json
import shlex
import shutil
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
            # webhook receiver config: the extra services one push builds.
            "bay_push_siblings",
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


def host_tools(directory: Path, *names: str) -> Path:
    """Make `directory` hold a symlink to this host's copy of each tool.

    A rendered box script names Debian paths (`/usr/bin/cat`) and a test may
    pin PATH to `/usr/bin:/bin`. NixOS has neither, so a test points the
    script or PATH at this folder instead. A missing tool fails the test.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        found = shutil.which(name)
        assert found, f"{name} is not on this host's PATH"
        (directory / name).symlink_to(found)
    return directory


def run_role_tasks(
    tmp_path: Path,
    tasks_file: Path,
    task_names: list[str],
    variables: dict,
    *,
    path_dir: Path | None = None,
    check: bool = False,
    transform=None,
):
    """Run the named tasks of a role task file with ansible-playbook on localhost.

    Tasks are found by name, also inside a block, and run in the order given.
    `path_dir` goes first on PATH, so a test can stand a fake `id` or
    `gpasswd` in front of the real one. Returns the CompletedProcess; the
    stdout holds the task results and the PLAY RECAP. `transform(task)` may
    return a changed copy of each chosen task, for example to point a `dest`
    at a scratch folder and drop `become`.
    """
    import os
    import subprocess
    import sys

    import yaml

    def flat(tasks):
        for task in tasks:
            if "block" in task:
                yield from flat(task["block"])
            else:
                yield task

    by_name = {t["name"]: t for t in flat(yaml.safe_load(tasks_file.read_text()))}
    chosen = []
    for name in task_names:
        assert name in by_name, f"task {name!r} not found in {tasks_file}"
        chosen.append(transform(dict(by_name[name])) if transform else by_name[name])
    work = tmp_path / "play"
    work.mkdir(parents=True, exist_ok=True)
    (work / "ansible.cfg").write_text("[defaults]\n")
    (work / "vars.json").write_text(json.dumps(variables))
    (work / "play.yml").write_text(
        yaml.safe_dump(
            [{"hosts": "localhost", "connection": "local", "gather_facts": False,
              "tasks": chosen}]
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(work / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    if path_dir is not None:
        env["PATH"] = f"{path_dir}:{env['PATH']}"
    cmd = [sys.executable, "-m", "ansible.cli.playbook", "-i", "localhost,",
           "-e", f"@{work / 'vars.json'}"]
    if check:
        cmd.append("--check")
    cmd.append(str(work / "play.yml"))
    return subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=180)
