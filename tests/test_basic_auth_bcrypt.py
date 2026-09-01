"""Traefik basic-auth hashes: bcrypt, deterministic, in both filter copies.

The old implementation shelled out to `openssl passwd -apr1 -salt <salt>
<password>` on the control machine. Three problems: the password was an argv
element of a child process, APR1 is MD5-crypt, and the salt seed
(md5(stack + service + username)) left the password out, so rotating a password
kept the same salt.

Determinism itself is load-bearing and must survive: the hash goes into a
Traefik container label, so a random salt would change the label on every
render and the reconciler would recreate every basic-auth protected container
on every deploy.

`bay_filters.py` exists in two copies — filter_plugins/ (used by the playbooks)
and roles/container_lifecycle/filter_plugins/ (loaded role-locally by the
reconciler). They are NOT byte-identical, so each is loaded and checked
separately here; a fix applied to one only is the failure this catches.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import bcrypt
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COPIES = {
    "root": _REPO_ROOT / "filter_plugins" / "bay_filters.py",
    "container_lifecycle": _REPO_ROOT
    / "roles"
    / "container_lifecycle"
    / "filter_plugins"
    / "bay_filters.py",
}

_BCRYPT_RE = re.compile(r"^\$2b\$10\$[./A-Za-z0-9]{53}$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bay_filters_{name}", _COPIES[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=sorted(_COPIES))
def copy_name(request):
    return request.param


@pytest.fixture
def mod(copy_name):
    return _load(copy_name)


# ── The hash function ────────────────────────────────────────────────────


def test_hash_is_bcrypt(mod):
    h = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    assert _BCRYPT_RE.match(h), f"not a cost-10 bcrypt hash: {h!r}"


def test_hash_verifies(mod):
    h = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    assert bcrypt.checkpw(b"hunter2", h.encode())
    assert not bcrypt.checkpw(b"hunter3", h.encode())


def test_hash_is_deterministic(mod):
    """A changing hash recreates every protected container on every deploy."""
    first = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    second = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    assert first == second


@pytest.mark.parametrize(
    "args",
    [
        ("other-stack", "metrics", "admin", "hunter2"),
        ("stack", "other-service", "admin", "hunter2"),
        ("stack", "metrics", "root", "hunter2"),
        ("stack", "metrics", "admin", "hunter3"),
    ],
    ids=["stack", "service", "username", "password"],
)
def test_every_seed_component_changes_the_hash(mod, args):
    base = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    assert mod.bay_basic_auth_hash(*args) != base


def test_password_is_part_of_the_salt(mod):
    """The old seed omitted it, so a rotated password reused its salt."""
    a = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter2")
    b = mod.bay_basic_auth_hash("stack", "metrics", "admin", "hunter3")
    assert a[:29] != b[:29], "salt did not change with the password"


def test_overlong_password_fails_loudly(mod):
    """bcrypt truncates past 72 bytes; a silent truncation is a weaker hash."""
    with pytest.raises(ValueError):
        mod.bay_basic_auth_hash("stack", "metrics", "admin", "x" * 73)


def test_both_copies_agree():
    """Two copies that disagree would recreate containers on alternate paths."""
    root = _load("root")
    role = _load("container_lifecycle")
    assert root.bay_basic_auth_hash("s", "svc", "u", "p") == role.bay_basic_auth_hash(
        "s", "svc", "u", "p"
    )


# ── The label the filter produces ────────────────────────────────────────


def _labels(mod):
    return mod.bay_traefik_labels(
        {
            "access": "public",
            "domains": ["metrics.example.com"],
            "ports": {"internal": 9090},
            "middleware": {
                "basic_auth": {
                    "credentials": [{"username": "admin", "password": "hunter2"}]
                }
            },
        },
        "metrics",
        {"stack_name": "stack", "traefik_docker_network": "services"},
    )


def test_basic_auth_label_carries_a_bcrypt_hash(mod):
    labels = _labels(mod)
    key = next(k for k in labels if k.endswith("basicauth.users"))
    username, _, hashed = labels[key].partition(":")
    assert username == "admin"
    assert _BCRYPT_RE.match(hashed), hashed
    assert bcrypt.checkpw(b"hunter2", hashed.encode())


def test_basic_auth_label_is_stable_across_renders(mod):
    assert _labels(mod) == _labels(mod)


@pytest.mark.parametrize("path", sorted(_COPIES))
def test_no_copy_shells_out_to_openssl(path):
    src = _COPIES[path].read_text()
    assert '"-apr1"' not in src, f"{path} still runs openssl passwd -apr1"
    assert "bcrypt" in src


def test_bcrypt_is_a_declared_dependency():
    """Ansible renders these filters inside the project venv."""
    pyproject = (_REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r'"bcrypt[>=~]', pyproject), "bcrypt missing from dependencies"
    assert 'name = "bcrypt"' in (_REPO_ROOT / "uv.lock").read_text()
