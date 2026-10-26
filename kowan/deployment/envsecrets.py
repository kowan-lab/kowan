"""Secret rows of a workload's environment.

An environment row (a compose deployment's stack variable, or a container
deployment's ``spec.env`` entry) marked ``secret`` keeps its value out of the
deployment: the value goes to one encrypted secret the deployment owns,
``<deployment>-env``, a field per row, and the row keeps only the reference
``[[<deployment>-env.KEY]]``. So the value is never in the deployments table,
its revisions or the API; anyone who may edit the deployment can set it, only
an admin can reveal it (Secrets page). The secret is deleted with the
deployment, and loses a field when its row goes or turns into config.

Rows that are not secret are kept as written, ``[[shared.secret]]`` references
to secrets made on the Secrets page included.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional

from kowan.errors import Validation
from kowan.models import Deployment
from kowan.models.credential import Credential, CredentialKindSecret
from kowan.models.deployment import KeyValue
from kowan.models.secret import SecretTypeGeneric

_OWNER = "deploymentId"


def env_rows(dep: Deployment) -> List[KeyValue]:
    """The rows of ``dep``'s environment: stack variables, or the container's env."""
    if dep.type == "compose":
        return dep.variables
    return dep.spec.env if dep.spec is not None else []


def owned_secret(store: Any, dep_id: str) -> Optional[Credential]:
    for c in store.list_credentials(CredentialKindSecret):
        if (c.meta or {}).get(_OWNER) == dep_id:
            return c
    return None


def _free_name(store: Any, dep: Deployment) -> str:
    taken = {c.name for c in store.list_credentials(CredentialKindSecret)}
    base = f"{dep.resource_name()}-env"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def apply(store: Any, dep: Deployment, now: str) -> bool:
    """Move ``dep``'s secret row values into its secret and leave references.

    A secret row whose value is a reference to this deployment's own secret
    (what the API returns, or "" from a form left untouched) keeps the stored
    value; any other value is the new one. Raises Validation when a new secret
    row has no value. Returns whether any secret value changed (the
    references alone do not show it).
    """
    rows = [r for r in env_rows(dep) if r.secret]
    owned = owned_secret(store, dep.id)
    stored: Dict[str, str] = store.credential_config(owned.id) if owned is not None else {}
    name = owned.name if owned is not None else _free_name(store, dep)
    own_ref = re.compile(r"^\[\[\s*" + re.escape(name) + r"\.([A-Za-z0-9_.:-]+?)\s*\]\]$")

    data: Dict[str, str] = {}
    for r in rows:
        m = own_ref.match(r.value or "")
        if m is not None or r.value == "":
            # Unchanged (or renamed: the reference still names the old field).
            old = m.group(1) if m is not None else r.key
            if old not in stored:
                raise Validation(f"set a value for the secret {r.key}")
            data[r.key] = stored[old]
        else:
            data[r.key] = r.value
        r.value = f"[[{name}.{r.key}]]"

    changed = data != stored
    if not data:
        if owned is not None:
            store.delete_credential(owned.id)
        return changed
    meta = {"description": f"Environment of the workload {dep.name}", _OWNER: dep.id}
    if owned is not None:
        owned.meta = {**(owned.meta or {}), **meta}
        owned.updated_at = now
        store.update_credential(owned, data, replace=True)
        return changed
    store.create_credential(Credential(
        id=str(uuid.uuid4()), kind=CredentialKindSecret, type=SecretTypeGeneric, name=name,
        meta=meta, createdAt=now, updatedAt=now), data)
    return changed


def delete_for(store: Any, dep_id: str) -> None:
    """Delete the secret a deployment owns (with the deployment)."""
    owned = owned_secret(store, dep_id)
    if owned is not None:
        store.delete_credential(owned.id)
