"""Keep the two `headscale_version` pins from drifting apart.

The value is declared twice, and only one of the two decides which image runs:

  * ``roles/access_gateway/defaults/main.yml`` — AUTHORITATIVE.
    ``roles/container_lifecycle/tasks/build_specs.yml`` builds the headscale
    container spec from it, and the reconciler is the only thing that creates
    the container.
  * ``roles/headscale/defaults/main.yml`` — read by the headscale role's own
    tasks (config rendering, policy validation). Never reaches the image tag.

Bay 0.6.8 bumped the second one alone. Every check passed, the release was
tagged, the consumer pinned it, `bay deploy` ran clean and reported no change —
and the server stayed on the old version, because nothing that mattered had
changed. A duplicated constant fails silently in exactly one direction, so the
guard has to be a test rather than a comment.

The docker-compose template reads the same variable, but it is documentation
only (`deploy_stack/tasks/main.yml` says so); it is not what runs the container.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

AUTHORITATIVE = REPO / "roles/access_gateway/defaults/main.yml"
SECONDARY = REPO / "roles/headscale/defaults/main.yml"

_PIN = re.compile(r'^headscale_version:\s*"([^"]+)"\s*$', re.MULTILINE)


def _pin(path: Path) -> str:
    found = _PIN.findall(path.read_text(encoding="utf-8"))
    assert found, f"no `headscale_version:` pin found in {path.relative_to(REPO)}"
    assert len(found) == 1, (
        f"{path.relative_to(REPO)} declares headscale_version {len(found)} times: {found}"
    )
    return found[0]


def test_both_pins_agree():
    authoritative = _pin(AUTHORITATIVE)
    secondary = _pin(SECONDARY)
    assert authoritative == secondary, (
        f"headscale_version has drifted: "
        f"{AUTHORITATIVE.relative_to(REPO)} pins {authoritative!r} but "
        f"{SECONDARY.relative_to(REPO)} pins {secondary!r}. "
        f"The first one decides the running image. Bump both."
    )


def test_reconciler_spec_reads_the_variable():
    """The bug is only silent while the spec is built from the variable.

    If the image tag is ever inlined into build_specs.yml, the pin above stops
    being load-bearing and this whole guard is theatre. Fail loudly instead.
    """
    specs = (REPO / "roles/container_lifecycle/tasks/build_specs.yml").read_text(
        encoding="utf-8"
    )
    assert 'image: "headscale/headscale:{{ headscale_version }}"' in specs, (
        "build_specs.yml no longer builds the headscale image tag from "
        "`headscale_version` — re-point test_both_pins_agree at whatever "
        "decides the image now."
    )


@pytest.mark.parametrize("path", [AUTHORITATIVE, SECONDARY])
def test_pin_is_a_plain_version(path: Path):
    assert re.fullmatch(r"\d+\.\d+\.\d+", _pin(path)), (
        f"{path.relative_to(REPO)} should pin an exact headscale version, "
        f"never a floating tag like `latest`."
    )
