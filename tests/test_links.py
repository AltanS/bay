"""Tests for cross-region link validation."""

from bay_cli.links import link_env_var_name, validate_links


class TestValidateLinks:
    """validate_links() validation cases."""

    def test_valid_link_cross_region(self):
        """Service in NA links to accessory in EU -- passes."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"postgres": {"region": "eu"}},
            },
        }
        accessories = {
            "postgres": {
                "image": "postgres:16",
                "regions": ["eu"],
            },
        }
        errors = validate_links(services, accessories)
        assert errors == []

    def test_target_not_found(self):
        """Link to nonexistent target -- error."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"nonexistent": {"region": "eu"}},
            },
        }
        errors = validate_links(services, {})
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_target_wrong_region(self):
        """Target has regions: [na] but link says region: eu -- error."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"postgres": {"region": "eu"}},
            },
        }
        accessories = {
            "postgres": {
                "image": "postgres:16",
                "regions": ["na"],
            },
        }
        errors = validate_links(services, accessories)
        assert len(errors) == 1
        assert "does not include" in errors[0]

    def test_same_region_link_rejected(self):
        """Same-region link -- error with depends_on suggestion."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["eu"],
                "links": {"redis": {"region": "eu"}},
            },
        }
        accessories = {
            "redis": {
                "image": "redis:7",
                "regions": ["eu"],
            },
        }
        errors = validate_links(services, accessories)
        assert len(errors) == 1
        assert "depends_on" in errors[0]

    def test_self_link_rejected(self):
        """Service links to itself -- error."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"n8n": {"region": "eu"}},
            },
        }
        errors = validate_links(services, {})
        assert len(errors) == 1
        assert "cannot link to itself" in errors[0]

    def test_target_no_regions_filter(self):
        """Target with no regions: (deploys everywhere) -- passes for any region."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"postgres": {"region": "eu"}},
            },
        }
        accessories = {
            "postgres": {
                "image": "postgres:16",
                # no regions: field -- deploys everywhere
            },
        }
        errors = validate_links(services, accessories)
        assert errors == []

    def test_missing_region_field(self):
        """Link entry without region: -- error."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"postgres": {}},
            },
        }
        accessories = {
            "postgres": {"image": "postgres:16"},
        }
        errors = validate_links(services, accessories)
        assert len(errors) == 1
        assert "missing required 'region'" in errors[0]

    def test_no_links_passes(self):
        """Services without links: -- no errors."""
        services = {
            "n8n": {"image": "n8nio/n8n:latest"},
        }
        errors = validate_links(services, {})
        assert errors == []

    def test_ansible_validation_link_to_service(self):
        """Link to a service (not accessory) -- passes."""
        services = {
            "frontend": {
                "image": "frontend:latest",
                "regions": ["na"],
                "links": {"api": {"region": "eu"}},
            },
            "api": {
                "image": "api:latest",
                "regions": ["eu"],
            },
        }
        errors = validate_links(services, {})
        assert errors == []

    def test_unknown_region_with_inventory(self):
        """Link specifies region not in inventory -- error."""
        services = {
            "n8n": {
                "image": "n8nio/n8n:latest",
                "regions": ["na"],
                "links": {"postgres": {"region": "ap"}},
            },
        }
        accessories = {
            "postgres": {"image": "postgres:16"},
        }
        errors = validate_links(services, accessories, inventory_regions=["na", "eu"])
        assert len(errors) == 1
        assert "unknown region" in errors[0]

    def test_same_stack_link_no_regions_rejected(self):
        """Neither service nor target has regions: — both run everywhere,
        so on any host they're in the same stack. Must be rejected.

        This is the sandbox-shape loophole from the 2026-04-22 incident:
        gatus (no regions) links to redis (no regions) and the rewrite
        silently exposed redis on 0.0.0.0. After the link-target fix, this case is
        rejected with the same-stack error.
        """
        services = {
            "gatus": {
                "image": "twinproduction/gatus:latest",
                "links": {"redis": {"region": "testing"}},
            },
        }
        accessories = {
            "redis": {"image": "redis:7-alpine"},
        }
        errors = validate_links(services, accessories)
        assert len(errors) == 1
        assert "same-stack dependency" in errors[0]
        assert "gatus" in errors[0] and "redis" in errors[0]
        assert "services` Docker network" in errors[0]
        assert "depends_on" in errors[0]

    def test_same_stack_link_svc_no_regions_target_in_region_rejected(self):
        """Service runs everywhere, target pins to the link region —
        still same-stack on the link-region host."""
        services = {
            "gatus": {
                "image": "twinproduction/gatus:latest",
                "links": {"redis": {"region": "eu"}},
            },
        }
        accessories = {
            "redis": {"image": "redis:7-alpine", "regions": ["eu"]},
        }
        errors = validate_links(services, accessories)
        assert len(errors) == 1
        assert "same-stack dependency" in errors[0]

    def test_cross_region_link_still_passes_after_same_stack_check(self):
        """The new same-stack check must not catch legitimate
        cross-region links (svc regions differ from link region)."""
        services = {
            "storefront-na": {
                "image": "example/storefront:latest",
                "regions": ["na"],
                "links": {"postgres": {"region": "eu"}},
            },
        }
        accessories = {
            "postgres": {"image": "postgres:17", "regions": ["eu"]},
        }
        errors = validate_links(services, accessories)
        assert errors == []

    def test_yaml_round_trip(self, tmp_path):
        """links: field survives YAML round-trip via ruamel.yaml."""
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.preserve_quotes = True

        services_content = {
            "services": {
                "n8n": {
                    "access": "vpn",
                    "image": "n8nio/n8n:latest",
                    "domains": ["n8n.example.com"],
                    "ports": {"internal": 5678},
                    "links": {
                        "postgres": {"region": "eu"},
                        "redis": {"region": "eu"},
                    },
                },
            },
            "accessories": {
                "postgres": {
                    "image": "postgres:16",
                    "port": "127.0.0.1:5432:5432",
                },
            },
        }

        # Write
        services_path = tmp_path / "group_vars" / "all"
        services_path.mkdir(parents=True)
        with (services_path / "services.yml").open("w") as f:
            yaml.dump(services_content, f)

        # Read back
        with (services_path / "services.yml").open() as f:
            loaded = yaml.load(f)

        # Verify links preserved
        assert "links" in loaded["services"]["n8n"]
        links = loaded["services"]["n8n"]["links"]
        assert "postgres" in links
        assert links["postgres"]["region"] == "eu"
        assert "redis" in links
        assert links["redis"]["region"] == "eu"


class TestLinkEnvVarName:
    """link_env_var_name() conversion."""

    def test_simple(self):
        assert link_env_var_name("postgres") == "POSTGRES"

    def test_hyphenated(self):
        assert link_env_var_name("my-redis") == "MY_REDIS"

    def test_underscored(self):
        assert link_env_var_name("my_cache") == "MY_CACHE"

    def test_mixed(self):
        assert link_env_var_name("my-db_cache") == "MY_DB_CACHE"
