"""Transactional self-update and rollback for the server and the agents.

See ``docs/updates-design.md`` for the full design. The package is split so the
pure, testable logic is separate from the Docker plumbing:

  * :mod:`.plan`        — the update plan handed to the helper (base64 JSON), the
                          helper's JSON-lines journal and its result parsing.
  * :mod:`.clone`       — build a ``POST /containers/create`` body that clones an
                          existing container onto a new image (pure).
  * :mod:`.verify`      — the "is the new container healthy?" rules (pure
                          evaluation + an async polling loop).
  * :mod:`.engine`      — the small Docker Engine surface the helper needs, and
                          its implementation over :class:`kowan.docker.LocalClient`.
  * :mod:`.dbsnap`      — server database snapshot / restore / retention.
  * :mod:`.helper`      — the one-shot helper container (``self-update``): the
                          swap state machine with automatic rollback.
  * :mod:`.orchestrator`— the control-plane side: status, lock, pull, preflight,
                          snapshot, helper launch, rolling agent batches.
  * :mod:`.reconcile`   — fold finished helpers back into the ``updates`` table.
  * :mod:`.preflight`   — the ``preflight`` and ``healthcheck`` sub-commands.

Why a helper container at all: a process cannot replace the container it runs
in (removing the container kills the process half-way). The helper is a
SEPARATE container started from the CURRENT (known-good) image, so a broken new
image can never break the code that has to roll it back.
"""

from __future__ import annotations

COMPONENT_SERVER = "server"
COMPONENT_AGENT = "agent"
COMPONENTS = (COMPONENT_SERVER, COMPONENT_AGENT)

MODE_UPDATE = "update"
MODE_ROLLBACK = "rollback"

STATUS_RUNNING = "running"
STATUS_UPDATED = "updated"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"
STATUS_UNKNOWN = "unknown"
FINAL_STATUSES = (
    STATUS_UPDATED, STATUS_ROLLED_BACK, STATUS_FAILED, STATUS_ABORTED, STATUS_UNKNOWN,
)

# Labels put on every container this package creates.
LABEL_ROLE = "kowan.role"
LABEL_UPDATE_ID = "kowan.update.id"
# OCI version label the images carry (build arg VERSION).
LABEL_VERSION = "org.opencontainers.image.version"

DOCKER_SOCK = "/var/run/docker.sock"


def previous_name(name: str) -> str:
    """Name of the parked previous container of ``name``."""
    return f"{name}-previous"


def helper_name(component: str, update_id: str) -> str:
    return f"kowan-{component}-updater-{update_id}"


def preflight_name(component: str, update_id: str) -> str:
    return f"kowan-{component}-preflight-{update_id}"
