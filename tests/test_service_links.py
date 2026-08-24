"""Tests for CLI link support in service commands."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

from bay_cli.catalog import CatalogEntry
from bay_cli.commands import service as svc_mod
from bay_cli.config import StackConfig

runner = CliRunner()


def _make_config(tmp_path, services_content=None):
    """Create a minimal StackConfig for testing."""
    from ruamel.yaml import YAML

    gv = tmp_path / "group_vars" / "all"
    gv.mkdir(parents=True)

    yaml = YAML()
    yaml.preserve_quotes = True

    content = services_content or {
        "services": {
            "n8n": {
                "access": "vpn",
                "image": "n8nio/n8n:latest",
                "domains": ["n8n.example.com"],
                "ports": {"internal": 5678},
                "regions": ["na"],
            },
        },
        "accessories": {
            "postgres": {
                "image": "postgres:16",
                "port": "127.0.0.1:5432:5432",
                "regions": ["eu"],
            },
        },
    }

    with (gv / "services.yml").open("w") as f:
        yaml.dump(content, f)

    return StackConfig(tmp_path)


class TestServiceAddLink:
    """service add --link flag."""

    def test_add_with_link_dry_run(self, tmp_path):
        """Adding a service with --link shows links in dry-run diff."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        def mock_catalog():
            return {}

        with (
            patch.object(svc_mod, "_get_config", mock_config),
            patch.object(svc_mod, "_get_catalog", mock_catalog),
        ):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "add",
                    "--name", "myapp",
                    "--image", "myorg/app:latest",
                    "--port", "8080",
                    "--region", "na",
                    "--link", "postgres:eu",
                    "--dry-run",
                ],
            )
            assert result.exit_code == 0
            assert "links" in result.output.lower() or "postgres" in result.output

    def test_add_with_link_json(self, tmp_path):
        """Adding with --link includes links in JSON output."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        def mock_catalog():
            return {}

        with (
            patch.object(svc_mod, "_get_config", mock_config),
            patch.object(svc_mod, "_get_catalog", mock_catalog),
        ):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "--json",
                    "service", "add",
                    "--name", "myapp",
                    "--image", "myorg/app:latest",
                    "--port", "8080",
                    "--region", "na",
                    "--link", "postgres:eu",
                    "--dry-run",
                ],
            )
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["ok"]
            assert "postgres" in data["data"]["links"]

    def test_add_self_link_rejected(self, tmp_path):
        """Self-link rejected."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        def mock_catalog():
            return {}

        with (
            patch.object(svc_mod, "_get_config", mock_config),
            patch.object(svc_mod, "_get_catalog", mock_catalog),
        ):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "add",
                    "--name", "myapp",
                    "--image", "myorg/app:latest",
                    "--port", "8080",
                    "--region", "na",
                    "--link", "myapp:eu",
                ],
            )
            assert result.exit_code != 0

    def test_add_invalid_link_format(self, tmp_path):
        """Missing colon in --link format rejected."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        def mock_catalog():
            return {}

        with (
            patch.object(svc_mod, "_get_config", mock_config),
            patch.object(svc_mod, "_get_catalog", mock_catalog),
        ):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "add",
                    "--name", "myapp",
                    "--image", "myorg/app:latest",
                    "--port", "8080",
                    "--link", "postgres",
                ],
            )
            assert result.exit_code != 0


class TestServiceEditLink:
    """service edit --link and --unlink flags."""

    def test_edit_add_link(self, tmp_path):
        """Adding a link via edit."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "edit", "n8n",
                    "--link", "postgres:eu",
                    "--dry-run",
                ],
            )
            assert result.exit_code == 0

    def test_edit_unlink(self, tmp_path):
        """Removing a link via --unlink."""
        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "regions": ["na"],
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "port": "127.0.0.1:5432:5432",
                    "regions": ["eu"],
                },
            },
        }
        cfg = _make_config(tmp_path, services_content)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "edit", "n8n",
                    "--unlink", "postgres",
                    "--dry-run",
                ],
            )
            assert result.exit_code == 0

    def test_edit_same_region_link_rejected(self, tmp_path):
        """Same-region link rejected on edit."""
        cfg = _make_config(tmp_path)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(
                root_app,
                [
                    "service", "edit", "n8n",
                    "--link", "postgres:na",
                ],
            )
            # Should fail because n8n is in "na" and link target is also "na"
            assert result.exit_code != 0


class TestServiceListLinks:
    """service list shows link count."""

    def test_list_shows_links_column(self, tmp_path):
        """Rich table includes Links column."""
        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "port": "127.0.0.1:5432:5432",
                },
            },
        }
        cfg = _make_config(tmp_path, services_content)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(root_app, ["service", "list"])
            assert result.exit_code == 0
            # Should show "1" for n8n's link count
            assert "1" in result.output

    def test_list_json_includes_links_count(self, tmp_path):
        """JSON output includes links_count."""
        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "links": {"postgres": {"region": "eu"}, "redis": {"region": "eu"}},
                },
            },
            "accessories": {},
        }
        cfg = _make_config(tmp_path, services_content)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(root_app, ["--json", "service", "list"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["data"]["services"][0]["links_count"] == 2


class TestServiceShowLinks:
    """service show displays link details."""

    def test_show_displays_link_env_vars(self, tmp_path):
        """service show prints expected env var names for links."""
        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {},
        }
        cfg = _make_config(tmp_path, services_content)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(root_app, ["service", "show", "n8n"])
            assert result.exit_code == 0
            assert "LINKS_POSTGRES_HOST" in result.output
            assert "LINKS_POSTGRES_PORT" in result.output
            assert "LINKS_POSTGRES_URL" in result.output

    def test_show_json_includes_links(self, tmp_path):
        """JSON show includes links dict."""
        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "links": {"postgres": {"region": "eu"}},
                },
            },
            "accessories": {},
        }
        cfg = _make_config(tmp_path, services_content)

        def mock_config():
            return cfg

        with patch.object(svc_mod, "_get_config", mock_config):
            from bay_cli.cli import app as root_app

            result = runner.invoke(root_app, ["--json", "service", "show", "n8n"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "links" in data["data"]["service"]
