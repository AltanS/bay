"""``bay init``: draft a bay.toml in an app repo and register the app in the fleet.

The draft is a starting point, never a guess presented as fact. It reads
three hints from the repo:

* ``Dockerfile``: ``EXPOSE <port>`` gives the port. Without a Dockerfile the
  draft says so in a comment (the file builds ``./Dockerfile`` by default).
* ``package.json``: port 3000, and ``npm start`` as a commented command when
  a start script exists.
* ``pyproject.toml``: port 8000.

``access.mode`` is ``public``. There is one ``[deploy.<primary env>]`` table
with the box and domain from ``bay.fleet.toml`` unless ``--box`` and
``--domain`` say otherwise.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bay_cli import bay_toml, gitrepo, lockfile
from bay_cli.context import Context
from bay_cli.errors import BayError, ErrorCode
from bay_cli.fleet import PROJECTS_DIR

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class Hints:
    port: int
    command: str | None
    dockerfile: bool
    source: str


def detect(repo: Path) -> Hints:
    dockerfile = repo / "Dockerfile"
    expose = None
    if dockerfile.is_file():
        match = _EXPOSE_RE.search(dockerfile.read_text(errors="replace"))
        if match:
            expose = int(match.group(1))
    command = None
    port, source = 8080, "default"
    package = repo / "package.json"
    if package.is_file():
        port, source = 3000, "package.json"
        try:
            data = json.loads(package.read_text())
        except ValueError:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
            if "start" in data["scripts"]:
                command = "npm start"
    elif (repo / "pyproject.toml").is_file():
        port, source = 8000, "pyproject.toml"
    if expose is not None:
        port, source = expose, "Dockerfile EXPOSE"
    return Hints(port=port, command=command, dockerfile=dockerfile.is_file(), source=source)


def default_name(repo: Path) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", repo.name.lower()).strip("-")
    return name or "app"


def draft(*, name: str, fleet_name: str, env: str, box: str, domain: str, hints: Hints) -> str:
    lines = [
        "# Drafted by bay init. Check every value, then commit this file.",
        "# Reference: docs/bay-toml.md in the Bay repo.",
        f'name = "{name}"',
        f'fleet = "{fleet_name}"',
        f"port = {hints.port}                    # from {hints.source}",
    ]
    if hints.command:
        lines.append(f'# command = "{hints.command}"   # default: the image CMD')
    if not hints.dockerfile:
        lines.append(
            '# No Dockerfile found. Add one, or set image = "<image>" on a line above [access].'
        )
    lines += [
        "",
        "[access]",
        'mode = "public"               # public | tailnet | internal',
        "",
        f"[deploy.{env}]",
        f'box = "{box}"',
        f'domain = "{domain}"',
        "",
    ]
    return "\n".join(lines)


def init_project(
    cx: Context,
    repo: Path,
    *,
    name: str | None = None,
    box: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Write ``bay.toml`` and ``projects/<name>.lock``, then commit the fleet."""
    from bay_cli.plan import load_fleet_doc

    if not gitrepo.is_repo(repo):
        raise BayError(f"{repo} is not a git repo", hint="Run bay init inside the app repo.")
    top = gitrepo._out(repo, "rev-parse", "--show-toplevel")
    root = Path(top) if top else repo
    toml_file = root / "bay.toml"
    if toml_file.exists():
        raise BayError(
            f"{toml_file} already exists",
            code=ErrorCode.CONFLICT,
            hint="bay init drafts a new file only. Use bay plan to check it.",
        )
    if not gitrepo.is_repo(cx.fleet_root) or gitrepo.head(cx.fleet_root) is None:
        raise BayError(f"the fleet {cx.fleet_root} is not a git repo with a commit")

    fleet_doc = load_fleet_doc(cx)
    project = name or default_name(root)
    if not _NAME_RE.match(project):
        raise BayError(
            f"name {project} must use only a-z, 0-9 and -, and start with a letter or digit",
            hint="Pass --name.",
        )
    lock_file = lockfile.lock_path(cx.fleet_root, project)
    if lock_file.exists() or (cx.fleet_root / PROJECTS_DIR / project).exists():
        raise BayError(
            f"project {project} already exists in fleet {fleet_doc['name']}",
            code=ErrorCode.CONFLICT,
            hint="A name never changes. Pick another name with --name.",
        )

    boxes = fleet_doc.get("boxes") or {}
    chosen_box = box or str(fleet_doc["default_box"])
    if chosen_box not in boxes:
        raise BayError(
            f"box {chosen_box} is not in bay.fleet.toml", hint="Boxes: " + ", ".join(sorted(boxes))
        )
    env = str(fleet_doc.get("primary_env", "production"))
    chosen_domain = domain or f"{project}.{fleet_doc['default_domain']}"
    hints = detect(root)
    text = draft(
        name=project,
        fleet_name=str(fleet_doc["name"]),
        env=env,
        box=chosen_box,
        domain=chosen_domain,
        hints=hints,
    )
    doc = tomllib.loads(text)
    problems = bay_toml.validate(doc)
    if problems:
        raise BayError("the draft is not valid:\n  " + "\n  ".join(str(v) for v in problems))

    repo_url = gitrepo.remote_url(root)
    raw = lockfile.new_lock(project, repo=repo_url, local_path=str(root.resolve()))
    toml_file.write_text(text)
    lockfile.write(lock_file, raw)
    try:
        fleet_commit = gitrepo.commit_paths(cx.fleet_root, [lock_file], f"bay: init {project}")
    except gitrepo.GitError as exc:
        raise BayError(f"cannot commit the fleet repo: {exc}") from None
    warnings: list[str] = []
    if repo_url is None:
        warnings.append(
            "the repo has no origin remote; a build from source needs one "
            "(the lock's repo is empty)"
        )
    if not hints.dockerfile:
        warnings.append("no Dockerfile: add one, or set image in bay.toml")
    return {
        "project": project,
        "fleet": fleet_doc["name"],
        "bay_toml": str(toml_file),
        "lock": str(lock_file),
        "repo": repo_url,
        "fleet_commit": fleet_commit,
        "env": env,
        "box": chosen_box,
        "domain": chosen_domain,
        "warnings": warnings,
    }
