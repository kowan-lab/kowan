"""Auto-update policies for containers and deployments.

  * :mod:`kowan.autoupdate.policy` - pure: modes, label precedence, maintenance
    windows, minimum image age (tests/test_autoupdate.py).
  * :mod:`kowan.autoupdate.engine` - safe switch: pull, scan gate, recreate,
    health verification, automatic rollback.
  * :mod:`kowan.autoupdate.runner` - :class:`AutoUpdater`, the background loop.

HTTP surface in :mod:`kowan.api.autoupdate`; labels documented in docs/labels.md.
"""
