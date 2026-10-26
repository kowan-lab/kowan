"""Volume backups with restic (see docs/volume-backups.md).

* :mod:`kowan.volbackup.restic`   pure helpers: repository env, retention flags,
  the in-container shell scripts and restic JSON output parsing.
* :mod:`kowan.volbackup.executor` runs a short-lived ``restic/restic`` container
  on a host through the Docker ``Ops`` interface (works on remote agents).
* :mod:`kowan.volbackup.service`  repositories/jobs validation, volume selection,
  hooks, backup/restore/browse, run history, failure alerts and the schedule.
"""
