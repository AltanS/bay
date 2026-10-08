"""Which pushed files are build inputs of a project: the box side of a no-input-change push.

The webhook receiver passes a push that changes none of a project's build
inputs with the trigger marker ``no_input_change`` (docs/build-pipeline.md,
"No-input-change push"). ``rebuild.sh`` never trusts that marker: it diffs the
previous commit against the pushed one itself and asks this module which of the
changed files count as build inputs. None: the image of the previous commit
gets the pushed commit's tag. Any: it builds.

The rule is the receiver's (``app.py`` ``input_filter``):

* ``--include`` patterns (``[build] watch``): a file counts only when it
  matches one. Without them, ``--context`` (the ``[build]`` context, given only
  when it is narrower than the repo root) narrows the inputs to the files under
  it, plus the Dockerfile and its ``<Dockerfile>.dockerignore``. With neither,
  every file counts.
* ``--exclude`` patterns (``[build] ignore``) then drop files again.

Patterns use gitignore syntax and the receiver matches them with ``pathspec``
(``PathSpec.from_lines("gitignore", ...)``). The box has no ``pathspec``, so
:func:`translate` is a stdlib port of its gitignore translation;
``tests/test_no_input_change.py`` checks both give the same answers. Stdlib
only, like the rest of the package.

``python -m bay_reconcile.pushinputs [--include P]... [--exclude P]...
[--context DIR --dockerfile FILE]`` reads the changed files, one per line, on
stdin and prints the ones that are build inputs. Exit codes: 0 done (an empty
output means no input changed), 2 a usage error or a pattern it cannot read.
The caller treats any exit other than 0 as "an input changed".
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Sequence


class PatternError(ValueError):
    """A gitignore pattern that cannot be translated (pathspec refuses it too)."""


def _segment_glob(pattern: str) -> str:
    """One path segment glob as a regex: ``*`` and ``?`` never match ``/``."""
    escape = False
    regex = ""
    i, end = 0, len(pattern)
    while i < end:
        char = pattern[i]
        i += 1
        if escape:
            escape = False
            regex += re.escape(char)
        elif char == "\\":
            escape = True
        elif char == "*":
            regex += "[^/]*"
        elif char == "?":
            regex += "[^/]"
        elif char == "[":
            j = i
            if j < end and pattern[j] in "!^":
                j += 1
            if j < end and pattern[j] == "]":
                j += 1
            while j < end and pattern[j] != "]":
                j += 1
            if j < end:
                j += 1
                expr = "["
                if pattern[i] in "!^":
                    expr += "^"
                    i += 1
                expr += pattern[i:j].replace("\\", "\\\\")
                regex += expr
                i = j
            else:
                regex += "\\["
        else:
            regex += re.escape(char)
    if escape:
        raise PatternError(f"escape character with nothing to escape: {pattern!r}")
    return regex


def _normalize(is_dir: bool, segs: list[str]) -> tuple[list[str] | None, str | None]:
    if not segs[0]:
        del segs[0]
    elif len(segs) == 1 or (len(segs) == 2 and not segs[1]):
        if segs[0] != "**":
            segs.insert(0, "**")
    if not segs:
        raise PatternError("pattern normalized to nothing")
    if not segs[-1]:
        segs[-1] = "**"
    for i in range(len(segs) - 1, 0, -1):
        if segs[i - 1] == "**" and segs[i] == "**":
            del segs[i]
    if len(segs) == 1 and segs[0] == "**":
        return None, ("/" if is_dir else ".")
    if len(segs) == 2 and segs == ["**", "*"]:
        return None, "."
    if len(segs) == 3 and segs == ["**", "*", "**"]:
        return None, "/"
    return segs, None


def _segments(segs: list[str]) -> str:
    out: list[str] = []
    need_slash = False
    last = len(segs) - 1
    for i, seg in enumerate(segs):
        if seg == "**":
            if i == 0:
                out.append("^(?:.+/)?")
            elif i < last:
                out.append("(?:/.+)?")
                need_slash = True
            else:
                out.append("/")
            continue
        if i == 0:
            out.append("^")
        if need_slash:
            out.append("/")
        out.append("[^/]+" if seg == "*" else _segment_glob(seg))
        if i == last:
            out.append("/?$" if seg == "*" else "(?:/|$)")
        need_slash = True
    return "".join(out)


def translate(pattern: str) -> tuple[str | None, bool | None]:
    """A gitignore pattern as ``(regex, include)``; ``(None, None)`` for a blank or a comment.

    ``include`` is False for a ``!`` pattern. Same translation as pathspec's
    ``gitignore`` pattern.
    """
    if not pattern.endswith("\\ "):
        pattern = pattern.rstrip()
    if not pattern or pattern.startswith("#"):
        return None, None
    include = True
    if pattern.startswith("!"):
        include = False
        pattern = pattern[1:]
    segs = pattern.split("/")
    is_dir = not segs[-1]
    if pattern == "/":
        is_dir = False
    segs_or_none, override = _normalize(is_dir, segs)
    if override is not None:
        return override, include
    assert segs_or_none is not None
    return _segments(segs_or_none), include


class Spec:
    """A list of gitignore patterns; the last pattern that matches a file decides."""

    def __init__(self, lines: Iterable[str]) -> None:
        self.rules: list[tuple[re.Pattern[str], bool]] = []
        for line in lines:
            regex, include = translate(line)
            if regex is not None and include is not None:
                self.rules.append((re.compile(regex), include))

    def match(self, path: str) -> bool:
        path = path.lstrip("/")
        while path.startswith("./"):
            path = path[2:]
        hit = False
        for regex, include in self.rules:
            if regex.search(path):
                hit = include
        return hit


def clean_dir(path: str | None) -> str:
    """A repo-relative directory without ``./`` and slashes at the ends; ``""`` is the root."""
    path = (path or "").strip()
    while path.startswith("./"):
        path = path[2:]
    path = path.strip("/")
    return "" if path == "." else path


def input_files(
    files: Iterable[str],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    context: str = "",
    dockerfile: str = "",
) -> list[str]:
    """The changed files that are build inputs, sorted. Raises :class:`PatternError`."""
    names = sorted({f for f in files if f})
    if include:
        spec = Spec(include)
        names = [f for f in names if spec.match(f)]
    else:
        ctx = clean_dir(context)
        if ctx:
            own = {clean_dir(dockerfile)} if clean_dir(dockerfile) else set()
            own |= {f"{d}.dockerignore" for d in own}
            names = [f for f in names if f == ctx or f.startswith(ctx + "/") or f in own]
    if exclude:
        spec = Spec(exclude)
        names = [f for f in names if not spec.match(f)]
    return names


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    include: list[str] = []
    exclude: list[str] = []
    context = dockerfile = ""
    while args:
        flag = args.pop(0)
        if flag not in ("--include", "--exclude", "--context", "--dockerfile") or not args:
            print(
                "usage: python -m bay_reconcile.pushinputs [--include P]... [--exclude P]... "
                "[--context DIR --dockerfile FILE] < changed-files",
                file=sys.stderr,
            )
            return 2
        value = args.pop(0)
        if flag == "--include":
            include.append(value)
        elif flag == "--exclude":
            exclude.append(value)
        elif flag == "--context":
            context = value
        else:
            dockerfile = value
    files = [line.rstrip("\n") for line in sys.stdin]
    try:
        found = input_files(
            files, include=include, exclude=exclude, context=context, dockerfile=dockerfile
        )
    except (PatternError, re.error) as exc:
        print(f"cannot read a build input pattern: {exc}", file=sys.stderr)
        return 2
    for name in found:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
