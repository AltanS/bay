"""INI inventory file parser — preserves comments and structure on round-trip."""

from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from bay_cli.errors import BayError


@dataclass
class HostEntry:
    """A host in the inventory."""

    name: str
    variables: dict[str, str] = field(default_factory=dict)

    @property
    def ip(self) -> str:
        """Return ansible_host if set, otherwise the name (which may be an IP)."""
        return self.variables.get("ansible_host", self.name)

    def to_line(self) -> str:
        if self.variables:
            vars_str = " ".join(f"{k}={v}" for k, v in self.variables.items())
            return f"{self.name} {vars_str}"
        return self.name


# Internal representation: each line is tagged
_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_HOST_RE = re.compile(r"^(\S+)((?:\s+\w+=\S+)*)\s*$")


@dataclass
class _Line:
    """A single line in the inventory file."""

    kind: str  # "group", "children", "host", "comment", "blank"
    raw: str  # original text (for comments/blanks)
    group: str = ""  # group name (for group/children lines)
    host: HostEntry | None = None  # parsed host (for host lines)


class InventoryConfig:
    """Read/write interface to Ansible INI inventory files.

    Preserves comments, blank lines, host variables, and group structure
    on round-trip.
    """

    def __init__(self) -> None:
        self._path: Path | None = None
        self._original_lines: list[_Line] = []
        self._working_lines: list[_Line] = []
        self._loaded = False

    def load(self, path: Path) -> None:
        """Parse an INI inventory file."""
        if not path.is_file():
            raise BayError(f"Inventory file not found: {path}")

        self._path = path
        self._original_lines = self._parse(path.read_text())
        self._working_lines = copy.deepcopy(self._original_lines)
        self._loaded = True

    def _parse(self, text: str) -> list[_Line]:
        lines: list[_Line] = []
        current_group = ""

        for raw_line in text.splitlines():
            stripped = raw_line.strip()

            if not stripped:
                lines.append(_Line(kind="blank", raw=raw_line))
                continue

            if stripped.startswith("#") or stripped.startswith(";"):
                lines.append(_Line(kind="comment", raw=raw_line))
                continue

            group_match = _LINE_RE.match(stripped)
            if group_match:
                group_spec = group_match.group(1)
                if group_spec.endswith(":children"):
                    group_name = group_spec[: -len(":children")]
                    lines.append(_Line(kind="children", raw=raw_line, group=group_name))
                    current_group = f"{group_name}:children"
                else:
                    current_group = group_spec
                    lines.append(_Line(kind="group", raw=raw_line, group=group_spec))
                continue

            # Host line
            host_match = _HOST_RE.match(stripped)
            if host_match:
                hostname = host_match.group(1)
                vars_str = host_match.group(2).strip()
                variables: dict[str, str] = {}
                if vars_str:
                    for pair in vars_str.split():
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            variables[k] = v
                host = HostEntry(name=hostname, variables=variables)
                lines.append(
                    _Line(kind="host", raw=raw_line, group=current_group, host=host)
                )
                continue

            # Fallback: treat as comment
            lines.append(_Line(kind="comment", raw=raw_line))

        return lines

    # ── Read ─────────────────────────────────────────────────────────

    def list_hosts(self) -> list[dict[str, str]]:
        """Return all hosts with their group and variables."""
        self._ensure_loaded()
        result: list[dict[str, str]] = []
        for line in self._working_lines:
            if line.kind == "host" and line.host:
                entry: dict[str, str] = {
                    "name": line.host.name,
                    "ip": line.host.ip,
                    "group": line.group,
                }
                entry.update(line.host.variables)
                result.append(entry)
        return result

    def get_groups(self) -> list[str]:
        """Return all group names (excluding :children groups)."""
        self._ensure_loaded()
        groups: list[str] = []
        for line in self._working_lines:
            if line.kind == "group" and line.group not in groups:
                groups.append(line.group)
        return groups

    def get_children_groups(self) -> dict[str, list[str]]:
        """Return parent -> [child groups] mapping."""
        self._ensure_loaded()
        result: dict[str, list[str]] = {}
        current_parent = ""
        in_children = False

        for line in self._working_lines:
            if line.kind == "children":
                current_parent = line.group
                result[current_parent] = []
                in_children = True
            elif line.kind == "group":
                in_children = False
            elif in_children and line.kind == "host" and line.host:
                # In children section, "hosts" are actually child group names
                result[current_parent].append(line.host.name)

        return result

    # ── Mutations ────────────────────────────────────────────────────

    def add_host(
        self,
        name: str,
        ip: str,
        group: str,
        variables: dict[str, str] | None = None,
    ) -> None:
        """Add a host to the specified group.

        Idempotent: if host already exists in the group, this is a no-op.
        Creates the group section if it doesn't exist.
        """
        self._ensure_loaded()
        all_vars = variables or {}
        if ip != name:
            all_vars["ansible_host"] = ip

        host = HostEntry(name=name, variables=all_vars)

        # Check if host already exists in this group
        for line in self._working_lines:
            if (
                line.kind == "host"
                and line.group == group
                and line.host
                and line.host.name == name
            ):
                return  # idempotent

        # Find the group section and insert after it
        insert_idx = self._find_group_end(group)
        if insert_idx is None:
            # Group doesn't exist — create it
            if self._working_lines and self._working_lines[-1].kind != "blank":
                self._working_lines.append(_Line(kind="blank", raw=""))
            self._working_lines.append(
                _Line(kind="group", raw=f"[{group}]", group=group)
            )
            self._working_lines.append(
                _Line(kind="host", raw=host.to_line(), group=group, host=host)
            )
        else:
            self._working_lines.insert(
                insert_idx,
                _Line(kind="host", raw=host.to_line(), group=group, host=host),
            )

    def remove_host(self, name: str) -> bool:
        """Remove a host from all groups.

        Returns True if removed, False if not found.
        """
        self._ensure_loaded()
        removed = False
        new_lines: list[_Line] = []
        for line in self._working_lines:
            if line.kind == "host" and line.host and line.host.name == name:
                removed = True
                continue
            new_lines.append(line)
        self._working_lines = new_lines
        return removed

    # ── Diff & Save ──────────────────────────────────────────────────

    def diff(self) -> str:
        """Return unified diff of original vs working copy."""
        self._ensure_loaded()
        original = self._render(self._original_lines)
        working = self._render(self._working_lines)
        path_str = str(self._path or "inventory")

        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            working.splitlines(keepends=True),
            fromfile=path_str,
            tofile=path_str,
        )
        return "".join(diff_lines)

    def save(self) -> None:
        """Write the working copy back to the inventory file."""
        self._ensure_loaded()
        if self._path is None:
            raise BayError("No inventory file loaded")
        self._path.write_text(self._render(self._working_lines))

    def has_changes(self) -> bool:
        """Check if there are unsaved changes."""
        self._ensure_loaded()
        return self._render(self._original_lines) != self._render(self._working_lines)

    # ── Helpers ───────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raise BayError("No inventory file loaded — call load() first")

    def _find_group_end(self, group: str) -> int | None:
        """Find the index where a new host should be inserted for the group."""
        in_group = False
        last_host_idx = None

        for i, line in enumerate(self._working_lines):
            if line.kind == "group" and line.group == group:
                in_group = True
                last_host_idx = i + 1  # insert right after [group]
            elif in_group:
                if line.kind in ("group", "children"):
                    break  # entered next section
                if line.kind == "host":
                    last_host_idx = i + 1

        return last_host_idx

    def _render(self, lines: list[_Line]) -> str:
        """Render lines back to text."""
        result: list[str] = []
        for line in lines:
            if line.kind == "host" and line.host:
                result.append(line.host.to_line())
            else:
                result.append(line.raw)
        text = "\n".join(result)
        if text and not text.endswith("\n"):
            text += "\n"
        return text
