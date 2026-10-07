"""Where the framework version is read from."""

from pathlib import Path

from bay_cli.errors import BayError


def read_installed_version(bay_dir: Path) -> str | None:
    """Read the installed framework version from ``bay_dir/version.yml``.

    Returns the ``bay_version`` string if present, ``None`` if the file
    is missing or the key is absent.  Raises :class:`BayError` on YAML
    parse failures.
    """
    version_path = bay_dir / "version.yml"
    if not version_path.is_file():
        return None

    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    yaml = YAML()
    try:
        with version_path.open() as fh:
            data = yaml.load(fh)
    except YAMLError as exc:
        raise BayError(f"Failed to parse {version_path}: {exc}") from exc

    if not isinstance(data, dict):
        return None

    value = data.get("bay_version")
    if value is None:
        return None
    return str(value)


def peek_installed_version(bay_dir: Path) -> str | None:
    """The ``bay_version`` of ``bay_dir/version.yml``, read without a YAML parser.

    :func:`read_installed_version` imports ruamel.yaml, which costs about 12 ms.
    Help text is built when ``bay_cli.cli`` is imported, on the path of every
    command, so it reads the one ``bay_version: "x.y.z"`` line instead. Returns
    ``None`` when the file or the line is missing.
    """
    import re

    try:
        text = (bay_dir / "version.yml").read_text()
    except OSError:
        return None
    match = re.search(r"^bay_version:\s*[\"']?([^\"'#\s]+)", text, re.MULTILINE)
    return match.group(1) if match else None
