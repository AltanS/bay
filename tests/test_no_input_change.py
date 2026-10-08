"""Gap 39: a push that changes none of a project's build inputs tags its image with the head.

Two projects build from one app repo and branch, each with its own `watch`. A
push that changes only one of them used to be skipped for the other by the
receiver, so the other had no image for the new head, and `bay up` (which pins
every project of the repo at its head) kept noting `kept <project>`.

Three layers:

* the webhook receiver (`app.py`): such a push passes with the trigger marker
  `no_input_change` instead of being skipped;
* `rebuild.sh`: it never trusts the marker, diffs the previous commit against
  the pushed one itself, and runs the config-only tag path (no build, no
  recreate) or, when it finds an input change, builds;
* `bay_reconcile.pushinputs`: the box-side matcher, checked against the
  receiver's `pathspec` rule.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pathspec
import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
for _extra in (
    _TESTS_DIR.parent / "roles" / "git_deploy" / "files" / "webhook",
    _TESTS_DIR.parent / "filter_plugins",
):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import app as webhook_app  # noqa: E402
from app import NO_INPUT_CHANGE, input_filter, push_filter  # noqa: E402
from bay_filters import bay_build_context  # noqa: E402
from test_rebuild_config import _render_rebuild_sh, _render_webhook_config  # noqa: E402
from test_track_mode import (  # noqa: E402
    _calls,
    _commit,
    _git,
    _harness,
    _registry_docker,
    _service_config,
    _shared_repo,
    _shared_services,
)

from bay_reconcile import pushinputs  # noqa: E402

TAGGED = ": tagged, run bay up"
NAMES = ["web", "admin", "web-next", "blog"]
WATCH = {"web": ["apps/web/**"], "admin": ["apps/admin/**"]}


def _watched(strategy: str) -> dict:
    """The gap-30 fixture (web and admin share one repo) with a `watch` each."""
    services = _shared_services(strategy)
    for name, include in WATCH.items():
        services[name]["build"]["paths"] = {"include": include}
    return services


def _render(strategy: str, services: dict | None = None) -> str:
    return _render_rebuild_sh(
        services or _watched(strategy), NAMES, git_deploy_services=NAMES,
        git_deploy_build_strategy=strategy,
    )


def _site(rendered: str, strategy: str) -> str:
    """The decision sites of one strategy, cut from the rendered script itself.

    From the previous commit up to the build: the config-only site and the
    no-input-change site, exactly as rebuild.sh runs them.
    """
    prev = 'PREV_COMMIT=$(_previous_commit "${SERVICE}" "${_PREV_HEAD}")'
    if strategy == "remote":
        start = rendered.index(prev)
        end = rendered.index('HOLD_REASON=$(_hold_reason "${REPO_DIR}")', start)
    else:
        start = rendered.index(prev, rendered.index("# ── Local strategy"))
        end = rendered.index("# ── Check if image already exists", start)
    return rendered[start:end]


def _run(
    rendered: str, strategy: str, name: str, checkout: Path, tmp_path: Path,
    *, marker: bool, extra: str = "",
) -> subprocess.CompletedProcess[str]:
    """One run of ``name``: config block, fetch or pull, the real decision sites."""
    log, refs = tmp_path / "docker.log", tmp_path / "refs"
    fetch = (
        '_PREV_HEAD=$(git rev-parse --short=12 HEAD 2>/dev/null || true)\n'
        "git fetch -q origin main && git reset -q --hard FETCH_HEAD"
        if strategy == "remote" else
        '_PREV_HEAD=$(_seen_head "${REPO_DIR}")\n'
        'git pull -q --ff-only origin main\n_mark_seen "${REPO_DIR}"'
    )
    script = f"""
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
{_registry_docker(log, refs)}
_log() {{ echo "[rebuild] [${{CORR_ID:-no-corr}}] $*"; }}
SERVICE={name!r}
CORR_ID="corr-{name}"
{_service_config(rendered, name)}
NO_INPUT_CHANGE={1 if marker else 0}
REPO_DIR={str(checkout)!r}
{extra}
cd "${{REPO_DIR}}"
{fetch}
SHA=$(git rev-parse --short=12 HEAD)
{_site(rendered, strategy)}
printf 'BUILD %s\\n' "${{SERVICE}}"
"""
    proc, _, alerts = _harness(rendered, script, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert alerts == [], "a tag-only push and its fallback send no alert here"
    return proc


def _tags(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("tag ") or "imagetools create" in c]


# ── the contract ────────────────────────────────────────────────────────


def test_no_input_change_push_tags_head(tmp_path: Path) -> None:
    """A push of only admin's code tags web's image with the pushed commit: the
    receiver passes it as no_input_change, rebuild.sh checks the diff and tags.
    No build, no `:latest` move, no recreate. Remote and local strategy."""
    # ── the receiver: web gets the marker, admin a normal trigger ──
    config = _render_webhook_config(_watched("remote"))
    assert config["web"]["paths"] == {"include": ["apps/web/**"]}
    assert push_filter({"apps/admin/main.js"}, config["web"])[0] is False
    assert push_filter({"apps/admin/main.js"}, config["admin"])[0] is True
    answer, trigger = _post(config["web"], ["apps/admin/main.js"], tmp_path / "rx")
    assert answer["status"] == "triggered" and answer["mode"] == NO_INPUT_CHANGE, answer
    lines = trigger.read_text().splitlines()
    assert lines == [answer["corr_id"], NO_INPUT_CHANGE]

    # ── remote: one checkout per service, tags in the registry ──
    remote_sh = _render("remote")
    tmp_r = tmp_path / "remote"
    tmp_r.mkdir()
    origin, first = _shared_repo(tmp_r)
    for name in ("web", "admin"):
        _git(tmp_r, "clone", "-q", str(origin), str(tmp_r / name))
    refs = tmp_r / "refs"
    refs.write_text(f"zot.example.com/demo/web:{first}\nzot.example.com/demo/admin:{first}\n")
    pushed = _commit(origin, {"apps/admin/main.js": "admin(2)\n"})

    proc = _run(remote_sh, "remote", "admin", tmp_r / "admin", tmp_r, marker=False)
    assert "BUILD admin" in proc.stdout, "admin's own code: a normal build"
    proc = _run(remote_sh, "remote", "web", tmp_r / "web", tmp_r, marker=True)
    assert f"no-input-change push {pushed}{TAGGED}" in proc.stdout, proc.stdout
    assert "BUILD" not in proc.stdout, "the tag ends the run before the build"
    calls = _calls(tmp_r)
    assert _tags(calls) == [
        f"buildx imagetools create --prefer-index=false -t zot.example.com/demo/web:{pushed} "
        f"zot.example.com/demo/web:{first}",
    ]
    assert not any("buildx build" in c or ":latest" in c for c in calls)

    # The sibling's bay.toml with the sibling's code (an adopt commit plus code):
    # no input of web either.
    both = _commit(origin, {
        "bay/admin.toml": (origin / "bay" / "admin.toml").read_text() + "\n# note\n",
        "apps/admin/main.js": "admin(3)\n",
    })
    proc = _run(remote_sh, "remote", "web", tmp_r / "web", tmp_r, marker=True)
    assert f"no-input-change push {both}{TAGGED}" in proc.stdout, proc.stdout
    assert _tags(_calls(tmp_r)) == [
        f"buildx imagetools create --prefer-index=false -t zot.example.com/demo/web:{both} "
        f"zot.example.com/demo/web:{pushed}",
    ]

    # ── local: one shared checkout, each service reads its own mark ──
    local_sh = _render("local")
    tmp_l = tmp_path / "local"
    tmp_l.mkdir()
    origin, first = _shared_repo(tmp_l)
    checkout = tmp_l / "checkout"
    _git(tmp_l, "clone", "-q", str(origin), str(checkout))
    refs = tmp_l / "refs"
    refs.write_text(f"bay-teststack-web:{first}\nbay-teststack-admin:{first}\n")
    pushed = _commit(origin, {"apps/admin/main.js": "admin(2)\n"})

    # admin runs first and pulls the shared checkout; web still diffs from its mark.
    proc = _run(local_sh, "local", "admin", checkout, tmp_l, marker=False)
    assert "BUILD admin" in proc.stdout
    proc = _run(local_sh, "local", "web", checkout, tmp_l, marker=True)
    assert f"no-input-change push {pushed}{TAGGED}" in proc.stdout, proc.stdout
    assert "BUILD" not in proc.stdout
    calls = _calls(tmp_l)
    assert _tags(calls) == [f"tag bay-teststack-web:{first} bay-teststack-web:{pushed}"]
    assert not any("build" in c or ":latest" in c for c in calls)

    # The same trigger again (a redelivery): nothing changed since, nothing to tag.
    proc = _run(local_sh, "local", "web", checkout, tmp_l, marker=True)
    assert f"no-input-change push {pushed}{TAGGED}" in proc.stdout
    assert _tags(_calls(tmp_l)) == []


def test_no_input_change_falls_back_to_build(tmp_path: Path) -> None:
    """rebuild.sh checks the range itself. When its diff has an input of the
    project (the receiver saw only the last push of the range), or anything
    cannot be checked, the push builds: never a tag over changed code."""
    for strategy in ("remote", "local"):
        tmp = tmp_path / strategy
        tmp.mkdir()
        _fallback_case(strategy, tmp)


def _fallback_case(strategy: str, tmp: Path) -> None:
    rendered = _render(strategy)
    origin, first = _shared_repo(tmp)
    checkout = tmp / "checkout"
    _git(tmp, "clone", "-q", str(origin), str(checkout))
    refs = tmp / "refs"
    repo = "zot.example.com/demo/web" if strategy == "remote" else "bay-teststack-web"
    refs.write_text(f"{repo}:{first}\n")

    def run(**kw: object) -> str:
        proc = _run(
            rendered, strategy, "web", checkout, tmp, marker=True, **kw  # type: ignore[arg-type]
        )
        return proc.stdout

    # web's own code, then admin's: the marker came for the admin push only.
    _commit(origin, {"apps/web/main.js": "web(2)\n"})
    stale = _commit(origin, {"apps/admin/main.js": "admin(2)\n"})
    out = run()
    assert f"no-input-change push {stale}: an input changed since {first}, building" in out
    assert "BUILD web" in out and TAGGED not in out
    assert _tags(_calls(tmp)) == [], f"{strategy}: never a tag over changed code"

    # From here web's previous commit is `stale` (as if that build had run).
    refs.write_text(refs.read_text() + f"{repo}:{stale}\n")

    # web's own bay.toml in the range: the config-only rule or the build decides.
    web_toml = origin / "bay" / "web.toml"
    own = _commit(origin, {
        "bay/web.toml": web_toml.read_text() + "\n# note\n",
        "apps/admin/main.js": "admin(3)\n",
    })
    out = run()
    assert f"no-input-change push {own}: an input changed since {stale}" in out
    assert "BUILD web" in out and _tags(_calls(tmp)) == []
    refs.write_text(refs.read_text() + f"{repo}:{own}\n")

    # The [build] hash at the head is not the pinned one: build.
    moved = _commit(origin, {"apps/admin/main.js": "admin(4)\n"})
    out = run(extra="PINNED_BUILD_HASH='sha256:other'")
    assert f"no-input-change push {moved}: an input changed since {own}" in out
    assert "BUILD web" in out and _tags(_calls(tmp)) == []

    # No image holds the previous commit: the normal path builds it.
    nxt = _commit(origin, {"apps/admin/main.js": "admin(5)\n"})
    out = run()
    assert f"no-input-change push {nxt}: no image known to hold {moved}, building" in out
    assert "BUILD web" in out and _tags(_calls(tmp)) == []
    refs.write_text(refs.read_text() + f"{repo}:{nxt}\n")

    # A pattern the box cannot read counts as an input change.
    bad = _commit(origin, {"apps/admin/main.js": "admin(6)\n"})
    out = run(extra="INPUT_ARGS=(--include 'apps/web\\')")
    assert f"no-input-change push {bad}: an input changed since {nxt}" in out
    assert "BUILD web" in out and _tags(_calls(tmp)) == []

    # Control: the same kind of push, now tagged.
    good = _commit(origin, {"apps/admin/main.js": "admin(7)\n"})
    refs.write_text(refs.read_text() + f"{repo}:{bad}\n")
    out = run()
    assert f"no-input-change push {good}{TAGGED}" in out and "BUILD" not in out
    assert len(_tags(_calls(tmp))) == 1


# ── the receiver ────────────────────────────────────────────────────────


def _post(svc_config: dict, files: list[str], tmp_path: Path, *, pending: str | None = None):
    """POST one signed push to the real receiver; return its answer and the trigger path."""
    trigger_dir = tmp_path / "triggers"
    trigger_dir.mkdir(parents=True)
    trigger = trigger_dir / "shop.trigger"
    if pending is not None:
        trigger.write_text(pending)
    saved = {
        k: getattr(webhook_app, k)
        for k in ("WEBHOOK_SECRET", "TRIGGER_DIR", "LOCAL_REGION", "SERVICE_CONFIG",
                  "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "BAY_RECIPIENTS", "ALERT_WEBHOOK_URL")
    }
    sent: list[str] = []
    real_send = webhook_app.send_alert
    webhook_app.WEBHOOK_SECRET = "testsecret"
    webhook_app.TRIGGER_DIR = trigger_dir
    webhook_app.LOCAL_REGION = ""
    webhook_app.SERVICE_CONFIG = {"shop": {**svc_config, "branch": "main"}}
    webhook_app.send_alert = lambda alert_id, message: sent.append(alert_id)
    body = json.dumps({
        "ref": "refs/heads/main",
        "forced": False,
        "commits": [
            {"id": "abc1234567890", "message": "m", "added": [], "removed": [], "modified": files}
        ],
        "head_commit": {"id": "abc1234567890", "message": "m"},
        "pusher": {"name": "tester"},
        "repository": {"full_name": "acme/shop"},
    }).encode()
    sig = "sha256=" + hmac.new(b"testsecret", body, hashlib.sha256).hexdigest()
    server = HTTPServer(("127.0.0.1", 0), webhook_app.WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/webhook/shop",
            data=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            answer = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()
        webhook_app.send_alert = real_send
        for k, v in saved.items():
            setattr(webhook_app, k, v)
    answer["_alerts"] = sent
    return answer, trigger


def test_receiver_marks_a_push_with_no_input_change(tmp_path: Path) -> None:
    config = _render_webhook_config(_watched("remote"))
    web = config["web"]
    # Sibling code only: no input of web, the marker. Own code, or own and
    # sibling code: a normal trigger. A project with no `watch` and no narrower
    # context counts every file, so every push builds it, as before.
    assert push_filter({"apps/admin/main.js"}, web)[0] is False
    assert push_filter({"apps/web/main.js"}, web) == (True, "1 file(s) matched after filtering")
    assert push_filter({"apps/web/main.js", "apps/admin/main.js"}, web)[0] is True
    no_watch = config["blog"]
    assert "paths" not in no_watch and "context" not in no_watch
    assert push_filter({"apps/admin/main.js"}, no_watch) == (True, "no path filtering configured")

    cases = [
        (["apps/admin/main.js"], NO_INPUT_CHANGE),
        (["apps/web/main.js"], None),
        (["apps/web/main.js", "apps/admin/main.js"], None),
    ]
    for i, (files, mode) in enumerate(cases):
        answer, trigger = _post(web, files, tmp_path / f"c{i}")
        assert answer["status"] == "triggered", answer
        assert answer.get("mode") == mode, (files, answer)
        assert trigger.read_text().splitlines()[1:] == ([mode] if mode else []), files
        # A tag-only push is no build and sends no webhook.received alert.
        assert answer["_alerts"] == ([] if mode else ["webhook.received"]), files
    answer, trigger = _post(no_watch, ["apps/admin/main.js"], tmp_path / "nw")
    assert "mode" not in answer and trigger.read_text().splitlines()[1:] == []

    # A trigger that is already waiting is never replaced by the marker: a
    # normal push or a manual rebuild must still build.
    answer, trigger = _post(web, ["apps/admin/main.js"], tmp_path / "p1", pending="corr-0\n")
    assert answer["mode"] == NO_INPUT_CHANGE
    assert trigger.read_text() == "corr-0\n"
    answer, trigger = _post(
        web, ["apps/web/main.js"], tmp_path / "p2", pending=f"corr-0\n{NO_INPUT_CHANGE}"
    )
    assert "mode" not in answer and trigger.read_text().splitlines()[1:] == []


def test_build_context_narrows_inputs_without_watch(tmp_path: Path) -> None:
    """No `watch`: a [build] context narrower than the repo is the input set,
    plus the Dockerfile. The context of the repo root narrows nothing."""
    assert bay_build_context({"context": "./apps/api/"}) == "apps/api"
    assert bay_build_context({"context": "."}) == ""
    assert bay_build_context({"context": "./"}) == ""
    assert bay_build_context({}) == ""
    assert bay_build_context({"context": "apps/api", "paths": {"include": ["x/**"]}}) == ""
    assert bay_build_context({"context": "apps/api", "paths": {"exclude": ["*.md"]}}) == "apps/api"

    services = _shared_services("remote")
    services["web"]["build"].update(context="./apps/web", dockerfile="docker/web.Dockerfile")
    services["admin"]["build"].update(context=".")
    config = _render_webhook_config(services)
    assert config["web"]["context"] == "apps/web"
    assert config["web"]["dockerfile"] == "docker/web.Dockerfile"
    assert "context" not in config["admin"] and "dockerfile" not in config["admin"]

    web = config["web"]
    assert push_filter({"apps/admin/main.js"}, web) == (
        False, "0/1 file(s) under the build context apps/web"
    )
    assert push_filter({"apps/web/main.js"}, web)[0] is True
    assert push_filter({"docker/web.Dockerfile"}, web)[0] is True
    assert push_filter({"docker/web.Dockerfile.dockerignore"}, web)[0] is True
    assert push_filter({"apps/webx/main.js"}, web)[0] is False, "a prefix is not the directory"
    assert push_filter({"apps/admin/main.js"}, config["admin"])[0] is True

    # The script gets the same rule, for its own check.
    rendered = _render_rebuild_sh(services, NAMES, git_deploy_services=NAMES,
                                  git_deploy_build_strategy="remote")
    assert (
        'INPUT_ARGS=("--context" "apps/web" "--dockerfile" "docker/web.Dockerfile")'
        in _service_config(rendered, "web")
    )
    assert "INPUT_ARGS=()" in _service_config(rendered, "admin")
    watched = _render("remote")
    assert 'INPUT_ARGS=("--include" "apps/web/**")' in _service_config(watched, "web")


# ── the box-side matcher ────────────────────────────────────────────────

PATTERNS = [
    "apps/platform/**", "packages/**", "conf/**", "*.md", "/README.md", "docs/",
    "src", "src/*", "**/test/**", "a/**/b", "*.py", "!keep.md", "[ab]*.txt",
    "file?.js", "**", "*", "*/", "/", "dir/sub/", r"\#hash", "#comment", "",
    "lib/**/*.ts", "**/node_modules", ".github/**", "*.{js,ts}", "foo/**/",
]
PATHS = [
    "apps/platform/x.ts", "apps/platform", "apps/platformx/y", "packages/db/a.sql",
    "conf/nginx.conf", "README.md", "docs/README.md", "docs/x/y.txt", "src",
    "src/a.py", "src/a/b.py", "x/test/y", "test/y", "a/b", "a/x/y/b", "a/b/c",
    "keep.md", "a.txt", "b1.txt", "c.txt", "file1.js", "file10.js", "#hash",
    "lib/x/y.ts", "lib/y.ts", "x/node_modules/z", ".github/workflows/ci.yml",
    "foo/bar", "dir/sub/x", "top.py", "pnpm-lock.yaml",
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_pushinputs_matches_pathspec(pattern: str) -> None:
    """The box matcher answers like the receiver's pathspec, pattern by pattern."""
    spec = pathspec.PathSpec.from_lines("gitignore", [pattern])
    ours = pushinputs.Spec([pattern])
    for path in PATHS:
        assert ours.match(path) == spec.match_file(path), (pattern, path)


def test_pushinputs_agrees_with_the_receiver(tmp_path: Path) -> None:
    """Same input rule on both sides: watch, else a narrower context, then ignore."""
    configs = [
        {"paths": {"include": ["apps/platform/**", "packages/**"]}},
        {"paths": {"include": ["apps/**"], "exclude": ["*.md", "apps/old/**"]}},
        {"paths": {"exclude": ["*.md", ".github/**", "tests/**"]}},
        {"context": "apps/api", "dockerfile": "apps/api/Dockerfile"},
        {"context": "apps/api", "dockerfile": "docker/api.Dockerfile",
         "paths": {"exclude": ["**/*.md"]}},
        {"paths": {"include": ["!apps/**", "apps/x/**"]}},
    ]
    pushes = [
        {"apps/remix-lcc/a.ts", "pnpm-lock.yaml"}, {"apps/platform/a.ts"},
        {"packages/db/x.sql", "README.md"}, {"README.md"}, {"apps/old/a.ts"},
        {".github/workflows/ci.yml", "tests/t.py"}, {"apps/api/main.go"},
        {"apps/api/README.md"}, {"docker/api.Dockerfile"}, {"apps/x/y"},
        {"docker/api.Dockerfile.dockerignore"}, {"apps/apix/main.go"},
    ]
    for cfg in configs:
        paths = cfg.get("paths") or {}
        for files in pushes:
            ok, _ = input_filter(set(files), cfg)
            found = pushinputs.input_files(
                files, include=paths.get("include") or (), exclude=paths.get("exclude") or (),
                context=cfg.get("context", ""), dockerfile=cfg.get("dockerfile", ""),
            )
            assert bool(found) == ok, (cfg, files, found)

    # The CLI entry point rebuild.sh calls: files on stdin, inputs on stdout.
    args = ["--include", "apps/platform/**", "--include", "packages/**"]
    proc = subprocess.run(
        [sys.executable, "-m", "bay_reconcile.pushinputs", *args],
        input="apps/remix-lcc/a.ts\npnpm-lock.yaml\npackages/db/x.sql\n",
        capture_output=True, text=True, check=False,
    )
    assert (proc.returncode, proc.stdout) == (0, "packages/db/x.sql\n"), proc.stderr
    proc = subprocess.run(
        [sys.executable, "-m", "bay_reconcile.pushinputs", "--include", "apps\\"],
        input="apps/a\n", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2 and proc.stdout == ""
    assert pushinputs.main(["--bogus", "x"]) == 2


# ── the rendered script ─────────────────────────────────────────────────


def test_rebuild_sh_reads_the_marker_from_the_trigger(tmp_path: Path) -> None:
    """The real trigger-reading block: line 2 `no_input_change` sets the mode;
    `pull`, an empty line 2 and a manual run (no trigger) do not."""
    rendered = _render("remote")
    start = rendered.index('TRIGGER_FILE="${STACK_DIR}/triggers/${SERVICE}.trigger"')
    end = rendered.index('_log "Starting rebuild', start)
    block = rendered[start:end]
    (tmp_path / "triggers").mkdir()
    for content, want in (
        (f"corr-1\n{NO_INPUT_CHANGE}", "1 0 corr-1"),
        ("corr-2\npull\nabcdef1234567\n17", "0 1 corr-2"),
        ("corr-3\n", "0 0 corr-3"),
        (None, "0 0 manual-"),
    ):
        trigger = tmp_path / "triggers" / "web.trigger"
        if content is not None:
            trigger.write_text(content)
        script = (
            f"STACK_DIR={str(tmp_path)!r}\nSERVICE=web\n_log() {{ :; }}\n{block}\n"
            'printf "\\n%s %s %s" "${NO_INPUT_CHANGE}" "${PULL_SIGNAL}" "${CORR_ID}"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines()[-1].startswith(want), (content, proc.stdout)
        assert not trigger.exists(), "the trigger is consumed"


def test_rebuild_sh_reads_the_marker_and_decides_after_config_only() -> None:
    for strategy in ("remote", "local"):
        rendered = _render(strategy)
        assert 'elif [[ "${PULL_SIGNAL_RAW}" == "no_input_change" ]]; then' in rendered
        assert "NO_INPUT_CHANGE=0" in rendered
        site = _site(rendered, strategy)
        config_only = site.index('if _config_only "${REPO_DIR}" "${PREV_COMMIT}"; then')
        marker = site.index('if [[ "${NO_INPUT_CHANGE}" == "1" ]]; then')
        assert config_only < marker
        assert "_no_input_change_push" in site[marker:]
    # A trigger without the marker (an older receiver, a manual trigger) builds
    # as before; the marker on the pull path is never read.
    remote = _render("remote")
    pull = remote.index('if [[ "${PULL_SIGNAL}" -eq 1 && -n "${IMAGE_REF}" ]]; then')
    assert "NO_INPUT_CHANGE" not in remote[pull:remote.index("# ── Pull-only guard", pull)]
    # The box helper ships with the reconciler package and is pure stdlib.
    source = Path(pushinputs.__file__).read_text()
    assert "import pathspec" not in source
