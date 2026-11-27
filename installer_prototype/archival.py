"""Explicit offline authorization, independent of online TUF metadata expiry."""
import time
import re


INDEFINITE_POLICY = {"name": "wpilib-archive-v2", "lifetime": "indefinite"}


def validate_policy(policy):
    if policy == INDEFINITE_POLICY:
        return
    if (not isinstance(policy, dict) or set(policy) != {"name", "max_days"}
            or policy["name"] != "wpilib-archive-v1" or type(policy["max_days"]) is not int
            or not 1 <= policy["max_days"] <= 730):
        raise ValueError("TUF bootstrap requires an explicit archival trust policy")


def authorization(registry, record, registry_evidence, snapshots):
    if registry.get("online_trust") != "tuf":
        return None
    if registry_evidence is None or any(s.tuf_evidence is None for s in snapshots):
        raise ValueError("Archival assembly requires fresh TUF verification of every repository")
    signed = record["signed"]
    def stable(evidence):
        # Metadata-only renewal does not change the authorized catalog or ZIP.
        return {key: evidence[key] for key in ("repository_id", "catalog_sha256")}
    policy = registry["archive_policy"]
    validate_policy(policy)
    issuance = "issued_at" if policy == INDEFINITE_POLICY else "not_before"
    return {"policy": policy["name"], issuance: signed["issued_at"],
            "expires_at": signed["offline_expires_at"], "registry_tuf": stable(registry_evidence),
            "publishers_tuf": {s.publisher: stable(s.tuf_evidence) for s in snapshots}}


def verify_archival(manifest, registry, record, *, now=None):
    if registry.get("online_trust") != "tuf":
        return
    value = manifest.get("archival", {})
    signed = record["signed"]
    policy = registry.get("archive_policy", {})
    validate_policy(policy)
    indefinite = policy == INDEFINITE_POLICY
    issuance = "issued_at" if indefinite else "not_before"
    if (manifest.get("schema") != 3 or policy["name"] != value.get("policy")
            or type(value.get(issuance)) is not int or value[issuance] < 1
            or value[issuance] != signed.get("issued_at")
            or "expires_at" not in value or "offline_expires_at" not in signed
            or value["expires_at"] != signed["offline_expires_at"]
            or not isinstance(value.get("registry_tuf"), dict)
            or set(value.get("publishers_tuf", {})) != set(registry["repositories"])):
        raise ValueError("Invalid offline archival authorization")
    if indefinite:
        if value["expires_at"] is not None or "not_before" in value:
            raise ValueError("Indefinite archival authorization must have no expiry or validity window")
    elif (type(value["expires_at"]) is not int
          or not 0 < value["expires_at"] - value[issuance] <= policy["max_days"] * 86400):
        raise ValueError("Invalid offline archival authorization")
    for evidence in [value["registry_tuf"], *value["publishers_tuf"].values()]:
        if (not isinstance(evidence, dict) or not isinstance(evidence.get("repository_id"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("catalog_sha256", ""))):
            raise ValueError("Invalid archival TUF provenance")
    if any(value["publishers_tuf"][name]["repository_id"] != repo["tuf"]["repository_id"]
           for name, repo in registry["repositories"].items()):
        raise ValueError("Archival publisher TUF identity mismatch")
    if not indefinite:
        now = time.time() if now is None else now
        if not value["not_before"] <= now < value["expires_at"]:
            raise ValueError("Offline archival authorization is expired or not yet valid")
