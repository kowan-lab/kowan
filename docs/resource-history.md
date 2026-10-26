# Resource history and the memory-growth warning

CPU, memory and network per workload over time, on its overview (6 h, 24 h, 7 d,
30 d), and a warning when memory keeps climbing without a restart.

Code: `kowan/monitor/workloads.py` (buckets, trend, alert),
`kowan/store/workload_metrics.py`, `kowan/api/workloadres.py`; UI:
`components/apps/WorkloadResources.tsx`.

## Recording

The monitor already samples every running container's stats each tick. They
are folded into one row per workload, host and 5-minute bucket: average and
peak CPU, average and peak memory, the memory limit, network counters and
the container count. Ranges over two days are read in hourly buckets. History
is kept 30 days.

## Memory growth

When a bucket closes, the last 6 hours of the workload's memory get a
least-squares line. It counts as a steady climb (a likely leak) when there
are at least 24 points over at least 2 hours, the line explains them well
(R² ≥ 0.8), the rise is at least 50 MiB and 20% of the starting memory, and
the container count never changed (a restart or a scale resets memory). Then
a warning alert `memgrowth:<workload>` is raised, with the pace and, with a
limit, the hours left before it is reached; it resolves when the climb stops.

## API

```
GET /api/v1/resource-history/{workload}?hours=24     (1..720)
→ {hours, points: [{time, containers, cpu, cpuMax, mem, memMax, memLimit, netRx, netTx}], growth}
```

Team-scoped users need read access to the workload's containers (or its
deployment).
