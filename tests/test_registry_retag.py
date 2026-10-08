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
# One `imagetools create` command up to the end of its line.
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


def _violations(text: str) -> list[str]:
    found = []
    for match in _CREATE.finditer(text):
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
        seen += len(_CREATE.findall(text))
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
