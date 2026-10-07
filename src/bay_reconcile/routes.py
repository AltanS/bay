"""The tailnet routes a box serves, read from its rendered Traefik route file.

The traefik role renders ``<stack_dir>/dynamic/tailnet-proxies.yml`` from
``roles/traefik/templates/dynamic/tailnet-proxies.yml.j2`` on the ingress box.
:func:`rendered_routes` reads that file back so the deploy receipt can list the
routes the box really serves (RUNNING), next to the fleet table (WANTED) and
the compiled ``tailnet_proxies`` (PINNED). ``bay show --routes`` compares the
three.

Not wired yet: :mod:`bay_reconcile.receipt` will add
``"routes": rendered_routes(<file>)`` to the receipt. Until then the receipt
has no ``routes`` key and ``bay show --routes`` reports RUNNING as unknown.

Stdlib only, like the rest of the package: it runs on the box, which has no
YAML library. The parser reads only the fixed shape the template writes; a
line it does not know is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROUTE_FILE = Path("dynamic") / "tailnet-proxies.yml"
_SUFFIX = "-tailnet"
_HOST_RE = re.compile(r"Host\(`([^`]+)`\)")
_KEY_RE = re.compile(r"^( *)([A-Za-z0-9_.-]+):\s*(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text: str) -> list[dict[str, Any]]:
    """Routes in a rendered route file, sorted by name.

    Each: ``name``, ``domains``, ``upstream``, ``pass_host_header``,
    ``identity_inject`` and ``entrypoint``, the keys of a compiled
    ``tailnet_proxies`` entry with every default spelled out.
    """
    routes: dict[str, dict[str, Any]] = {}
    section = ""
    current: dict[str, Any] | None = None
    field = ""
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        item = line.strip()
        if item.startswith("- "):
            value = _unquote(item[2:])
            if current is None:
                continue
            if field == "entryPoints" and current.get("entrypoint") is None:
                current["entrypoint"] = value
            elif field == "middlewares" and value == "tailnet-identity@file":
                current["identity_inject"] = True
            elif field == "servers" and value.startswith("url:"):
                current["upstream"] = _unquote(value[4:])
            continue
        m = _KEY_RE.match(line)
        if not m:
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        if indent == 2:
            section, current, field = key, None, ""
            continue
        if indent == 4 and section in ("routers", "services") and key.endswith(_SUFFIX):
            name = key[: -len(_SUFFIX)]
            current = routes.setdefault(
                name,
                {
                    "name": name,
                    "domains": [],
                    "upstream": None,
                    "pass_host_header": True,
                    "identity_inject": False,
                    "entrypoint": None,
                },
            )
            field = ""
            continue
        if current is None or indent < 6:
            continue
        field = key
        if key == "rule" and section == "routers":
            current["domains"] = _HOST_RE.findall(_unquote(value))
        elif key == "passHostHeader" and section == "services":
            current["pass_host_header"] = _unquote(value).lower() != "false"
    return [routes[k] for k in sorted(routes)]


def rendered_routes(stack_dir: Path) -> list[dict[str, Any]]:
    """Routes in ``<stack_dir>/dynamic/tailnet-proxies.yml``; ``[]`` when there is none."""
    path = stack_dir / ROUTE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return parse(text)
