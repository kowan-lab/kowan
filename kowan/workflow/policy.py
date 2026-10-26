"""Who may manage workflows with script steps.

A script step runs bash on the control plane, so only an admin may create,
change, run or expose (webhook) a workflow that contains one. REST and MCP both
call this, so the rule cannot drift between them.
"""

from __future__ import annotations

from typing import Iterable, Optional

from kowan.models import WorkflowStepScript

SCRIPT_ADMIN_ONLY = "workflows with script steps can only be managed by an admin"


def has_script(steps: Optional[Iterable]) -> bool:
    return any(getattr(st, "type", "") == WorkflowStepScript for st in (steps or []))


def script_rights_problem(role: str, *step_lists) -> str:
    """The reason ``role`` may not act on these steps, or "" when it may."""
    if role == "admin" or not any(has_script(steps) for steps in step_lists):
        return ""
    return SCRIPT_ADMIN_ONLY
