"""Cross-region service link validation."""

from __future__ import annotations


def validate_links(
    services: dict,
    accessories: dict,
    inventory_regions: list[str] | None = None,
) -> list[str]:
    """Validate all links: entries across services.

    Args:
        services: The services dict from services.yml
        accessories: The accessories dict from services.yml
        inventory_regions: Optional list of known region names from inventory

    Returns:
        List of validation error messages (empty = valid)
    """
    errors: list[str] = []
    all_targets = {}
    all_targets.update(services)
    all_targets.update(accessories)

    for svc_name, svc_config in services.items():
        links = svc_config.get("links")
        if not links or not isinstance(links, dict):
            continue

        svc_regions = svc_config.get("regions", [])

        for target_name, link_config in links.items():
            # Self-referential check
            if target_name == svc_name:
                errors.append(
                    f"Service '{svc_name}': cannot link to itself"
                )
                continue

            # Target must exist
            if target_name not in all_targets:
                errors.append(
                    f"Service '{svc_name}': link target '{target_name}' not found in services or accessories"
                )
                continue

            # Region must be specified
            if not isinstance(link_config, dict) or not link_config.get("region"):
                errors.append(
                    f"Service '{svc_name}': link to '{target_name}' missing required 'region' field"
                )
                continue

            link_region = link_config["region"]

            # Validate region exists in inventory (if inventory_regions provided)
            if inventory_regions and link_region not in inventory_regions:
                errors.append(
                    f"Service '{svc_name}': link to '{target_name}' specifies unknown region '{link_region}'"
                )
                continue

            target_config = all_targets[target_name]
            target_regions = target_config.get("regions", [])

            # Same-stack check: the link is same-stack if, on
            # the host matching link_region, both the service and the
            # target are deployed. That happens when:
            #   - svc has no regions (runs everywhere) OR svc.regions
            #     includes link_region; AND
            #   - target has no regions (runs everywhere) OR
            #     target.regions includes link_region.
            # Cross-region links (e.g. svc.regions=[na] linking to a
            # target in eu) are unaffected — svc_regions=[na] excludes
            # link_region=eu so this check is skipped. Same-region
            # links, and the sandbox-shape "both have no regions"
            # links, are rejected here with guidance toward the Docker
            # `services` network and depends_on.
            svc_on_link_region = not svc_regions or link_region in svc_regions
            target_on_link_region = (
                not target_regions or link_region in target_regions
            )
            if svc_on_link_region and target_on_link_region:
                errors.append(
                    f"same-stack dependency: service '{svc_name}' links to "
                    f"'{target_name}' — both are present in region '{link_region}'. "
                    f"Same-stack containers reach each other by container name via "
                    f"the `services` Docker network. Remove `links: {target_name}:` "
                    f"from {svc_name} in services.yml and, if ordering is needed, "
                    f"use `depends_on: [{target_name}]`."
                )
                continue

            # Target must be deployed in the linked region
            if target_regions and link_region not in target_regions:
                errors.append(
                    f"Service '{svc_name}': link target '{target_name}' has regions {target_regions} which does not include '{link_region}'"
                )

    return errors


def link_env_var_name(target_name: str) -> str:
    """Convert a target service name to the env var prefix.

    Examples:
        postgres -> POSTGRES
        my-redis -> MY_REDIS
        my_cache -> MY_CACHE
    """
    return target_name.upper().replace("-", "_")
