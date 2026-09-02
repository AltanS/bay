"""Every git invocation must disable interactive credential prompts.

`git fetch`/`clone`/`pull`/`ls-remote` talking to a remote that answers with a
credential challenge (e.g. a public HTTPS repo GitHub decides to challenge
anyway) will, by default, block on stdin for a username. Ansible has no tty
to answer with, so the task — and the whole deploy — hangs until something
kills it.

This bit in production: `remote_build.yml`'s no-token "Fetch latest changes
(SSH)" task had no `GIT_TERMINAL_PROMPT` in its `environment:`, unlike the
token variant right above it, and a deploy hung for 10 minutes.

`GIT_TERMINAL_PROMPT=0` makes git fail fast (`fatal: could not read
Username for '...'`) instead of waiting. Every git task/template in the
framework must set it, not just the ones that already authenticate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROLES = _REPO_ROOT / "roles"

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_GIT_REMOTE_VERBS = ("clone", "fetch", "pull", "ls-remote", "submodule")


def _is_git_remote_call(line: str) -> bool:
    line = line.strip()
    if not line:
        return False
    # Matches a bare `git <verb>` invocation anywhere in the string — tasks
    # sometimes prefix it with `env FOO=bar git fetch ...` or wrap it in a
    # `git -c ... fetch ...` form, so this can't require the string to start
    # with "git".
    tokens = line.replace("\n", " ").split()
    for i, tok in enumerate(tokens):
        if tok == "git" or tok.endswith("/git"):
            rest = tokens[i + 1 :]
            # Skip `-c key=value` pairs between `git` and the verb.
            j = 0
            while j < len(rest) and rest[j] == "-c":
                j += 2
            if j < len(rest) and rest[j] in _GIT_REMOTE_VERBS:
                # `submodule status`/`submodule sync` don't touch the network;
                # only update/init do (and the framework doesn't use those
                # today, but keep the check honest for the future).
                return True
    return False


def _walk_tasks(items):
    """Flatten block/rescue/always nesting (and loop bodies, which are plain
    dicts under the same keys) into a single sequence of task dicts."""
    for task in items or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _walk_tasks(task[key])


def _task_command_lines(task: dict):
    """Every raw command string a task could run, across the modules the
    framework uses for git (`command`, `shell`)."""
    for mod_name in ("ansible.builtin.command", "ansible.builtin.shell"):
        mod = task.get(mod_name)
        if isinstance(mod, dict):
            for key in ("cmd", "_raw_params"):
                if key in mod:
                    yield str(mod[key])
        elif isinstance(mod, str):
            yield mod
    # Free-form `command: git fetch ...` / `shell: git fetch ...` shorthand.
    for mod_name in ("command", "shell"):
        val = task.get(mod_name)
        if isinstance(val, str):
            yield val


def _all_task_files():
    return sorted(_ROLES.glob("*/tasks/*.yml")) + sorted(_ROLES.glob("*/tasks/**/*.yml"))


def _git_remote_tasks_missing_prompt_guard():
    offenders = []
    seen_files = set()
    for task_file in _all_task_files():
        if task_file in seen_files:
            continue
        seen_files.add(task_file)
        try:
            loaded = yaml.safe_load(task_file.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(loaded, list):
            continue
        for task in _walk_tasks(loaded):
            lines = list(_task_command_lines(task))
            if not any(_is_git_remote_call(line) for line in lines):
                continue
            env = task.get("environment")
            if not isinstance(env, dict) or env.get("GIT_TERMINAL_PROMPT") != "0":
                offenders.append(
                    f"{task_file.relative_to(_REPO_ROOT)}: "
                    f"{task.get('name', '<unnamed>')}"
                )
    return offenders


def test_every_git_remote_task_disables_terminal_prompt():
    offenders = _git_remote_tasks_missing_prompt_guard()
    assert not offenders, (
        "git tasks that can hang on an interactive credential prompt "
        f"(missing environment.GIT_TERMINAL_PROMPT: \"0\"): {offenders}"
    )


def _mentions_git_remote_call(text: str) -> bool:
    """True if the template actually shells out to git fetch/clone somewhere,
    as opposed to merely mentioning it in a comment or doc string (e.g. a
    Jinja `{#- ... -#}` block or a `#` shell comment describing behavior)."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#") or line.startswith("{#"):
            continue
        # Backtick-quoted prose like `` `git clone` `` inside a comment body
        # that isn't itself a shell comment line (multi-line {# #} blocks) —
        # skip lines that are just backtick-wrapped mentions.
        if "`git clone`" in line or "`git fetch`" in line:
            continue
        if "git fetch" in line or "git clone" in line:
            return True
    return False


def _templates_missing_prompt_guard():
    offenders = []
    for template in sorted(_ROLES.glob("*/templates/**/*.j2")):
        text = template.read_text()
        if not _mentions_git_remote_call(text):
            continue
        if "GIT_TERMINAL_PROMPT=0" not in text and "GIT_TERMINAL_PROMPT: \"0\"" not in text:
            offenders.append(str(template.relative_to(_REPO_ROOT)))
    return offenders


def test_every_git_template_disables_terminal_prompt():
    offenders = _templates_missing_prompt_guard()
    assert not offenders, (
        "templates that shell out to git fetch/clone without disabling "
        f"terminal prompts: {offenders}"
    )


def test_rebuild_sh_ssh_pull_disables_terminal_prompt():
    """Regression for the actual incident: the deploy-key (SSH, no token)
    PULL_CMD in rebuild.sh was missing GIT_TERMINAL_PROMPT=0 on its own
    line, even though the file-level check above would have stayed green
    (other command strings in the same file already set it)."""
    from test_rebuild_config import _local_service, _render_rebuild_sh

    rendered = _render_rebuild_sh(
        _local_service(), ["localapp"], git_deploy_services=["localapp"]
    )
    pull_lines = [
        line for line in rendered.splitlines() if line.strip().startswith("PULL_CMD=")
    ]
    assert pull_lines, "expected a PULL_CMD assignment in the local/SSH render"
    assert "GIT_TERMINAL_PROMPT=0" in pull_lines[0], pull_lines[0]
