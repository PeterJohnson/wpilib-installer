"""Validate trusted publisher configuration without naming specific projects."""
import base64
import re

from .common import TARGETS, check_url


def validate_registry(registry):
    if registry.get("schema") != 1 or registry.get("target") not in TARGETS:
        raise ValueError("Unsupported registry schema or target")
    for field in ("season", "channel"):
        if not isinstance(registry.get(field), str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", registry[field]):
            raise ValueError(f"Invalid registry {field}")
    repositories = registry.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        raise ValueError("Registry must configure at least one repository")
    owned, tuf_ids = set(), set()
    for publisher, repo in repositories.items():
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", publisher)
                or re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", publisher)):
            raise ValueError(f"Unsafe publisher ID: {publisher!r}")
        if not isinstance(repo, dict):
            raise ValueError(f"Invalid repository configuration: {publisher}")
        check_url(repo["url"])
        if registry.get("online_trust") == "tuf" and "tuf" not in repo:
            raise ValueError(f"Missing TUF trust for publisher: {publisher}")
        if "tuf" in repo:
            from .tuf_repository import validate_trust
            validate_trust(repo["tuf"])
            identity = repo["tuf"]["repository_id"]
            if identity in tuf_ids:
                raise ValueError("Publishers must have independent TUF repository identities")
            tuf_ids.add(identity)
        try:
            if len(base64.b64decode(repo["public_key"], validate=True)) != 32:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid publisher public key: {publisher}") from exc
        floor = repo.get("minimum_revision", 1)
        if type(floor) is not int or floor < 1:
            raise ValueError(f"Invalid revision floor: {publisher}")
        components = repo.get("components")
        if not isinstance(components, list) or not components:
            raise ValueError(f"Missing component allowlist: {publisher}")
        for component in components:
            if not isinstance(component, str) or not re.fullmatch(r"[a-z][a-z0-9.]+", component):
                raise ValueError(f"Invalid component ID: {component!r}")
            if component in owned:
                raise ValueError(f"Component has multiple owners: {component}")
            owned.add(component)
    order = registry.get("repository_order", sorted(repositories))
    if (not isinstance(order, list) or any(not isinstance(p, str) for p in order)
            or len(order) != len(repositories) or set(order) != set(repositories)):
        raise ValueError("repository_order must list every configured publisher exactly once")
    profiles = registry.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles must map names to component lists")
    for name, components in profiles.items():
        if (not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or not isinstance(components, list)
                or not components or any(not isinstance(c, str) or c not in owned for c in components)):
            raise ValueError(f"Invalid profile or unowned component: {name}")
    return order
