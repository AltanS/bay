"""Tests for bay_image_consumers and bay_image_region_map filters.

Verifies that the image consumer mapping filters correctly:
- Map image refs to the services that use them
- Handle services with no image field
- Handle services not in the active service list
- Compute image-to-region mapping for build server fan-out
- Handle shared images across regions (storefront pattern)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import custom filters from the filter_plugins directory
_filter_dir = str(
    Path(__file__).resolve().parent.parent / "filter_plugins"
)
if _filter_dir not in sys.path:
    sys.path.insert(0, _filter_dir)

from bay_filters import bay_image_consumers, bay_image_region_map


# ── Test fixtures ────────────────────────────────────────────────────


def _storefront_services():
    """Multi-service consumer topology: 1 builder + 5 image-only + 1 cross-region."""
    return {
        "storefront-de": {
            "build": {
                "repo": "git@github.com:acmecorp/storefront.git",
                "branch": "master",
                "strategy": "remote",
            },
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["storefront.de"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-es": {
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["ketocontrol.es"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-it": {
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["ketocontrollo.it"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-fr": {
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["storefront.fr"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-nl": {
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["storefront.nl"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "storefront-com": {
            "image": "registry.infra.example.com/demo/storefront-storefront:latest",
            "access": "public",
            "domains": ["storefront.com"],
            "ports": {"internal": 3000},
            "regions": ["na"],
        },
        "storefront-platform": {
            "build": {
                "repo": "git@github.com:acmecorp/storefront.git",
                "branch": "master",
                "strategy": "remote",
                "dockerfile": "apps/platform/Dockerfile",
            },
            "image": "registry.infra.example.com/demo/storefront-platform:latest",
            "access": "vpn",
            "domains": ["platform.storefront.de"],
            "ports": {"internal": 5100},
            "regions": ["eu"],
        },
    }


def _simple_services():
    """Simple services: one builder, one image-only, one unrelated."""
    return {
        "animals": {
            "build": {
                "repo": "git@github.com:acmecorp/animals.git",
                "strategy": "remote",
            },
            "image": "registry.example.com/animals:latest",
            "access": "public",
            "domains": ["animals.example.com"],
            "ports": {"internal": 3000},
            "regions": ["eu"],
        },
        "gatus": {
            "image": "twinproduction/gatus:latest",
            "access": "vpn",
            "domains": ["status.example.com"],
            "ports": {"internal": 8080},
            "regions": ["eu"],
        },
    }


# ── bay_image_consumers tests ───────────────────────────────────────


class TestImageConsumers:
    """bay_image_consumers maps image refs to services that use them."""

    def test_shared_image_groups_all_consumers(self):
        """All services sharing an image are grouped together."""
        services = _storefront_services()
        all_names = list(services.keys())
        result = bay_image_consumers(services, all_names)

        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        assert lcc_image in result
        assert result[lcc_image] == [
            "storefront-com",
            "storefront-de",
            "storefront-es",
            "storefront-fr",
            "storefront-it",
            "storefront-nl",
        ]

    def test_separate_image_separate_group(self):
        """Services with distinct images get separate groups."""
        services = _storefront_services()
        all_names = list(services.keys())
        result = bay_image_consumers(services, all_names)

        platform_image = "registry.infra.example.com/demo/storefront-platform:latest"
        assert platform_image in result
        assert result[platform_image] == ["storefront-platform"]

    def test_only_active_services_included(self):
        """Only services in the service_names list are included."""
        services = _storefront_services()
        # EU-only: exclude storefront-com (NA)
        eu_names = [
            "storefront-de", "storefront-es", "storefront-it",
            "storefront-fr", "storefront-nl", "storefront-platform",
        ]
        result = bay_image_consumers(services, eu_names)

        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        assert lcc_image in result
        # storefront-com should NOT be in the result (not in active list)
        assert "storefront-com" not in result[lcc_image]
        assert len(result[lcc_image]) == 5

    def test_services_without_image_skipped(self):
        """Services without an image field are silently skipped."""
        services = {
            "no-image-svc": {
                "build": {"repo": "git@github.com:test/test.git"},
                "access": "public",
                "domains": ["test.com"],
                "ports": {"internal": 3000},
            },
            "with-image-svc": {
                "image": "test-image:latest",
                "access": "public",
                "domains": ["app.com"],
                "ports": {"internal": 3000},
            },
        }
        result = bay_image_consumers(
            services, ["no-image-svc", "with-image-svc"]
        )
        assert "test-image:latest" in result
        assert result["test-image:latest"] == ["with-image-svc"]
        assert len(result) == 1

    def test_empty_service_names(self):
        """Empty service names list returns empty dict."""
        services = _storefront_services()
        result = bay_image_consumers(services, [])
        assert result == {}

    def test_unknown_service_names_ignored(self):
        """Service names not in services dict are silently ignored."""
        services = _simple_services()
        result = bay_image_consumers(services, ["nonexistent", "animals"])
        assert "registry.example.com/animals:latest" in result
        assert result["registry.example.com/animals:latest"] == ["animals"]

    def test_result_sorted_deterministic(self):
        """Service names within each group are sorted alphabetically."""
        services = _storefront_services()
        all_names = list(services.keys())
        result = bay_image_consumers(services, all_names)

        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        names = result[lcc_image]
        assert names == sorted(names)

    def test_single_service_per_image(self):
        """Images used by only one service still appear in the mapping."""
        services = _simple_services()
        result = bay_image_consumers(services, ["animals", "gatus"])
        assert len(result) == 2
        assert result["registry.example.com/animals:latest"] == ["animals"]
        assert result["twinproduction/gatus:latest"] == ["gatus"]

    def test_producer_with_siblings_all_included_M90_GH_13(self):
        """GH-13 regression guard: a remote-build producer service
        that shares its image: with pull-only sibling services must appear
        in image-map.json alongside its siblings.

        Background: the storefront cutover added a remote-build
        producer (`storefront`) that consumed the same registry image
        (`storefront-storefront:latest`) as its existing sibling locales. The
        regression — silently dropping the producer from the receiver's
        fan-out recipient list — had no test guarding it. This fixture is
        the minimal reproduction: one producer + two siblings sharing an
        image. All three must appear under the shared key.

        """
        services = {
            "test-producer": {
                "build": {
                    "repo": "git@github.com:example/test-producer.git",
                    "strategy": "remote",
                },
                "image": "registry.example.com/test-producer-img:latest",
                "access": "public",
                "domains": ["test-producer.example.com"],
                "ports": {"internal": 3000},
                "regions": ["eu"],
            },
            "sibling-a": {
                "image": "registry.example.com/test-producer-img:latest",
                "access": "public",
                "domains": ["sibling-a.example.com"],
                "ports": {"internal": 3000},
                "regions": ["eu"],
            },
            "sibling-b": {
                "image": "registry.example.com/test-producer-img:latest",
                "access": "public",
                "domains": ["sibling-b.example.com"],
                "ports": {"internal": 3000},
                "regions": ["eu"],
            },
        }
        all_names = list(services.keys())
        result = bay_image_consumers(services, all_names)

        image_ref = "registry.example.com/test-producer-img:latest"
        assert image_ref in result, (
            f"GH-13: shared image key missing from image-map; "
            f"got keys {list(result)}"
        )
        consumers = result[image_ref]
        assert set(consumers) == {"test-producer", "sibling-a", "sibling-b"}, (
            f"GH-13: producer and siblings must all map to the shared "
            f"image. Got: {consumers}. Missing producer here is the exact "
            f"storefront cutover regression."
        )
        # Producer specifically — the regression dropped this one.
        assert "test-producer" in consumers, (
            "GH-13: 'test-producer' must appear under the shared image "
            "key. The bay_image_consumers filter must NOT exclude "
            "services that have a build: block."
        )


# ── bay_image_region_map tests ──────────────────────────────────────


class TestImageRegionMap:
    """bay_image_region_map maps built image refs to target regions."""

    def test_shared_image_collects_all_regions(self):
        """Shared image collects regions from all consumer services."""
        services = _storefront_services()
        build_names = ["storefront-de", "storefront-platform"]
        result = bay_image_region_map(services, build_names)

        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        assert lcc_image in result
        assert result[lcc_image] == ["eu", "na"]

    def test_single_region_image(self):
        """Image used only in one region maps to that region."""
        services = _storefront_services()
        build_names = ["storefront-de", "storefront-platform"]
        result = bay_image_region_map(services, build_names)

        platform_image = "registry.infra.example.com/demo/storefront-platform:latest"
        assert platform_image in result
        assert result[platform_image] == ["eu"]

    def test_only_remote_strategy_included(self):
        """Only services with remote/push strategy are included."""
        services = {
            "local-svc": {
                "build": {
                    "repo": "git@github.com:test/test.git",
                    "strategy": "local",
                },
                "image": "local-image:latest",
                "regions": ["eu"],
            },
            "remote-svc": {
                "build": {
                    "repo": "git@github.com:test/remote.git",
                    "strategy": "remote",
                },
                "image": "remote-image:latest",
                "regions": ["eu"],
            },
        }
        result = bay_image_region_map(services, ["local-svc", "remote-svc"])
        assert "local-image:latest" not in result
        assert "remote-image:latest" in result

    def test_push_strategy_alias_included(self):
        """Deprecated 'push' strategy is also included."""
        services = {
            "push-svc": {
                "build": {
                    "repo": "git@github.com:test/push.git",
                    "strategy": "push",
                },
                "image": "push-image:latest",
                "regions": ["eu"],
            },
        }
        result = bay_image_region_map(services, ["push-svc"])
        assert "push-image:latest" in result

    def test_no_build_services_returns_empty(self):
        """No build services in list returns empty dict."""
        services = _storefront_services()
        result = bay_image_region_map(services, [])
        assert result == {}

    def test_non_builder_service_ignored(self):
        """Services without build blocks are skipped."""
        services = _storefront_services()
        # storefront-es has no build block
        result = bay_image_region_map(services, ["storefront-es"])
        assert result == {}

    def test_build_service_without_image_skipped(self):
        """Build services without image field are skipped."""
        services = {
            "no-image": {
                "build": {
                    "repo": "git@github.com:test/test.git",
                    "strategy": "remote",
                },
                "regions": ["eu"],
            },
        }
        result = bay_image_region_map(services, ["no-image"])
        assert result == {}

    def test_regions_sorted(self):
        """Region lists are sorted alphabetically."""
        services = _storefront_services()
        build_names = ["storefront-de"]
        result = bay_image_region_map(services, build_names)

        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        regions = result[lcc_image]
        assert regions == sorted(regions)

    def test_consumer_without_regions_ignored(self):
        """Consumers without regions field do not contribute regions."""
        services = {
            "builder": {
                "build": {
                    "repo": "git@github.com:test/test.git",
                    "strategy": "remote",
                },
                "image": "shared-image:latest",
                "regions": ["eu"],
            },
            "consumer-no-regions": {
                "image": "shared-image:latest",
                # no regions field
            },
        }
        result = bay_image_region_map(services, ["builder"])
        assert result["shared-image:latest"] == ["eu"]

    def test_acme_topology(self):
        """Real demo topology produces expected fan-out map."""
        services = _storefront_services()
        services["animals"] = {
            "build": {
                "repo": "git@github.com:acmecorp/animals.git",
                "strategy": "remote",
            },
            "image": "registry.infra.example.com/demo/animals:latest",
            "regions": ["eu"],
        }

        build_names = ["storefront-de", "storefront-platform", "animals"]
        result = bay_image_region_map(services, build_names)

        assert len(result) == 3
        lcc_image = "registry.infra.example.com/demo/storefront-storefront:latest"
        assert result[lcc_image] == ["eu", "na"]
        platform_image = "registry.infra.example.com/demo/storefront-platform:latest"
        assert result[platform_image] == ["eu"]
        animals_image = "registry.infra.example.com/demo/animals:latest"
        assert result[animals_image] == ["eu"]
