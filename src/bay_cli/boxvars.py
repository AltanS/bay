"""Read one plain variable of a fleet box from the fleet's group_vars.

The compile is pure (fleet file + bay.toml files), but a few bay.toml keys are
only safe on a box configured a certain way, and that configuration lives in
group_vars. This reads it the way Ansible would for a plain YAML value:
``group_vars/<box group>``, then ``group_vars/<box env>``, then
``group_vars/all``; within a group, ``<group>.yml`` and every ``*.yml`` /
``*.yaml`` in the ``<group>/`` folder. A vault file is skipped, and a Jinja
value is not evaluated: a flag that is not a literal fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def _files(root: Path, group: str) -> list[Path]:
    base = root / "group_vars"
    files = [base / f"{group}.yml", base / f"{group}.yaml"]
    folder = base / group
    if folder.is_dir():
        files += sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")])
    return [f for f in files if f.is_file()]


def _groups(box_name: str, box: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for group in (box.get("group"), box.get("env"), "all"):
        if isinstance(group, str) and group and group not in out:
            out.append(group)
    if not box.get("group") and box_name not in out:
        out.insert(0, box_name)
    return out


def box_var(root: Path, box_name: str, box: Mapping[str, Any], key: str) -> tuple[bool, Any, str]:
    """``(found, value, source)``; ``source`` names the file, or the folders searched."""
    groups = _groups(box_name, box)
    for group in groups:
        for path in _files(root, group):
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            if text.lstrip().startswith("$ANSIBLE_VAULT"):
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                continue
            if isinstance(data, Mapping) and key in data:
                return True, data[key], str(path.relative_to(root))
    searched = ", ".join(f"group_vars/{g}" for g in groups)
    return False, None, searched


def box_flag(root: Path, box_name: str, box: Mapping[str, Any], key: str) -> tuple[Any, str]:
    """``(state, source)``: state is True, False, None (unset) or the raw value
    when it is neither a boolean nor a boolean word (a Jinja expression)."""
    found, value, source = box_var(root, box_name, box, key)
    if not found:
        return None, source
    if isinstance(value, bool):
        return value, source
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE:
            return True, source
        if word in _FALSE:
            return False, source
    return repr(value), source
