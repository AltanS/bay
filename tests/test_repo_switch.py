"""A changed build repo: the box builds from the new repo, or fails loud.

An app in the fleet names its repo in ``[build] repo``. Before 2.17.1 a change
of that URL followed by ``bay up`` (``--tags deploy_stack`` only) left the box
on the old repo:

* remote strategy: the checkout is per service and kept its ``origin``, so the
  build server went on building the OLD repo, with no error;
* local strategy: the checkout folder is keyed on repo and branch, so the new
  URL named a folder nobody cloned, and the next push died at the repo lock
  with a bare "Webhook deploy failed".

rebuild.sh now carries the expected repo per service (EXPECTED_ORIGIN) and
the credentials of a fetch (GIT_AUTH). ``_ensure_checkout`` clones a missing
checkout; ``_check_origin`` repoints a checkout whose origin names another
repo, after the new repo answered. Either case marks a full build: no earlier
commit decides a config-only or no-input-change push. A repo that cannot be
used ends in ``_repo_failed``: one build.failed alert that names the service,
the repo (redacted), the branch and the likely cause.

These tests render the real template, cut the real strategy sections out of
it, and run them against temp git repos. ``bay plan`` gets a note for the
change (``repo_change_notes``).

2.17.2 adds the same-URL case: a forced push that leaves the old head outside
the new history. Both strategies fetch and reset (a local build used to pull);
``_note_rewritten_history`` then marks a full build when the old head is not an
ancestor of the new head. A service without a token gets no GIT_ASKPASS, since
Ansible writes the helper only for a token.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from test_rebuild_config import _render_rebuild_sh  # noqa: E402
from test_track_mode import _git, _harness  # noqa: E402

from bay_cli import plan as planmod  # noqa: E402
from bay_cli.reposource import normalize_url, redact_url  # noqa: E402

ROOT = _TESTS_DIR.parent
TOKEN = "ghp_REPO_SWITCH_SENTINEL_7a6b5c"
EMBEDDED = "ghp_EMBEDDED_IN_URL_4d3c2b"
REPO = "git@github.com:example-org/shop.git"

# ── the normal form and the redaction, in Python and in bash ────────────────

NORMAL_FORMS = [
    ("git@github.com:example-org/shop.git", "github.com/example-org/shop"),
    ("ssh://git@github.com/example-org/shop", "github.com/example-org/shop"),
    ("ssh://git@github.com:22/example-org/shop.git", "github.com/example-org/shop"),
    ("https://github.com/example-org/shop.git", "github.com/example-org/shop"),
    ("https://github.com/example-org/shop.git/", "github.com/example-org/shop"),
    ("https://GitHub.COM/Example-Org/Shop", "github.com/example-org/shop"),
    (
        f"https://x-access-token:{TOKEN}@github.com/example-org/shop.git",
        "github.com/example-org/shop",
    ),
    (f"https://{TOKEN}@github.com/example-org/shop", "github.com/example-org/shop"),
    ("  git@github.com:example-org/shop.git  ", "github.com/example-org/shop"),
    ("git@git.example.com:group/sub/api.git", "git.example.com/group/sub/api"),
    ("https://git.example.com:8443/group/api.git", "git.example.com/group/api"),
]


def _bash_fn(rendered: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{.*?^\}}", rendered, flags=re.DOTALL | re.MULTILINE
    )
    assert match is not None, f"{name}() is not in the rendered script"
    return match.group(0)


@pytest.fixture(scope="module")
def local_key_sh() -> str:
    return _render(strategy="local", token=False)


def _bash(rendered: str, fn: str, *args: str) -> str:
    script = _bash_fn(rendered, fn) + f'\n{fn} "$@"\n'
    proc = subprocess.run(
        ["bash", "-c", script, "bash", *args], capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.mark.parametrize(("url", "normal"), NORMAL_FORMS)
def test_normal_form_python_and_bash_agree(local_key_sh: str, url: str, normal: str) -> None:
    assert normalize_url(url) == normal
    assert _bash(local_key_sh, "_repo_norm", url) == normal


def test_normal_form_keeps_two_repos_apart(local_key_sh: str) -> None:
    for a, b in [
        ("git@github.com:example-org/shop.git", "git@github.com:example-org/shop-next.git"),
        ("git@github.com:example-org/shop.git", "git@git.example.com:example-org/shop.git"),
        ("https://github.com/example-org/shop", "https://github.com/other-org/shop"),
    ]:
        assert normalize_url(a) != normalize_url(b)
        assert _bash(local_key_sh, "_repo_norm", a) != _bash(local_key_sh, "_repo_norm", b)


def test_normal_form_of_local_paths(local_key_sh: str, tmp_path: Path) -> None:
    repo = tmp_path / "b.git"
    want = str(repo.resolve())[: -len(".git")]
    for url in (str(repo), f"file://{repo}", f"{repo}/"):
        assert normalize_url(url) == want
        assert _bash(local_key_sh, "_repo_norm", url) == want


REDACTIONS = [
    (
        f"https://x-access-token:{TOKEN}@github.com/o/r.git",
        "https://x-access-token:***@github.com/o/r.git",
    ),
    (f"https://{TOKEN}@github.com/o/r", "https://***@github.com/o/r"),
    (f"HTTP://user:{TOKEN}@host/o/r", "HTTP://user:***@host/o/r"),
    ("git@github.com:o/r.git", "git@github.com:o/r.git"),
    ("ssh://git@github.com/o/r", "ssh://git@github.com/o/r"),
    (
        f"fatal: unable to access 'https://x:{TOKEN}@github.com/o/r/': 403",
        "fatal: unable to access 'https://x:***@github.com/o/r/': 403",
    ),
]


@pytest.mark.parametrize(("text", "want"), REDACTIONS)
def test_redaction_python_and_bash_agree(local_key_sh: str, text: str, want: str) -> None:
    assert redact_url(text) == want
    assert _bash(local_key_sh, "_redact_url", text) == want


# ── the render: four fixtures ───────────────────────────────────────────────


def _services(strategy: str, token: bool, repo: str = REPO) -> dict:
    build: dict = {"repo": repo, "branch": "main", "strategy": strategy}
    if token:
        build["token"] = TOKEN
    svc: dict = {
        "build": build,
        "access": "public",
        "domains": ["shop.example.com"],
        "ports": {"internal": 3000},
    }
    if strategy == "remote":
        svc["image"] = "registry.example.com/example-org/shop:latest"
    return {"shop": svc}


def _render(*, strategy: str, token: bool, repo: str = REPO) -> str:
    return _render_rebuild_sh(
        _services(strategy, token, repo),
        ["shop"],
        git_deploy_services=["shop"] if strategy == "local" else [],
        git_deploy_build_strategy=strategy,
        secrets={"SHOP_GIT_TOKEN": TOKEN} if token else {},
    )


def _block(rendered: str, name: str = "shop") -> str:
    start = rendered.index(f"if [[ \"${{SERVICE}}\" == '{name}' ]]; then")
    end = rendered.index("\nelse\n", start)
    return rendered[start:end]


SLUG = re.compile(r'REPO_DIR="\$\{BUILD_DIR\}/shared/([^/"]+)/repo"')
FIXTURES = {
    ("remote", False): (REPO, "", "ssh"),
    ("remote", True): (
        "https://github.com/example-org/shop.git",
        '"GIT_ASKPASS=/opt/teststack/push-builds/.git-askpass-shop"',
        "token",
    ),
    ("local", False): (
        REPO,
        '"GIT_SSH_COMMAND=ssh -i ${DEPLOY_KEY} -o StrictHostKeyChecking=yes '
        '-o UserKnownHostsFile=/opt/teststack/builds/github_known_hosts"',
        "deploy-key",
    ),
    ("local", True): (
        "https://github.com/example-org/shop.git",
        '"GIT_ASKPASS=/opt/teststack/builds/.git-askpass-{slug}"',
        "token",
    ),
}


@pytest.mark.parametrize(("strategy", "token"), list(FIXTURES))
def test_render_carries_the_expected_repo_and_its_credentials(
    strategy: str, token: bool, tmp_path: Path
) -> None:
    rendered = _render(strategy=strategy, token=token)
    out = tmp_path / "rebuild.sh"
    out.write_text(rendered)
    assert subprocess.run(["bash", "-n", str(out)], capture_output=True).returncode == 0

    block = _block(rendered)
    origin, auth, kind = FIXTURES[(strategy, token)]
    slug = SLUG.search(block).group(1) if strategy == "local" else ""
    assert f'  EXPECTED_ORIGIN="{origin}"' in block
    words = " ".join(["GIT_TERMINAL_PROMPT=0", *([auth.replace("{slug}", slug)] if auth else [])])
    assert f"  GIT_AUTH=({words})" in block
    assert f'  REPO_AUTH="{kind}"' in block
    if strategy == "local" and not token:
        # GIT_AUTH reads DEPLOY_KEY: it must be set first.
        assert block.index("  DEPLOY_KEY=") < block.index("  GIT_AUTH=")

    # The token value is never in the script: the GIT_ASKPASS helper reads it.
    assert TOKEN not in rendered


@pytest.mark.parametrize("strategy", ["remote", "local"])
def test_guard_runs_before_the_lock_and_the_fetch(strategy: str) -> None:
    """Clone a missing checkout, take the repo lock, check origin, fetch, reset, note the history."""
    rendered = _render(strategy=strategy, token=False)
    if strategy == "remote":
        start = rendered.index('if [[ "${BUILD_STRATEGY}" == "remote" ]]; then')
        fetch = rendered.index('_FETCH_STDERR=$(eval "${FETCH_CMD}" 2>&1)', start)
    else:
        start = rendered.index("# ── Local strategy: build and restart on this server ──")
        fetch = rendered.index('git fetch -q origin "${BRANCH}"', start)
    ensure = rendered.index("_ensure_checkout\n", start)
    lock = rendered.index('exec 200>"${REPO_DIR}/.git/rebuild.lock"', start)
    check = rendered.index("_check_origin\n", start)
    full = rendered.index('if [[ "${FULL_BUILD}" == "1" ]]; then', check)
    prev = rendered.index('PREV_COMMIT=$(_previous_commit "${SERVICE}" "${_PREV_HEAD}")', start)
    config_only = rendered.index('if _config_only "${REPO_DIR}" "${PREV_COMMIT}"; then', start)
    assert ensure < lock < check < fetch < config_only
    reset = rendered.index("--hard FETCH_HEAD", fetch)
    note = rendered.index('_note_rewritten_history "${_PREV_HEAD}"', reset)
    assert fetch < reset < note < prev
    assert prev < rendered.index('PREV_COMMIT=""', prev) < config_only
    assert check < full
    # The mark goes once the image of the head exists, before the hold guard.
    done = rendered.index("_full_build_done\n", start)
    hold = rendered.index('HOLD_REASON=$(_hold_reason "${REPO_DIR}")', start)
    assert config_only < done
    assert (done < hold) if strategy == "local" else (done > hold)


def test_embedded_token_reaches_only_the_git_url_assignments() -> None:
    """A token typed into the URL itself never reaches a log, echo or alert line."""
    repo = f"https://x-access-token:{EMBEDDED}@github.com/example-org/shop.git"
    for strategy in ("remote", "local"):
        rendered = _render(strategy=strategy, token=True, repo=repo)
        lines = [ln.strip() for ln in rendered.splitlines() if EMBEDDED in ln]
        assert lines, "the fixture must reach the script"
        for line in lines:
            assert re.match(r"^(EXPECTED_ORIGIN|CLONE_URL|CLONE_REPO)=", line), line


# ── real git: the strategy sections against temp repos ──────────────────────

_EXTRA = (
    "_repo_norm",
    "_redact_url",
    "_full_build_mark",
    "_full_build_pending",
    "_full_build_done",
    "_note_rewritten_history",
    "_repo_failed",
    "_ensure_checkout",
    "_check_origin",
)


def _repo(path: Path, files: dict[str, str], message: str = "init") -> str:
    path.mkdir(parents=True)
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "--short=12", "HEAD")


def _section(rendered: str, strategy: str) -> str:
    """The strategy section as rebuild.sh runs it, up to the build."""
    if strategy == "remote":
        start = rendered.index('if [[ "${BUILD_STRATEGY}" == "remote" ]]; then')
        start = rendered.index("\n", start) + 1
        end = rendered.index("  # Hold guard, on the checkout of the pushed commit", start)
    else:
        start = rendered.index("# ── Local strategy: build and restart on this server ──")
        end = rendered.index("# ── Check if image already exists for this SHA ──", start)
    return rendered[start:end]


def _config(rendered: str) -> str:
    """The one-line assignments of the shop block, ready to run."""
    lines = re.findall(r"^  ([A-Z_]+=.*)$", _block(rendered), flags=re.MULTILINE)
    return "\n".join(ln for ln in lines if not ln.endswith("("))


def _run(
    rendered: str,
    strategy: str,
    tmp_path: Path,
    *,
    checkout: Path,
    expected: str,
    marker: bool = False,
    extra: str = "",
):
    helpers = []
    for name in _EXTRA:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}", rendered, flags=re.DOTALL | re.MULTILINE)
        if match:  # absent from a template before 2.17.1 (the red run)
            helpers.append(match.group(0))
    script = f"""
set -e
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
SERVICE=shop
STATE_FILE="${{STATE_DIR}}/shop.json"
RECEIPT_ENV=production
BUILD_DIR={str(tmp_path / "builds")!r}
REMOTE_BUILD_DIR={str(tmp_path / "push-builds")!r}
{chr(10).join(helpers)}
_deploy_failed() {{ _record_failure "${{SHA}}" "Webhook deploy"; }}
trap '_deploy_failed' ERR
{_config(rendered)}
REPO_DIR={str(checkout)!r}
EXPECTED_ORIGIN={expected!r}
CLONE_URL={expected!r}
CLONE_REPO={expected!r}
NO_INPUT_CHANGE={1 if marker else 0}
{extra}
{_section(rendered, strategy)}
printf 'BUILD %s %s\\n' "${{SERVICE}}" "${{SHA}}"
"""
    return _harness(rendered, script, tmp_path)


def _origin(checkout: Path) -> str:
    return _git(checkout, "remote", "get-url", "origin")


def _head(checkout: Path) -> str:
    return _git(checkout, "rev-parse", "--short=12", "HEAD")


@pytest.fixture
def two_repos(tmp_path: Path) -> tuple[Path, str, Path, str]:
    """Repo A and repo B with unrelated histories and the same tree (a fresh-history copy)."""
    files = {"app.js": "app(1)\n", "Dockerfile": "FROM scratch\n"}
    a = tmp_path / "a.git"
    a_head = _repo(a, files, "first commit of the old repo")
    b = tmp_path / "b.git"
    b_head = _repo(b, files, "first commit of the new repo")
    assert a_head != b_head
    return a, a_head, b, b_head


def _remote_checkout(tmp_path: Path, origin: Path) -> Path:
    checkout = tmp_path / "push-builds" / "shop" / "repo"
    checkout.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--branch", "main", "--single-branch", str(origin), str(checkout))
    return checkout


def test_remote_checkout_follows_a_changed_repo(two_repos, tmp_path: Path) -> None:
    """Checkout of A, expected B (unrelated history): the run builds B's head from B.

    The receiver's no-input-change marker is set and B's tree equals A's, so the
    old commit would have tagged A's image with B's commit: a switch builds.
    """
    a, a_head, b, b_head = two_repos
    checkout = _remote_checkout(tmp_path, a)
    rendered = _render(strategy="remote", token=False)

    proc, _, alerts = _run(
        rendered, "remote", tmp_path, checkout=checkout, expected=str(b), marker=True
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _head(checkout) == b_head
    assert _origin(checkout) == str(b)
    assert f"BUILD shop {b_head}" in proc.stdout
    assert f"repo of shop changed from {a} to {b}, checkout repointed" in proc.stdout
    assert f"full build of shop at {b_head}" in proc.stdout
    assert "no-input-change push" not in proc.stdout and "config-only push" not in proc.stdout
    assert alerts == []
    # The mark stays until the image of the head exists (_full_build_done).
    assert (checkout / ".git" / "bay-full-build" / "shop").is_file()


def test_remote_second_run_after_a_failed_first_build_still_builds_in_full(
    two_repos, tmp_path: Path
) -> None:
    a, _, b, b_head = two_repos
    checkout = _remote_checkout(tmp_path, a)
    rendered = _render(strategy="remote", token=False)
    first, _, _ = _run(rendered, "remote", tmp_path, checkout=checkout, expected=str(b))
    assert first.returncode == 0, first.stderr
    # The first build failed after the repoint: origin is B, the mark is there.
    second, _, _ = _run(
        rendered, "remote", tmp_path, checkout=checkout, expected=str(b), marker=True
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "checkout repointed" not in second.stdout
    assert f"full build of shop at {b_head}" in second.stdout
    assert f"BUILD shop {b_head}" in second.stdout

    # After _full_build_done, a run of the same repo is a normal one again.
    (checkout / ".git" / "bay-full-build" / "shop").unlink()
    third, _, _ = _run(rendered, "remote", tmp_path, checkout=checkout, expected=str(b))
    assert "full build" not in third.stdout


def test_remote_unreachable_new_repo_fails_loud_and_changes_nothing(
    two_repos, tmp_path: Path
) -> None:
    a, a_head, _, _ = two_repos
    checkout = _remote_checkout(tmp_path, a)
    missing = tmp_path / "gone.git"
    rendered = _render(strategy="remote", token=False)

    proc, docker, alerts = _run(
        rendered, "remote", tmp_path, checkout=checkout, expected=str(missing)
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "BUILD" not in proc.stdout
    assert _head(checkout) == a_head
    assert _origin(checkout) == str(a)
    assert not (checkout / ".git" / "bay-full-build").exists()
    assert docker == []
    assert f"repo check failed for shop: cannot use {missing} (branch main)" in proc.stdout
    assert len(alerts) == 1, alerts
    alert = alerts[0]
    assert alert.startswith("build.failed ")
    assert "Repo check failed</b> (1/3)" in alert
    assert "<code>shop</code>" in alert
    assert f"Repo: {missing}" in alert
    assert "Likely cause: the SSH key of user" in alert
    state = json.loads((tmp_path / "state" / "shop.json").read_text())
    assert state["consecutive_failures"] == 1
    assert state["last_failure"]["stage"] == "Repo check"


def test_missing_branch_names_the_branch(two_repos, tmp_path: Path) -> None:
    a, _, b, _ = two_repos
    checkout = _remote_checkout(tmp_path, a)
    rendered = _render(strategy="remote", token=False)
    proc, _, alerts = _run(
        rendered, "remote", tmp_path, checkout=checkout, expected=str(b), extra='BRANCH="release"'
    )
    assert proc.returncode == 1
    assert _origin(checkout) == str(a)
    assert "Likely cause: the branch release does not exist in this repo" in alerts[0]


def test_local_new_folder_is_cloned_and_marked_for_every_service(two_repos, tmp_path: Path) -> None:
    """The local folder is keyed on repo and branch: a new URL names a folder nobody cloned."""
    _, _, b, b_head = two_repos
    checkout = tmp_path / "builds" / "shared" / "shop-main-0000" / "repo"
    rendered = _render(strategy="local", token=False)

    proc, _, alerts = _run(
        rendered,
        "local",
        tmp_path,
        checkout=checkout,
        expected=str(b),
        marker=True,
        extra='CHECKOUT_SERVICES=("shop" "shop-admin")',
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == []
    assert _head(checkout) == b_head and _origin(checkout) == str(b)
    assert f"Cloning {b} (branch main) for shop" in proc.stdout
    assert f"BUILD shop {b_head}" in proc.stdout
    assert "no-input-change push" not in proc.stdout
    marks = checkout / ".git" / "bay-full-build"
    assert sorted(p.name for p in marks.iterdir()) == ["shop", "shop-admin"]
    assert not list(checkout.parent.glob("repo.clone.*"))
    assert oct(checkout.parent.stat().st_mode & 0o777) == "0o700"


def test_local_folder_with_another_origin_moves_without_a_pull(two_repos, tmp_path: Path) -> None:
    """A pull refuses an unrelated history; a full build fetches and resets."""
    a, _, b, b_head = two_repos
    checkout = tmp_path / "builds" / "shared" / "shop-main-0000" / "repo"
    checkout.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--branch", "main", "--single-branch", str(a), str(checkout))
    rendered = _render(strategy="local", token=False)

    proc, _, _ = _run(rendered, "local", tmp_path, checkout=checkout, expected=str(b))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _head(checkout) == b_head and _origin(checkout) == str(b)
    assert "checkout repointed" in proc.stdout
    assert f"BUILD shop {b_head}" in proc.stdout


# ── the same URL with a replaced history (2.17.2) ───────────────────────────

FILES = {"app.js": "app(1)\n", "Dockerfile": "FROM scratch\n"}


def _rewrite_history(repo: Path) -> str:
    """A forced push with an unrelated root commit: same tree, no shared commit."""
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the same files under a fresh root commit")
    _git(repo, "branch", "-q", "-M", "rewritten", "main")
    return _git(repo, "rev-parse", "--short=12", "HEAD")


def _push_commit(repo: Path) -> str:
    (repo / "app.js").write_text("app(2)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a normal push")
    return _git(repo, "rev-parse", "--short=12", "HEAD")


def _local_checkout(tmp_path: Path, origin: Path) -> Path:
    checkout = tmp_path / "builds" / "shared" / "shop-main-0000" / "repo"
    checkout.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--branch", "main", "--single-branch", str(origin), str(checkout))
    return checkout


def _rewritten_line(old: str, new: str) -> str:
    return f"history of shop was rewritten ({old} is not in {new}), full build"


@pytest.mark.parametrize("marker", [True, False])
def test_local_same_url_rewritten_history_fetches_and_builds_in_full(
    tmp_path: Path, marker: bool
) -> None:
    """The red test of 2.17.2: a pull stopped with exit 128 on an unrelated root commit."""
    origin = tmp_path / "origin.git"
    old = _repo(origin, FILES, "first commit")
    checkout = _local_checkout(tmp_path, origin)
    new = _rewrite_history(origin)
    assert old != new
    rendered = _render(strategy="local", token=False)

    proc, _, alerts = _run(
        rendered, "local", tmp_path, checkout=checkout, expected=str(origin), marker=marker
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == []
    assert _head(checkout) == new
    assert f"BUILD shop {new}" in proc.stdout
    assert _rewritten_line(old, new) in proc.stdout
    assert f"full build of shop at {new}" in proc.stdout
    assert "no-input-change push" not in proc.stdout and "config-only push" not in proc.stdout
    assert "checkout repointed" not in proc.stdout
    assert (checkout / ".git" / "bay-full-build" / "shop").is_file()


def test_local_rewritten_history_marks_every_service_of_the_checkout(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _repo(origin, FILES, "first commit")
    checkout = _local_checkout(tmp_path, origin)
    _rewrite_history(origin)
    proc, _, _ = _run(
        _render(strategy="local", token=False),
        "local",
        tmp_path,
        checkout=checkout,
        expected=str(origin),
        extra='CHECKOUT_SERVICES=("shop" "shop-admin")',
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    marks = checkout / ".git" / "bay-full-build"
    assert sorted(p.name for p in marks.iterdir()) == ["shop", "shop-admin"]


def test_local_fast_forward_is_unchanged(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _repo(origin, FILES, "first commit")
    checkout = _local_checkout(tmp_path, origin)
    new = _push_commit(origin)
    proc, _, alerts = _run(
        _render(strategy="local", token=False),
        "local",
        tmp_path,
        checkout=checkout,
        expected=str(origin),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == []
    assert _head(checkout) == new
    assert f"BUILD shop {new}" in proc.stdout
    assert "rewritten" not in proc.stdout and "full build" not in proc.stdout
    assert not (checkout / ".git" / "bay-full-build").exists()


def test_local_fetch_failure_is_the_loud_repo_failure(tmp_path: Path) -> None:
    """A fetch that fails ends in _repo_failed (one alert, the likely cause), not the ERR trap."""
    origin = tmp_path / "origin.git"
    old = _repo(origin, FILES, "first commit")
    checkout = _local_checkout(tmp_path, origin)
    shutil.rmtree(origin)
    proc, _, alerts = _run(
        _render(strategy="local", token=False),
        "local",
        tmp_path,
        checkout=checkout,
        expected=str(origin),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "BUILD" not in proc.stdout
    assert _head(checkout) == old
    assert len(alerts) == 1 and "Repo fetch failed</b> (1/3)" in alerts[0]
    assert "Webhook deploy" not in alerts[0]
    state = json.loads((tmp_path / "state" / "shop.json").read_text())
    assert state["last_failure"]["stage"] == "Repo fetch"


@pytest.mark.parametrize("marker", [True, False])
def test_remote_same_url_rewritten_history_builds_in_full(tmp_path: Path, marker: bool) -> None:
    """The remote path resets to FETCH_HEAD; it now also marks a rewrite as a full build.

    Before 2.17.2 it built only because nothing used the old commit: the receiver writes no
    marker for a force push, and the old commit stays in the object store after the reset.
    With the marker set (a trigger file) a same-tree rewrite would have been a no-input-change push.
    """
    origin = tmp_path / "origin.git"
    old = _repo(origin, FILES, "first commit")
    checkout = _remote_checkout(tmp_path, origin)
    new = _rewrite_history(origin)
    rendered = _render(strategy="remote", token=False)

    proc, _, alerts = _run(
        rendered, "remote", tmp_path, checkout=checkout, expected=str(origin), marker=marker
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == []
    assert _head(checkout) == new
    assert _rewritten_line(old, new) in proc.stdout
    assert f"full build of shop at {new}" in proc.stdout
    assert f"BUILD shop {new}" in proc.stdout
    assert "no-input-change push" not in proc.stdout and "config-only push" not in proc.stdout
    assert (checkout / ".git" / "bay-full-build" / "shop").is_file()


def test_remote_fast_forward_is_unchanged(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _repo(origin, FILES, "first commit")
    checkout = _remote_checkout(tmp_path, origin)
    new = _push_commit(origin)
    proc, _, alerts = _run(
        _render(strategy="remote", token=False),
        "remote",
        tmp_path,
        checkout=checkout,
        expected=str(origin),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == []
    assert _head(checkout) == new
    assert "rewritten" not in proc.stdout and "full build" not in proc.stdout
    assert not (checkout / ".git" / "bay-full-build").exists()


def test_rewritten_history_rule_in_bash(tmp_path: Path) -> None:
    """An ancestor is no rewrite; a missing old head is; no old head and a full build are not."""
    rendered = _render(strategy="local", token=False)
    repo = tmp_path / "co"
    first = _repo(repo, {"a": "1\n"})
    (repo / "a").write_text("2\n")
    _git(repo, "commit", "-q", "-am", "second")
    helpers = "\n".join(_bash_fn(rendered, n) for n in _EXTRA if n in rendered)

    def run(old: str, full: str = "0") -> tuple[str, str]:
        script = f"""
set -e
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
SERVICE=shop
CHECKOUT_SERVICES=()
REPO_DIR={str(repo)!r}
FULL_BUILD={full}
FULL_BUILD_WHY=initial
{helpers}
_note_rewritten_history {old!r}
printf 'FULL_BUILD=%s WHY=%s\\n' "${{FULL_BUILD}}" "${{FULL_BUILD_WHY}}"
"""
        proc, _, _ = _harness(rendered, script, tmp_path)
        assert proc.returncode == 0, proc.stderr
        shutil.rmtree(repo / ".git" / "bay-full-build", ignore_errors=True)
        return proc.stdout, proc.stderr

    assert "FULL_BUILD=0 WHY=initial" in run(first)[0]
    assert "FULL_BUILD=0 WHY=initial" in run("")[0]
    gone = "0123456789ab"
    out = run(gone)[0]
    assert "FULL_BUILD=1" in out and "WHY=the history of the deploy branch was rewritten" in out
    assert f"history of shop was rewritten ({gone} is not in " in out
    # A run that already builds in full says nothing more.
    out = run(gone, full="1")[0]
    assert "rewritten (" not in out and "WHY=initial" in out


# ── a service without a token has no GIT_ASKPASS (2.17.2) ───────────────────


@pytest.mark.parametrize("strategy", ["remote", "local"])
def test_no_token_service_renders_no_askpass(strategy: str) -> None:
    """Ansible writes the helper only for a token: a service without one must not name it."""
    rendered = _render(strategy=strategy, token=False)
    block = _block(rendered)
    assert "GIT_ASKPASS" not in block and ".git-askpass" not in block
    assert "GIT_TERMINAL_PROMPT=0" in block
    if strategy == "remote":
        assert (
            '  FETCH_CMD="GIT_TERMINAL_PROMPT=0 git -c url.\\"\\${CLONE_URL}\\".insteadOf='
            '\\"\\${CLONE_REPO}\\" fetch origin \\"\\${BRANCH}\\""'
        ) in block
        assert "  GIT_AUTH=(GIT_TERMINAL_PROMPT=0)" in block


def test_token_service_keeps_its_askpass_lines() -> None:
    """These lines were byte-compared against the 2.17.1 render; they must not move."""
    remote = _block(_render(strategy="remote", token=True))
    assert (
        '  FETCH_CMD="GIT_ASKPASS=/opt/teststack/push-builds/.git-askpass-shop GIT_TERMINAL_PROMPT=0 '
        'git -c url.\\"\\${CLONE_URL}\\".insteadOf=\\"\\${CLONE_REPO}\\" fetch origin '
        '\\"\\${BRANCH}\\""'
    ) in remote
    assert (
        '  GIT_AUTH=(GIT_TERMINAL_PROMPT=0 "GIT_ASKPASS=/opt/teststack/push-builds/.git-askpass-shop")'
    ) in remote
    local = _block(_render(strategy="local", token=True))
    slug = SLUG.search(local).group(1)
    assert (
        f'  GIT_AUTH=(GIT_TERMINAL_PROMPT=0 "GIT_ASKPASS=/opt/teststack/builds/.git-askpass-{slug}")'
    ) in local


@contextmanager
def _answers_401():
    """A git server that wants a login: every request gets 401."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="repo"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/example-org/shop.git"
    finally:
        server.shutdown()
        server.server_close()


def test_remote_no_token_repo_that_wants_a_login_fails_with_gits_own_message(
    tmp_path: Path,
) -> None:
    """A 401 made git exec the missing helper: 'cannot exec ... .git-askpass-shop'."""
    origin = tmp_path / "origin.git"
    old = _repo(origin, FILES, "first commit")
    checkout = _remote_checkout(tmp_path, origin)
    with _answers_401() as url:
        _git(checkout, "remote", "set-url", "origin", url)
        proc, _, alerts = _run(
            _render(strategy="remote", token=False),
            "remote",
            tmp_path,
            checkout=checkout,
            expected=url,
            # A desktop session exports SSH_ASKPASS, which git falls back to: keep it out.
            extra="unset SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY",
        )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _head(checkout) == old
    assert len(alerts) == 1 and "Repo fetch failed</b> (1/3)" in alerts[0]
    seen = proc.stdout + proc.stderr + alerts[0]
    assert "cannot exec" not in seen and ".git-askpass" not in seen
    assert "terminal prompts disabled" in alerts[0]


@pytest.mark.parametrize("key_present", [False, True])
def test_local_clone_failure_fails_loud_and_leaves_nothing(
    tmp_path: Path, key_present: bool
) -> None:
    checkout = tmp_path / "builds" / "shared" / "shop-main-0000" / "repo"
    missing = tmp_path / "gone.git"
    if key_present:
        (tmp_path / "builds" / "shop").mkdir(parents=True)
        (tmp_path / "builds" / "shop" / ".deploy_key").write_text("key\n")
    rendered = _render(strategy="local", token=False)

    proc, _, alerts = _run(rendered, "local", tmp_path, checkout=checkout, expected=str(missing))

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "BUILD" not in proc.stdout
    assert not checkout.exists()
    assert not list(checkout.parent.glob("repo.clone.*"))
    assert len(alerts) == 1 and "Repo clone failed</b> (1/3)" in alerts[0]
    if key_present:
        assert "is not on this repo; GitHub takes a key on one repo only" in alerts[0]
    else:
        assert "run bay deploy production --tags git_deploy to make it" in alerts[0]


def test_embedded_token_is_redacted_in_logs_alerts_and_state(two_repos, tmp_path: Path) -> None:
    a, _, _, _ = two_repos
    checkout = _remote_checkout(tmp_path, a)
    url = f"https://x-access-token:{EMBEDDED}@127.0.0.1:1/example-org/shop.git"
    rendered = _render(strategy="remote", token=True)

    proc, _, alerts = _run(rendered, "remote", tmp_path, checkout=checkout, expected=url)

    assert proc.returncode == 1
    seen = (
        proc.stdout
        + proc.stderr
        + "\n".join(alerts)
        + (tmp_path / "state" / "shop.json").read_text()
    )
    assert EMBEDDED not in seen
    assert "https://x-access-token:***@127.0.0.1:1/example-org/shop.git" in proc.stdout
    assert "Likely cause: the token in vault key SHOP_GIT_TOKEN cannot read this repo" in alerts[0]
    assert _origin(checkout) == str(a)


def test_same_repo_in_another_form_only_sets_origin(tmp_path: Path) -> None:
    """A trailing .git or a token in the old origin is no switch: no full build."""
    rendered = _render(strategy="local", token=False)
    checkout = tmp_path / "co"
    _repo(checkout, {"a": "1\n"})
    _git(
        checkout,
        "remote",
        "add",
        "origin",
        f"https://x-access-token:{EMBEDDED}@github.com/example-org/shop",
    )
    script = f"""
set -e
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
SERVICE=shop
CHECKOUT_SERVICES=()
REPO_DIR={str(checkout)!r}
EXPECTED_ORIGIN="https://github.com/example-org/shop.git"
{chr(10).join(_bash_fn(rendered, n) for n in _EXTRA)}
_check_origin
printf 'FULL_BUILD=%s\\n' "${{FULL_BUILD}}"
"""
    proc, _, alerts = _harness(rendered, script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "FULL_BUILD=0" in proc.stdout
    assert "(the same repo)" in proc.stdout and EMBEDDED not in proc.stdout
    assert _origin(checkout) == "https://github.com/example-org/shop.git"
    assert alerts == []


# ── bay plan: a note for the change ─────────────────────────────────────────


def _compiled(repo: str, *, token: str | None = None, strategy: str | None = None) -> dict:
    build: dict = {"repo": repo, "branch": "main"}
    if token:
        build["token"] = token
    if strategy:
        build["strategy"] = strategy
    return {"services": {"shop": {"build": build, "domains": ["shop.example.com"]}}}


def _step() -> list[dict]:
    return [{"container": "shop", "action": "update", "risk": "safe", "what": "changed: build"}]


def test_plan_note_for_a_changed_repo() -> None:
    notes = planmod.repo_change_notes(
        _compiled("git@github.com:example-org/shop.git"),
        _compiled("git@github.com:example-org/shop-next.git"),
        _step(),
    )
    assert notes == [
        "shop: the build repo changes from git@github.com:example-org/shop.git to "
        "git@github.com:example-org/shop-next.git. The box builds from the new repo from the "
        "next push. Add the box's deploy key to the new repo and create its push webhook first."
    ]


def test_plan_note_absent_when_the_repo_does_not_change() -> None:
    for old, new in [
        ("git@github.com:example-org/shop.git", "git@github.com:example-org/shop.git"),
        ("git@github.com:example-org/shop.git", "https://github.com/example-org/shop"),
    ]:
        assert planmod.repo_change_notes(_compiled(old), _compiled(new), _step()) == []
    # A changed repo of a container with no step (not in this plan) has no note.
    assert (
        planmod.repo_change_notes(
            _compiled("git@github.com:o/a.git"), _compiled("git@github.com:o/b.git"), []
        )
        == []
    )
    # Bay does not clone the repo of a registry build.
    assert (
        planmod.repo_change_notes(
            _compiled("git@github.com:o/a.git"),
            _compiled("git@github.com:o/b.git", strategy="registry"),
            _step(),
        )
        == []
    )


def test_plan_note_redacts_a_token_and_names_the_token_key() -> None:
    notes = planmod.repo_change_notes(
        _compiled(f"https://x-access-token:{EMBEDDED}@github.com/example-org/shop.git"),
        _compiled(
            "https://github.com/example-org/shop-next.git",
            token="{{ secrets.SHOP_GIT_TOKEN }}",
            strategy="remote",
        ),
        _step(),
    )
    assert len(notes) == 1
    assert EMBEDDED not in notes[0]
    assert "from https://x-access-token:***@github.com/example-org/shop.git to" in notes[0]
    assert (
        "The build server builds from the new repo from the next push or trigger file; "
        "the deployment box recreates the container when it gets the pull signal. "
        "Give the token SHOP_GIT_TOKEN"
    ) in notes[0]
    assert "Give the token SHOP_GIT_TOKEN read access to the new repo" in notes[0]


def test_plan_notes_are_not_hashed() -> None:
    assert "notes" in planmod._UNHASHED
    body = {"steps": _step(), "notes": []}
    assert planmod.body_sha256(body) == planmod.body_sha256({**body, "notes": ["shop: x"]})


def test_compile_and_diff_adds_the_note() -> None:
    """Every plan kind (project, env, remove) diffs through _compile_and_diff."""
    src = (ROOT / "src" / "bay_cli" / "plan.py").read_text()
    body = src[src.index("def _compile_and_diff(") : src.index("def _finish(")]
    assert "notes.extend(repo_change_notes(current, out.wanted_data, out.steps))" in body


# ── the local token helper is rendered under deploy_stack too ──────────────


def test_local_askpass_rendered_by_the_deploy_stack_tasks(tmp_path: Path) -> None:
    """The slug of a local checkout changes with the repo: its helper must be there after bay up."""
    tasks = yaml.safe_load((ROOT / "roles/git_deploy/tasks/render_rebuild_script.yml").read_text())
    outer = next(t for t in tasks if "block" in t)
    wanted = [
        t
        for t in outer["block"]
        if t["name"].startswith(
            (
                "Ensure the build directory exists for the local credential helpers",
                "Install the git credential helper of each local build with a token",
            )
        )
    ]
    assert len(wanted) == 2
    for task in wanted:
        module = (
            "ansible.builtin.template"
            if "ansible.builtin.template" in task
            else "ansible.builtin.file"
        )
        task[module].pop("owner", None)
        if module == "ansible.builtin.template":
            task[module]["src"] = str(ROOT / "roles/git_deploy/templates/git-askpass.sh.j2")
    build_dir = tmp_path / "builds"
    services = {
        "shop": {"build": {"repo": "git@github.com:example-org/shop.git", "token": TOKEN}},
        "plain": {"build": {"repo": "git@github.com:example-org/plain.git"}},
    }
    extra = {
        "app_user": "deploy",
        "git_deploy_build_dir": str(build_dir),
        "services": services,
        "git_deploy_services": ["shop", "plain"],
    }
    (tmp_path / "vars.json").write_text(json.dumps(extra))
    (tmp_path / "ansible.cfg").write_text(
        f"[defaults]\nfilter_plugins = {ROOT / 'filter_plugins'}\n"
    )
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "tasks": [{"name": "block", "block": wanted, "vars": outer["vars"]}],
                }
            ]
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "0",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "0",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ansible.cli.playbook",
            "-i",
            "localhost,",
            "-e",
            f"@{tmp_path / 'vars.json'}",
            str(tmp_path / "play.yml"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert TOKEN not in proc.stdout + proc.stderr, "no_log leaked the token"

    sys.path.insert(0, str(ROOT / "filter_plugins"))
    from bay_filters import bay_repo_slug

    helper = build_dir / f".git-askpass-{bay_repo_slug(services['shop']['build']['repo'], 'main')}"
    assert helper.is_file() and TOKEN in helper.read_text()
    assert oct(helper.stat().st_mode & 0o777) == "0o700"
    assert [p.name for p in build_dir.iterdir()] == [helper.name]
    # The path is the one rebuild.sh reads for this service.
    rendered = _render(strategy="local", token=True)
    assert f'"GIT_ASKPASS=/opt/teststack/builds/{helper.name}"' in _block(rendered)
