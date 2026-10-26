"""Claim -> role and claim -> team mapping for SSO logins.

A provider's ``config`` carries::

    {
      "defaultRole": "viewer",
      "roleMappings": [{"claim": "groups", "value": "kowan-admins", "role": "admin"}],
      "teamMappings": [{"claim": "groups", "value": "web",         "team": "<team id or name>"}]
    }

``claim`` is a dot path into the claims (``realm_access.roles`` for Keycloak
realm roles); the claim value may be a string or a list of strings. The
highest matching role wins; every matching team mapping applies.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

ROLES_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def claim_values(claims: Dict[str, Any], path: str) -> List[str]:
    """String values at a dot ``path`` (a scalar becomes a one-item list)."""
    cur: Any = claims
    for part in (path or "").split("."):
        if not part:
            return []
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return []
    if isinstance(cur, (list, tuple)):
        return [str(v) for v in cur if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    if isinstance(cur, bool):
        return [str(cur).lower()]
    if isinstance(cur, (str, int, float)):
        return [str(cur)]
    return []


def _matches(claims: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    claim = str(rule.get("claim") or "groups")
    value = str(rule.get("value") or "")
    if not value:
        return False
    return value in claim_values(claims, claim)


def map_role(claims: Dict[str, Any], config: Dict[str, Any]) -> str:
    best = str(config.get("defaultRole") or "viewer")
    if best not in ROLES_RANK:
        best = "viewer"
    for rule in config.get("roleMappings") or []:
        if not isinstance(rule, dict):
            continue
        role = str(rule.get("role") or "")
        if role in ROLES_RANK and _matches(claims, rule) and ROLES_RANK[role] > ROLES_RANK[best]:
            best = role
    return best


def mapped_role_matched(claims: Dict[str, Any], config: Dict[str, Any]) -> bool:
    return any(isinstance(r, dict) and _matches(claims, r) for r in config.get("roleMappings") or [])


def map_teams(claims: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    """Team references (id or name) the claims map to, de-duplicated in order."""
    out: List[str] = []
    for rule in config.get("teamMappings") or []:
        if not isinstance(rule, dict):
            continue
        team = str(rule.get("team") or "").strip()
        if team and _matches(claims, rule) and team not in out:
            out.append(team)
    return out


def validate_rules(rules: Iterable[Any], target_key: str) -> List[Dict[str, str]]:
    """Normalize and validate a mapping list from the admin API."""
    out: List[Dict[str, str]] = []
    for r in rules or []:
        if not isinstance(r, dict):
            raise ValueError("mapping entries must be objects")
        claim = str(r.get("claim") or "groups").strip()
        value = str(r.get("value") or "").strip()
        target = str(r.get(target_key) or "").strip()
        if not value or not target:
            raise ValueError(f"mapping needs value and {target_key}")
        if target_key == "role" and target not in ROLES_RANK:
            raise ValueError(f"invalid role {target!r}")
        out.append({"claim": claim, "value": value, target_key: target})
    return out
