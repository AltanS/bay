"""Gateway backend for headscale operations via docker exec."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from bay_cli.errors import BayError


class LocalHeadscaleBackend:
    """Backend that wraps docker exec headscale commands via Ansible ad-hoc."""

    def __init__(self, env: str, bay_dir: Path, limit: str | None = None) -> None:
        self._env = env
        self._bay_dir = bay_dir
        self._limit = limit

    def _exec(self, cmd: str, *, message: str = "", check: bool = True) -> str:
        """Run a docker exec command and return extracted output."""
        from bay_cli.commands.gateway import _extract_output_from_ansible, _run_on_host

        result = _run_on_host(
            self._env,
            cmd,
            bay_dir=self._bay_dir,
            message=message,
            limit=self._limit,
            check=check,
        )
        return _extract_output_from_ansible(result.stdout or "")

    def _exec_json(self, cmd: str, *, message: str = "") -> list[dict]:
        """Run a docker exec command and parse JSON output."""
        output = self._exec(cmd, message=message)
        return json.loads(output)

    def list_nodes(self) -> list[dict]:
        cmd = "docker exec headscale headscale nodes list --output json"
        return self._exec_json(cmd, message="Fetching nodes...")

    def list_users(self) -> list[dict]:
        cmd = "docker exec headscale headscale users list -o json"
        return self._exec_json(cmd, message="Fetching users...")

    def create_user(self, name: str) -> None:
        cmd = f"docker exec headscale headscale users create {name}"
        self._exec(cmd, message=f"Creating user {name}...")

    def get_user_id(self, name: str) -> str:
        cmd = "docker exec headscale headscale users list -o json"
        output = self._exec(cmd, message="Looking up user...")
        users = json.loads(output)
        for u in users:
            if u.get("name") == name:
                return str(u["id"])
        raise _user_not_found(name)

    def rename_user(self, old_name: str, new_name: str) -> None:
        user_id = self.get_user_id(old_name)
        cmd = f"docker exec headscale headscale users rename --identifier {user_id} --new-name {new_name}"
        self._exec(cmd, message="Renaming user...")

    def delete_user(self, user_id: str) -> None:
        # `users destroy` takes --identifier/--name, never a positional (v0.28
        # ignores it and errors), and prompts without --force — there's no TTY
        # here. The CLI has already confirmed with the operator by this point.
        cmd = f"docker exec headscale headscale users destroy --identifier {user_id} --force"
        self._exec(cmd, message="Deleting user...")

    def generate_preauth_key(
        self,
        user_id: str,
        *,
        expiry: str = "",
        reusable: bool = False,
        tags: list[str] | None = None,
    ) -> str:
        """Mint a pre-auth key, optionally stamped with ACL tags.

        A key-stamped tag applies from the node's very first packet, unlike
        `headscale nodes tag` after the fact. headscale takes the tags as ONE
        comma-joined `--tags` value (v0.29.x); if a future release changes that
        spelling, this single line is the only place to fix it. Tag values are
        charset-validated by the CLI before they get here — this string lands in
        a root shell on the control host.
        """
        key_flags = f"--user {user_id}"
        if reusable:
            key_flags += " --reusable"
        if expiry:
            key_flags += f" --expiration {expiry}"
        if tags:
            key_flags += f" --tags {','.join(tags)}"
        cmd = f"docker exec headscale headscale preauthkeys create {key_flags}"
        return self._exec(cmd, message="Generating key...").strip()

    def create_api_key(self, expiration: str) -> str:
        cmd = f"docker exec headscale headscale apikeys create --expiration {expiration}"
        return _extract_api_key(self._exec(cmd, message="Creating API key..."))

    def delete_node(self, node_id: int) -> None:
        cmd = f"docker exec headscale headscale nodes delete -i {node_id} --force"
        self._exec(cmd, message="Deleting node...")

    def rename_node(self, node_id: int, new_name: str) -> None:
        cmd = f"docker exec headscale headscale nodes rename --identifier {node_id} {new_name}"
        self._exec(cmd, message="Renaming node...")

    def list_node_routes(self, node_id: int | None = None) -> list[dict]:
        cmd = "docker exec headscale headscale nodes list-routes -o json"
        if node_id is not None:
            cmd += f" -i {node_id}"
        return self._exec_json(cmd, message="Fetching routes...")

    def approve_routes(self, node_id: int, routes_csv: str) -> None:
        cmd = f"docker exec headscale headscale nodes approve-routes -i {node_id} -r {routes_csv}"
        self._exec(cmd, message="Updating routes...")


def _extract_api_key(raw: str) -> str:
    """Pull the API key out of `headscale apikeys create` output.

    Headscale prepends an "An updated version ... has been found" notice (plus a
    changelog URL) when a newer release exists, which otherwise gets returned as
    part of the key. The key itself is a single opaque token with no spaces, so
    return the last non-empty, space-free, non-URL line.
    """
    for line in reversed(raw.splitlines()):
        candidate = line.strip()
        if candidate and " " not in candidate and "://" not in candidate:
            return candidate
    return raw.strip()


def _user_not_found(name: str) -> BayError:
    """Create a consistent error for user-not-found."""
    return BayError(f"User '{name}' not found in headscale.")


# ── Access-gateway adapter: CLI-level contract (M107) ────────────────────
#
# `GatewayBackend` is minted from LocalHeadscaleBackend's existing method
# list rather than designed fresh — that class was already the de facto
# interface, so this only writes down what `bin/bay gateway` had always
# assumed. A backend with EQUIVALENT semantics (a remote-headscale or a
# tailscale.com control plane) can be dropped in as a new class with no
# change to gateway.py's command logic.
#
# A backend with LESSER capabilities is not forced to fake one. WireGuard
# here is a `vpn_allowed_ips` passthrough with no node database, no users and
# no keys; making it stub out a node API would be worse than saying so. Those
# backends get NullGatewayBackend, whose every operation raises one uniform,
# actionable BayError. The bar is: every command either works, or explains
# why it cannot — never a traceback, never a silently-wrong assumption that
# headscale is present.
#
# ACL and tag commands stay headscale-only by declaration, not by
# abstraction. Generalising "ACL audit" over backends that have no ACL
# concept would be speculative generality with zero second implementations.
@runtime_checkable
class GatewayBackend(Protocol):
    """Operations `bin/bay gateway` needs from an access-gateway backend."""

    def list_nodes(self) -> list[dict]: ...
    def list_users(self) -> list[dict]: ...
    def create_user(self, name: str) -> None: ...
    def get_user_id(self, name: str) -> str: ...
    def rename_user(self, old_name: str, new_name: str) -> None: ...
    def delete_user(self, user_id: str) -> None: ...
    def create_api_key(self, expiration: str) -> str: ...
    def delete_node(self, node_id: int) -> None: ...
    def rename_node(self, node_id: int, new_name: str) -> None: ...
    def list_node_routes(self, node_id: int | None = None) -> list[dict]: ...
    def approve_routes(self, node_id: int, routes_csv: str) -> None: ...


class NullGatewayBackend:
    """Backend for gateways that manage no nodes (wireguard, none).

    Every operation raises a uniform capability error naming the active
    backend and what to do instead. This exists so an operator on a
    non-headscale gateway gets one clear sentence rather than a traceback
    from a headscale command that was never going to work.
    """

    def __init__(self, gateway_type: str) -> None:
        self.gateway_type = gateway_type

    def _unsupported(self, operation: str):
        if self.gateway_type == "none":
            hint = (
                "No access gateway is configured. Set access_gateway to "
                "'headscale' in group_vars/all/access_gateway.yml to manage "
                "nodes from Bay."
            )
        else:
            hint = (
                f"The '{self.gateway_type}' backend has no node database — "
                "peers are configured directly on the WireGuard interface and "
                "allowed through vpn_allowed_ips in group_vars. Switch to "
                "access_gateway: headscale for managed enrollment."
            )
        return BayError(
            f"the '{self.gateway_type}' access gateway does not support {operation}",
            hint=hint,
        )

    def list_nodes(self) -> list[dict]:
        raise self._unsupported("listing nodes")

    def list_users(self) -> list[dict]:
        raise self._unsupported("listing users")

    def create_user(self, name: str) -> None:
        raise self._unsupported("creating users")

    def get_user_id(self, name: str) -> str:
        raise self._unsupported("looking up users")

    def rename_user(self, old_name: str, new_name: str) -> None:
        raise self._unsupported("renaming users")

    def delete_user(self, user_id: str) -> None:
        raise self._unsupported("deleting users")

    def generate_preauth_key(self, *args, **kwargs) -> str:
        raise self._unsupported("generating pre-auth keys")

    def create_api_key(self, expiration: str) -> str:
        raise self._unsupported("creating API keys")

    def delete_node(self, node_id: int) -> None:
        raise self._unsupported("deleting nodes")

    def rename_node(self, node_id: int, new_name: str) -> None:
        raise self._unsupported("renaming nodes")

    def list_node_routes(self, node_id: int | None = None) -> list[dict]:
        raise self._unsupported("listing routes")

    def approve_routes(self, node_id: int, routes_csv: str) -> None:
        raise self._unsupported("approving routes")
