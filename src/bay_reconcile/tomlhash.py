"""The canonical hash of a ``bay.toml``: one implementation for the CLI and the box.

``bay compile`` writes ``build.bay_toml_hash`` per project into the services
file. The build side (``rebuild.sh``, on the box or the build server) computes
the same hash from the ``bay.toml`` at the pushed commit and compares the two.
Equal: the push changes code only, the build deploys. Different: the push
changes config too, so the build is held until ``bay up`` releases it
(``docs/build-pipeline.md``, "Hold guard").

The hash is the SHA-256 of the PARSED file, re-serialised as JSON with sorted
keys and compact separators. A comment, blank line, key order or quoting
change gives the same hash; a value change gives another one.

Stdlib only, like the rest of the package: it runs on the box. ``tomllib`` is
Python 3.11+. On an older box ``tomli`` is used when it is installed; with
neither, the CLI entry point exits 3 and the caller treats the hash as unknown
(``rebuild.sh`` holds the build, never deploys it blind).

``python -m bay_reconcile.tomlhash <file>`` prints the hash. Exit codes: 0 the
hash is on stdout, 2 the file is missing or unreadable, 3 no TOML parser, 4 the
file is not valid TOML.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sys
from collections.abc import Sequence
from typing import Any

try:  # pragma: no cover - which branch runs depends on the interpreter
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore[no-redef,import-not-found,unused-ignore]
    except ModuleNotFoundError:
        _toml = None  # type: ignore[assignment]


class NoTomlParser(RuntimeError):
    """Neither ``tomllib`` nor ``tomli`` is importable on this interpreter."""


def _plain(value: Any) -> Any:
    """TOML dates and times are not JSON; use their ISO text."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, _dt.date | _dt.time):
        return value.isoformat()
    return value


def canonical_json(doc: Any) -> str:
    """The parsed document as compact JSON with sorted keys."""
    return json.dumps(_plain(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_doc(doc: Any) -> str:
    """``sha256:<hex>`` of an already parsed document."""
    digest = hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_hash(data: bytes) -> str:
    """``sha256:<hex>`` of a ``bay.toml`` given as bytes.

    Raises :class:`NoTomlParser` when the interpreter has no TOML parser, and
    ``ValueError`` (``tomllib.TOMLDecodeError`` is one) on invalid TOML.
    """
    if _toml is None:
        raise NoTomlParser("no TOML parser: python3 >= 3.11 or the tomli package is needed")
    doc = _toml.loads(data.decode("utf-8"))
    return hash_doc(doc)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m bay_reconcile.tomlhash <bay.toml>", file=sys.stderr)
        return 2
    try:
        with open(args[0], "rb") as handle:
            data = handle.read()
    except OSError as exc:
        print(f"cannot read {args[0]}: {exc.strerror}", file=sys.stderr)
        return 2
    try:
        print(canonical_hash(data))
    except NoTomlParser as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"{args[0]} is not valid TOML: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
