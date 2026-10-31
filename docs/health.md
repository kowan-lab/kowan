# Health and inventory

`GET /system/health` (admin, `kowan/api/health.py`): server version and start,
database sizes (`kowan.db`, `logs.db` with their WAL), free disk of the data
folder, every host with a timed `ping` (3 s), last seen and crash-watch state,
the background loops' last run (`Monitor.last_tick_at`,
`AutoUpdater.last_tick_at`, `UptimeRunner.last_tick_at`,
`LogCollector.last_sync_at`) against their interval (late after three
intervals), and work in flight. `status` is `degraded` with `problems` when a
loop is late, a host is offline or the disk is under 5% free.

`GET /inventory?format=csv|json&group=` (`kowan/api/inventory.py`): one row
per visible container (Kowan's support containers left out), CSV as a
download, JSON with the hosts that could not be read.
