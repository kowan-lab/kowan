"""Scheduled start/stop/restart of containers, deployments and stacks.

* :mod:`kowan.schedules.cron`    timezone-aware next-run (DST aware), shared.
* :mod:`kowan.schedules.runner`  CronRunner: the missed-run / overlap policy, shared
  with :mod:`kowan.volbackup`.
* :mod:`kowan.schedules.service` validation, ``kowan.schedule.*`` label discovery
  and precedence, execution and run history.
"""
