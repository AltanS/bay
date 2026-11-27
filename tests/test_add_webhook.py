"""``bay service add-webhook``: create or repair the GitHub push hook (2.27.0).

No test reaches GitHub or any host: :class:`FakeGitHub` stands in for the
``requests`` module, and records every call. The token and the webhook
secret below are fake values; :func:`test_no_output_holds_the_token_or_the_secret`
and its siblings search every output for them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml
from typer.testing import CliRunner

from bay_cli import github_webhook as gh
from bay_cli import lockfile
from bay_cli.cli import app
from bay_cli.errors import BayError
from bay_cli.fleet import GENERATED_SERVICES

FAKE_TOKEN = "fake-admin-token-for-tests"
FAKE_SECRET = "fake-webhook-secret-for-tests"
URL = "https://deploy.acme.example/webhook/shop"
FULL_CONFIG_KEYS = {"url", "content_type", "secret", "insecure_ssl"}

runner = CliRunner()


# ── a fake GitHub ───────────────────────────────────────────────────────────


class FakeResp:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status_code = status
        self._data = data
        self.content = b"" if data is None else json.dumps(data).encode()

    def json(self) -> Any:
        if self._data is None:
            raise ValueError("no body")
        return self._data


class FakeGitHub:
    """The hooks API of one repo, with GitHub's PATCH semantics (config replaced).

    ``ping_code`` is the receiver's answer to a ping; it lands after
    ``ping_delay`` polls (None: never). ``fail`` maps ``(method, path end)``
    to an HTTP status (or an exception) for that call.
    """

    def __init__(self, hooks: list[dict[str, Any]] | None = None) -> None:
        self.hooks: dict[int, dict[str, Any]] = {h["id"]: h for h in hooks or []}
        self.calls: list[dict[str, Any]] = []
        self.ping_code: int | None = 200
        self.ping_delay = 1
        self._pending: list[int] = []
        self.fail: dict[tuple[str, str], Any] = {}
        self.fail_body: Any = None

    def _view(self, hook: dict[str, Any]) -> dict[str, Any]:
        view = json.loads(json.dumps(hook))
        if view["config"].get("secret"):
            view["config"]["secret"] = "********"
        return view

    def request(self, method: str, url: str, **kw: Any) -> FakeResp:
        self.calls.append({"method": method, "url": url, **kw})
        assert url.startswith("https://api.github.com/repos/")
        path = url[len("https://api.github.com") :].split("?")[0]
        for (m, end), what in self.fail.items():
            if m == method and path.endswith(end):
                if isinstance(what, Exception):
                    raise what
                return FakeResp(what, self.fail_body)
        parts = path.strip("/").split("/")  # repos owner name hooks [id [pings]]
        if method == "GET" and len(parts) == 4:
            return FakeResp(200, [self._view(h) for h in self.hooks.values()])
        if method == "POST" and len(parts) == 4:
            new_id = max(self.hooks, default=0) + 1
            body = kw["json"]
            self.hooks[new_id] = {
                "id": new_id,
                "active": body["active"],
                "events": body["events"],
                "config": dict(body["config"]),
                "last_response": {"code": None, "status": "unused"},
            }
            return FakeResp(201, self._view(self.hooks[new_id]))
        hook = self.hooks[int(parts[4])]
        if method == "PATCH":
            body = kw["json"]
            hook["config"] = dict(body["config"])  # GitHub REPLACES the config
            hook["active"] = body.get("active", hook["active"])
            hook["events"] = body.get("events", hook["events"])
            return FakeResp(200, self._view(hook))
        if method == "POST" and parts[-1] == "pings":
            self._pending = [self.ping_delay] if self.ping_code is not None else []
            return FakeResp(204)
        if method == "GET":
            if self._pending:
                self._pending[0] -= 1
                if self._pending[0] < 0:
                    hook["last_response"] = {"code": self.ping_code}
                    self._pending = []
            return FakeResp(200, self._view(hook))
        raise AssertionError(f"unexpected call {method} {url}")

    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] in ("POST", "PATCH")]


def bay_hook(
    hook_id: int = 7,
    url: str = URL,
    *,
    code: int | None = 200,
    content_type: str = "json",
    secret: bool = True,
    active: bool = True,
    events: list[str] | None = None,
    insecure_ssl: str = "0",
) -> dict[str, Any]:
    config: dict[str, Any] = {"url": url, "content_type": content_type, "insecure_ssl": insecure_ssl}
    if secret:
        config["secret"] = "old-value"
    return {
        "id": hook_id,
        "active": active,
        "events": events if events is not None else ["push"],
        "config": config,
        "last_response": {"code": code},
    }


TARGET = gh.HookTarget("shop", ["shop"], "acme", "shop", "deploy.acme.example", "production")


def run(fake: FakeGitHub, *, check: bool = False, target: gh.HookTarget = TARGET) -> dict:
    client = gh.GitHubHooks(FAKE_TOKEN, http=fake, secrets=(FAKE_SECRET,))
    hooks = client.list_hooks(target.owner, target.name)
    return gh.ensure_hook(
        client, target, FAKE_SECRET, hooks, project="shop", env="production",
        check=check, sleep=lambda _s: None,
    )


# ── the flow per hook ───────────────────────────────────────────────────────


def test_create_sends_the_full_config_and_verifies_with_a_ping() -> None:
    fake = FakeGitHub()
    row = run(fake)
    assert (row["action"], row["verification"], row["next"]) == ("created", "verified", None)
    (post, ping) = fake.writes()
    assert post["url"] == "https://api.github.com/repos/acme/shop/hooks"
    assert set(post["json"]) == {"name", "active", "events", "config"}
    assert post["json"]["name"] == "web" and post["json"]["active"] is True
    assert post["json"]["events"] == ["push"]
    assert set(post["json"]["config"]) == FULL_CONFIG_KEYS
    assert post["json"]["config"] == {
        "url": URL, "content_type": "json", "secret": FAKE_SECRET, "insecure_ssl": "0",
    }
    assert ping["url"].endswith("/hooks/1/pings") and ping["json"] is None


def test_every_call_has_a_timeout_no_redirects_and_the_token_only_in_the_header() -> None:
    fake = FakeGitHub([bay_hook(code=401)])
    run(fake)
    assert fake.calls
    for call in fake.calls:
        assert call["timeout"] == gh.API_TIMEOUT
        assert call["allow_redirects"] is False
        assert call["headers"]["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert FAKE_TOKEN not in call["url"] and FAKE_SECRET not in call["url"]
        assert FAKE_TOKEN not in json.dumps(call["json"])
        # The secret rides only in the body of a write.
        if call["method"] == "GET" or call["url"].endswith("/pings"):
            assert FAKE_SECRET not in json.dumps(call["json"])


def test_a_right_hook_with_a_200_needs_nothing() -> None:
    fake = FakeGitHub([bay_hook()])
    row = run(fake)
    assert (row["action"], row["verification"], row["hook_id"]) == ("ok", "verified", 7)
    assert fake.writes() == []


@pytest.mark.parametrize(
    ("hook", "problem"),
    [
        (bay_hook(content_type="form"), "content type is form, not json"),
        (bay_hook(secret=False), "it has no secret"),
        (bay_hook(active=False), "the hook is not active"),
        (bay_hook(events=["pull_request"]), "push is not in its events"),
        (bay_hook(insecure_ssl="1"), "the TLS check is off (insecure_ssl)"),
    ],
)
def test_wrong_settings_are_repaired_with_one_full_patch(hook: dict, problem: str) -> None:
    fake = FakeGitHub([hook])
    row = run(fake)
    assert row["action"] == "repaired" and problem in row["problems"]
    patches = [c for c in fake.writes() if c["method"] == "PATCH"]
    assert len(patches) == 1
    body = patches[0]["json"]
    assert set(body) == {"active", "events", "config"}
    assert set(body["config"]) == FULL_CONFIG_KEYS
    assert body["config"]["secret"] == FAKE_SECRET and body["active"] is True
    assert "push" in body["events"]
    # GitHub replaced the config: the stored hook is right now.
    assert gh.hook_problems(fake.hooks[7]) == []


def test_a_repair_keeps_the_other_events() -> None:
    fake = FakeGitHub([bay_hook(events=["pull_request"])])
    run(fake)
    (patch,) = [c for c in fake.writes() if c["method"] == "PATCH"]
    assert patch["json"]["events"] == ["pull_request", "push"]


def test_a_401_last_response_is_repaired_and_verified() -> None:
    fake = FakeGitHub([bay_hook(code=401)])
    row = run(fake)
    assert row["action"] == "repaired" and "its last delivery got HTTP 401" in row["problems"]
    assert row["verification"] == "verified"
    assert [c["method"] for c in fake.writes()] == ["PATCH", "POST"]


@pytest.mark.parametrize(
    ("code", "word", "next_part"),
    [
        (200, "verified", None),
        (404, "receiver does not know the service", "run `bay up production --project shop` first"),
        (401, "signature refused", "may run an older secret"),
        (403, "signature refused", "may run an older secret"),
        (500, "receiver answered HTTP 500", "docker logs bay-webhook"),
    ],
)
def test_each_ping_answer(code: int, word: str, next_part: str | None) -> None:
    fake = FakeGitHub()
    fake.ping_code = code
    row = run(fake)
    assert row["action"] == "created" and row["verification"] == word
    if next_part is None:
        assert row["next"] is None
    else:
        assert next_part in row["next"]
    # The hook stays, whatever the answer.
    assert len(fake.hooks) == 1


def test_no_answer_in_time_is_not_verified_yet() -> None:
    fake = FakeGitHub()
    fake.ping_code = None
    row = run(fake)
    assert row["verification"] == "not verified yet"
    assert "the first push will show it" in row["detail"]
    polls = [c for c in fake.calls if c["method"] == "GET" and "/hooks/1" in c["url"]]
    assert len(polls) == gh.PING_POLLS + 1  # the read before the ping, then the polls


def test_a_repair_waits_for_a_code_that_differs_from_the_old_one() -> None:
    """A repaired hook shows its old 401 until the ping's answer lands."""
    fake = FakeGitHub([bay_hook(code=401)])
    fake.ping_delay = 3
    assert run(fake)["verification"] == "verified"


# ── --check ─────────────────────────────────────────────────────────────────


def test_check_writes_nothing() -> None:
    for hooks in ([], [bay_hook(code=401)], [bay_hook(content_type="form")], [bay_hook()]):
        fake = FakeGitHub(hooks)
        run(fake, check=True)
        assert fake.writes() == [] and {c["method"] for c in fake.calls} == {"GET"}


def test_check_reports_missing_broken_and_ok() -> None:
    assert run(FakeGitHub(), check=True)["action"] == "missing"
    broken = run(FakeGitHub([bay_hook(code=401)]), check=True)
    assert (broken["action"], broken["verification"]) == ("broken", "signature refused")
    assert "bay service add-webhook shop" in broken["next"]
    unknown = run(FakeGitHub([bay_hook(code=404)]), check=True)
    assert "bay up production --project shop" in unknown["next"]
    ok = run(FakeGitHub([bay_hook()]), check=True)
    assert (ok["action"], ok["verification"]) == ("ok", "verified")


# ── which hooks a project needs ─────────────────────────────────────────────

FLEET_DOC: dict[str, Any] = {
    "boxes": {
        "eu-1": {"env": "production", "group": "eu1"},
        "eu-2": {"env": "production", "group": "eu2", "webhook_domain": "deploy.eu2.acme.example"},
        "infra": {"env": "production", "group": "infra", "webhook_domain": "deploy.infra.acme.example"},
    },
    "webhook": {"domain": "deploy.acme.example", "secret": "WEBHOOK_SECRET"},
}


def _svc(strategy: str | None = None, repo: str = "https://github.com/acme/shop", **more: Any) -> dict:
    build: dict[str, Any] = {"repo": repo, "branch": "main", **more}
    if strategy:
        build["strategy"] = strategy
    return {"build": build}


def _targets(services: dict, *, box: str = "eu-1", main: str | None = "shop") -> tuple:
    calls: list[int] = []

    def server() -> tuple[list[str], str]:
        calls.append(1)
        return ["infra", "production"], "production"

    out = gh.hook_targets(
        FLEET_DOC, services, sorted(services), main=main, box=box, box_env="production",
        strategy_of=lambda e: e["build"].get("strategy", "local"), build_server=server,
    )
    return (*out, calls)


def test_local_build_lands_on_its_own_box() -> None:
    (t,), _, calls = _targets({"shop": _svc()})
    assert t.url() == "https://deploy.acme.example/webhook/shop" and calls == []
    (t,), _, _ = _targets({"shop": _svc()}, box="eu-2")
    assert t.url() == "https://deploy.eu2.acme.example/webhook/shop"


def test_remote_build_lands_on_the_build_server() -> None:
    (t,), _, calls = _targets({"shop": _svc("remote")})
    assert t.url() == "https://deploy.infra.acme.example/webhook/shop" and calls == [1]


def test_a_sibling_group_needs_one_hook() -> None:
    services = {
        "shop": _svc(bay_project="shop"),
        "shop-worker": _svc(bay_project="shop"),
    }
    (t,), _, _ = _targets(services)
    assert t.container == "shop" and t.members == ["shop", "shop-worker"]
    # A hook on any member counts: no create.
    fake = FakeGitHub([bay_hook(url="https://deploy.acme.example/webhook/shop-worker")])
    row = run(fake, target=t)
    assert (row["action"], row["container"]) == ("ok", "shop-worker") and fake.writes() == []


def test_a_group_whose_pushes_land_on_two_receivers_is_refused() -> None:
    services = {
        "shop": _svc(bay_project="shop"),
        "shop-worker": _svc("remote", bay_project="shop"),
    }
    with pytest.raises(BayError, match="different receivers"):
        _targets(services)


def test_a_second_hook_in_a_group_is_reported() -> None:
    target = gh.HookTarget("shop", ["shop", "shop-worker"], "acme", "shop",
                           "deploy.acme.example", "production")
    fake = FakeGitHub([bay_hook(7), bay_hook(8, url="https://deploy.acme.example/webhook/shop-worker")])
    row = run(fake, target=target)
    assert row["container"] == "shop"
    assert row["redundant_hooks"] == ["https://deploy.acme.example/webhook/shop-worker"]


def test_a_hook_on_an_old_domain_is_reported() -> None:
    fake = FakeGitHub([bay_hook(3, url="https://deploy.old.acme.example/webhook/shop")])
    row = run(fake)
    assert row["action"] == "created"
    assert row["stale_hooks"] == ["https://deploy.old.acme.example/webhook/shop"]


def test_registry_and_shared_builds_are_skipped() -> None:
    services = {"shop": _svc("registry"), "shop-alias": _svc(shared_from="shop")}
    targets, skipped, _ = _targets(services)
    assert targets == []
    assert {s["container"] for s in skipped} == {"shop", "shop-alias"}
    assert "registry" in next(s["reason"] for s in skipped if s["container"] == "shop")


def test_a_non_github_repo_is_refused() -> None:
    with pytest.raises(BayError, match="only github.com repos are supported"):
        _targets({"shop": _svc(repo="https://gitlab.example/acme/shop.git")})


def test_a_fleet_without_webhook_is_refused() -> None:
    with pytest.raises(BayError, match=r"no \[webhook\] domain"):
        gh.domain_for_box({"boxes": {"eu-1": {"env": "production"}}}, "eu-1")


# ── HTTP errors ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (401, "refused the github_admin_token (HTTP 401)"),
        (403, "cannot write the webhooks of acme/shop (HTTP 403)"),
        (404, "cannot write the webhooks of acme/shop (HTTP 404)"),
        (422, "GitHub refused the hook for acme/shop (HTTP 422"),
    ],
)
def test_write_errors_are_clear(status: int, words: str) -> None:
    fake = FakeGitHub()
    fake.fail[("POST", "/hooks")] = status
    fake.fail_body = {"message": "Validation Failed", "errors": [{"message": "Hook already exists"}]}
    with pytest.raises(BayError) as info:
        run(fake)
    assert words in str(info.value)
    if status in (401, 403, 404):
        assert "admin:repo_hook" in info.value.hint and "Webhooks: Read and write" in info.value.hint
    else:
        assert "Hook already exists" in str(info.value)


def test_a_422_that_echoes_a_credential_is_scrubbed() -> None:
    fake = FakeGitHub()
    fake.fail[("POST", "/hooks")] = 422
    fake.fail_body = {"message": f"bad {FAKE_SECRET}", "errors": [f"also {FAKE_TOKEN}"]}
    with pytest.raises(BayError) as info:
        run(fake)
    text = f"{info.value} {info.value.hint}"
    assert FAKE_SECRET not in text and FAKE_TOKEN not in text and "***" in text


def test_a_network_error_names_only_its_type() -> None:
    fake = FakeGitHub()
    fake.fail[("POST", "/hooks")] = requests.ConnectionError(f"boom {FAKE_SECRET} {FAKE_TOKEN}")
    with pytest.raises(BayError) as info:
        run(fake)
    assert "ConnectionError" in str(info.value)
    assert FAKE_SECRET not in str(info.value) and FAKE_TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_a_redirect_is_not_followed() -> None:
    fake = FakeGitHub()
    fake.fail[("GET", "/hooks")] = 301
    with pytest.raises(BayError, match="redirect"):
        run(fake)
    assert len(fake.calls) == 1


# ── the CLI on a small fleet ────────────────────────────────────────────────

FLEET_TOML = """\
name = "acme"
default_box = "eu-1"
default_domain = "acme.example"
primary_env = "production"

[boxes.eu-1]
env = "production"
group = "eu1"

[webhook]
domain = "deploy.acme.example"
secret = "WEBHOOK_SECRET"
"""

SHOP_TOML = """\
name = "shop"
fleet = "acme"
port = 3000

[build]
repo = "https://github.com/acme/shop"
dockerfile = "Dockerfile"

[deploy.production]
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@acme.example", *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "fleet"
    (root / "projects" / "shop").mkdir(parents=True)
    (root / "group_vars" / "production").mkdir(parents=True)
    (root / "group_vars" / "all").mkdir()
    (root / "bay.fleet.toml").write_text(FLEET_TOML)
    (root / "projects" / "shop" / "bay.toml").write_text(SHOP_TOML)
    (root / GENERATED_SERVICES).write_text(yaml.safe_dump({
        "services": {"shop": {"build": {"repo": "https://github.com/acme/shop", "branch": "main"},
                              "regions": ["eu1"]}},
    }))
    (root / ".vault_pass").write_text("not-used\n")
    _write_secrets(root, token=FAKE_TOKEN, secret=FAKE_SECRET)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fleet")
    head = _git(root, "rev-parse", "HEAD")
    lock = lockfile.new_lock("shop", repo=None)
    lock["commit"] = head
    lock["envs"] = {"production": {"box": "eu-1", "commit": head}}
    lockfile.write(lockfile.lock_path(root, "shop"), lock)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return root


def _write_secrets(root: Path, *, token: str | None, secret: str | None) -> None:
    values = {k: v for k, v in {"github_admin_token": token, "WEBHOOK_SECRET": secret}.items() if v}
    (root / "group_vars" / "production" / "secrets.yml").write_text(
        yaml.safe_dump({"secrets": values})
    )


def cli(fleet: Path, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, *args: str) -> Any:
    monkeypatch.setattr(requests, "request", fake.request)
    return runner.invoke(app, ["--fleet", str(fleet), "service", "add-webhook", "shop", *args])


def test_cli_creates_the_hook_and_prints_one_json_document(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub()
    result = cli(fleet, fake, monkeypatch, "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["result"] == "ok" and doc["env"] == "production" and doc["check"] is False
    (row,) = doc["hooks"]
    assert (row["container"], row["repo"], row["url"]) == ("shop", "acme/shop", URL)
    assert (row["action"], row["verification"]) == ("created", "verified")
    assert fake.hooks[1]["config"]["secret"] == FAKE_SECRET


def test_cli_check_exits_1_on_a_missing_hook_and_writes_nothing(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGitHub()
    result = cli(fleet, fake, monkeypatch, "--check")
    assert result.exit_code == 1, result.output
    assert "shop: missing" in result.stdout
    assert fake.writes() == []


def test_cli_human_output(fleet: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub([bay_hook(code=401)])
    fake.ping_code = 404
    result = cli(fleet, fake, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "shop: repaired, receiver does not know the service" in result.stdout
    assert f"url {URL}" in result.stdout and "next: " in result.stdout


@pytest.mark.parametrize("missing", ["token", "secret"])
def test_missing_credentials_stop_before_any_call(
    fleet: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _write_secrets(
        fleet,
        token=None if missing == "token" else FAKE_TOKEN,
        secret=None if missing == "secret" else FAKE_SECRET,
    )
    fake = FakeGitHub()
    result = cli(fleet, fake, monkeypatch, "--json")
    assert result.exit_code != 0
    doc = json.loads(result.stdout)
    assert ("github_admin_token" if missing == "token" else "WEBHOOK_SECRET") in doc["error"]
    assert fake.calls == []


def test_registry_project_has_nothing_to_hook(fleet: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fleet / GENERATED_SERVICES).write_text(yaml.safe_dump({
        "services": {"shop": {"build": {"repo": "https://github.com/acme/shop",
                                        "strategy": "registry"}}},
    }))
    fake = FakeGitHub()
    result = cli(fleet, fake, monkeypatch, "--json")
    assert result.exit_code != 0 and "strategy registry" in json.loads(result.stdout)["error"]
    assert fake.calls == []


def _all_text(result: Any) -> str:
    parts = [result.output, result.stdout, str(result.exception or "")]
    try:
        parts.append(result.stderr)
    except ValueError:
        pass
    return "\n".join(parts)


@pytest.mark.parametrize("mode", [[], ["--json"], ["--check"], ["--check", "--json"]])
def test_no_output_holds_the_token_or_the_secret(
    fleet: Path, monkeypatch: pytest.MonkeyPatch, mode: list[str]
) -> None:
    fake = FakeGitHub([bay_hook(code=401)])
    result = cli(fleet, fake, monkeypatch, *mode)
    text = _all_text(result)
    assert "shop" in text
    assert FAKE_TOKEN not in text and FAKE_SECRET not in text


@pytest.mark.parametrize("as_json", [False, True])
def test_no_error_output_holds_the_token_or_the_secret(
    fleet: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    for fail in (422, requests.ConnectionError(f"x {FAKE_SECRET} {FAKE_TOKEN}")):
        fake = FakeGitHub()
        fake.fail[("POST", "/hooks")] = fail
        fake.fail_body = {"message": f"echo {FAKE_SECRET} {FAKE_TOKEN}"}
        result = cli(fleet, fake, monkeypatch, *(["--json"] if as_json else []))
        assert result.exit_code != 0
        text = _all_text(result)
        assert FAKE_TOKEN not in text and FAKE_SECRET not in text
        if isinstance(result.exception, BaseException):
            assert FAKE_SECRET not in repr(result.exception.__context__ or "")

