"""Safe-update gate: may a freshly pulled image replace the running one?

Policies (UI names / ``kowan.scan.block`` label values):

  ``never``     never block (scan only for information; no scan at all when
                the gate is the only reason to scan);
  ``critical``  block when the new image has any CRITICAL vulnerability;
  ``high``      block on any CRITICAL or HIGH vulnerability;
  ``worse``     block when the new image is worse than the current one: more
                CRITICAL, or more HIGH, findings than the image it replaces.

The decision is pure so the tests can drive it with plain dicts.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

BLOCK_NEVER = "never"
BLOCK_CRITICAL = "critical"
BLOCK_HIGH = "high"
BLOCK_WORSE = "worse"
BLOCK_POLICIES = (BLOCK_NEVER, BLOCK_CRITICAL, BLOCK_HIGH, BLOCK_WORSE)



def parse_block_policy(value: Optional[str]) -> str:
    """Normalise a policy string; raises ``ValueError`` on an unknown value."""
    key = (value or "").strip().lower() or BLOCK_NEVER
    if key not in BLOCK_POLICIES:
        raise ValueError(f"invalid scan block policy {value!r} (use never|critical|high|worse)")
    return key


def gate_decision(
    policy: str,
    new_counts: Optional[Dict[str, int]],
    current_counts: Optional[Dict[str, int]] = None,
) -> Tuple[bool, str]:
    """Return ``(blocked, reason)`` for ``policy`` given the scan counts.

    ``new_counts is None`` means the new image could not be scanned: the gate
    fails closed (blocks) for every policy except ``never``. For ``worse`` a
    missing current scan is treated as "unknown baseline" and only the new
    image's own CRITICAL count blocks (so a gate never silently passes a
    critical image just because the old one was never scanned).
    """
    policy = parse_block_policy(policy)
    if policy == BLOCK_NEVER:
        return False, ""
    if new_counts is None:
        return True, "the new image could not be scanned"
    crit = int(new_counts.get("critical", 0))
    high = int(new_counts.get("high", 0))
    if policy == BLOCK_CRITICAL:
        if crit:
            return True, f"new image has {crit} critical vulnerabilit{'y' if crit == 1 else 'ies'}"
        return False, ""
    if policy == BLOCK_HIGH:
        if crit or high:
            return True, f"new image has {crit} critical and {high} high vulnerabilities"
        return False, ""
    # worse than current
    if current_counts is None:
        if crit:
            return True, f"new image has {crit} critical vulnerabilities and the current image has no scan to compare"
        return False, ""
    ccrit = int(current_counts.get("critical", 0))
    chigh = int(current_counts.get("high", 0))
    if crit > ccrit or high > chigh:
        return True, f"new image is worse than current (critical {ccrit}->{crit}, high {chigh}->{high})"
    return False, ""
