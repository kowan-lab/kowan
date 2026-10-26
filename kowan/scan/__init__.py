"""Image vulnerability scanning (Trivy run as a short-lived container).

  * :mod:`kowan.scan.trivy`   - the pure parts: build the Trivy container spec,
    strip docker-log timestamps, parse Trivy's JSON report into a summary
    (severity counts + sorted findings). No I/O, covered offline by tests.
  * :mod:`kowan.scan.gate`    - the safe-update gate: block policies and the
    decision "may this new image replace the running one?".
  * :mod:`kowan.scan.service` - :class:`ScanService`: runs Trivy on the target
    host through the host's Docker ``Ops`` (so it works over the agent tunnel
    too), de-duplicates concurrent scans and re-uses fresh (< 24 h) results per
    image digest stored in the ``image_scans`` table.

See ``docs/scanning.md``.
"""

from .gate import (  # noqa: F401
    BLOCK_CRITICAL,
    BLOCK_HIGH,
    BLOCK_NEVER,
    BLOCK_POLICIES,
    BLOCK_WORSE,
    gate_decision,
    parse_block_policy,
)
from .trivy import (  # noqa: F401
    SEVERITIES,
    ScanSummary,
    parse_trivy_report,
)
