"""A registry retag copies the manifest; it never re-wraps it (M120/01).

``docker buildx imagetools create`` with one source defaults to
``--prefer-index=true``. That wraps a plain manifest in a new image index. The
child is the same, but the top-level digest is new. On a box with the
containerd image store the image ID is the top-level digest, so the reconciler
reads the retagged image as a new image and recreates the container.
``--prefer-index=false`` makes the retag a carbon copy with the same digest.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLES = ROOT / "roles"
FLAG = "--prefer-index=false"
# A shell line continuation: a backslash right before the newline.
_CONTINUATION = re.compile(r"\\[ \t]*\r?\n")
# One `imagetools create` command up to the end of its (joined) line.
_CREATE = re.compile(r"imagetools\s+create\b(?P<args>[^\n]*)")
# Options of `imagetools create` that take a value.
_VALUE_OPTS = {"-t", "--tag", "-f", "--file", "--annotation", "--builder"}


def _sources(args: str) -> list[str]:
    """Positional arguments (the sources) of one `imagetools create` line."""
    try:
        tokens = shlex.split(args, posix=True)
    except ValueError:
        tokens = args.split()
    sources: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
        elif token in _VALUE_OPTS:
            skip = True
        elif token.startswith("-"):
            continue
        elif token[0] in "<>&|;" or token.startswith(("2>", "&>")):
            break  # a redirection or a pipe ends the command
        else:
            sources.append(token)
    return sources


def _join(text: str) -> str:
    """``text`` with backslash-continued lines joined, as the shell reads them.

    Without this a `imagetools create \\` that puts its flags and source on
    the next lines would show the guard no source and slip through.
    """
    return _CONTINUATION.sub(" ", text)


def _violations(text: str) -> list[str]:
    found = []
    for match in _CREATE.finditer(_join(text)):
        args = match.group("args")
        if len(_sources(args)) == 1 and FLAG not in args:
            found.append(match.group(0).strip())
    return found


def test_registry_retag_is_carbon_copy():
    """Every single-source `imagetools create` under roles/ is a carbon copy."""
    seen = 0
    bad = []
    for path in sorted(ROLES.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        seen += len(_CREATE.findall(_join(text)))
        bad += [f"{path.relative_to(ROOT)}: {line}" for line in _violations(text)]
    assert seen >= 3, "the guard no longer finds the known retag sites"
    assert not bad, "single-source imagetools create without " + FLAG + ":\n" + "\n".join(bad)


def test_registry_retag_guard_catches_a_missing_flag():
    """Prove the guard goes red: the bug shapes are found, the safe shapes pass."""
    assert _violations('docker buildx imagetools create -t "${R}:a" "${S}"')
    assert _violations('docker buildx imagetools create -t a -t b "${S}" >/dev/null 2>&1')
    assert not _violations(f'docker buildx imagetools create {FLAG} -t "${{R}}:a" "${{S}}"')
    assert not _violations(f'imagetools create -t a "${{S}}" {FLAG} >/dev/null 2>&1')
    # Several sources merge into one index on purpose: out of scope.
    assert not _violations("docker buildx imagetools create -t a src1 src2")


def test_registry_retag_guard_joins_continued_lines():
    """A multi-line `imagetools create \\` cannot escape the guard."""
    multi = 'docker buildx imagetools create \\\n  -t "${R}:a" \\\n  "${S}" >/dev/null\n'
    assert _violations(multi), "a continued command without the flag must go red"
    # The shape the unjoined guard missed: the trailing `\\` read as a second
    # source, so one real source looked like two.
    tail = 'docker buildx imagetools create -t "${R}:a" "${S}" \\\n  >/dev/null 2>&1\n'
    assert _violations(tail)
    # Trailing blanks after the backslash are tolerated by the join, too.
    assert _violations('imagetools create \\  \n -t a \\\n "${S}"\n')
    # The flag on a continued line counts.
    ok = f'docker buildx imagetools create \\\n  {FLAG} \\\n  -t "${{R}}:a" "${{S}}"\n'
    assert not _violations(ok)
    # Two separate commands stay two: a plain newline does not join.
    two = f'imagetools create {FLAG} -t a "${{S}}"\nimagetools create -t b "${{S}}"\n'
    assert len(_violations(two)) == 1
