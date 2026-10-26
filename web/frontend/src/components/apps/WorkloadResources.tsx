// Resources on a workload's overview: CPU, memory and network over the chosen range
// (from the monitor's 5-minute history) and a warning when memory keeps
// climbing without a restart, a likely leak.

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Callout, LineChart, Tabs, Text } from "@/ui/kit";
import { getWorkloadResources } from "../../api/client";
import { formatBytes } from "../../utils";
import type { MemoryTrend, WorkloadResourcePoint } from "../../types";
import { Band } from "./ui";

export const RANGES = [
  { value: "6", label: "6h" },
  { value: "24", label: "24h" },
  { value: "168", label: "7d" },
  { value: "720", label: "30d" },
];

const MiB = 1024 * 1024;

/** Axis label for a point: time of day for short ranges, date and hour for long ones. */
export function pointLabel(iso: string, hours: number): string {
  const d = new Date(iso);
  return hours > 48
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" })
    : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function growthText(g: MemoryTrend): string {
  let s = `Memory rose from ${formatBytes(g.start)} to ${formatBytes(g.end)} over ${Math.round(g.spanHours)}h, about ${formatBytes(g.slopeBytesPerHour)} an hour, without a restart.`;
  if (g.hoursToLimit !== null) s += ` At this pace it hits its ${formatBytes(g.limit)} limit in about ${Math.round(g.hoursToLimit)}h.`;
  return s;
}

/** Bytes per second between consecutive points; a counter reset (restart) counts as 0. */
export function rates(points: WorkloadResourcePoint[], key: "netRx" | "netTx"): number[] {
  return points.map((p, i) => {
    if (i === 0) return 0;
    const prev = points[i - 1];
    const secs = (Date.parse(p.time) - Date.parse(prev.time)) / 1000;
    const delta = p[key] - prev[key];
    return secs > 0 && delta > 0 ? delta / secs : 0;
  });
}

function summary(points: WorkloadResourcePoint[]) {
  const last = points[points.length - 1];
  const peakCpu = Math.max(...points.map((p) => p.cpuMax));
  const peakMem = Math.max(...points.map((p) => p.memMax));
  return { last, peakCpu, peakMem };
}

export default function WorkloadResources({ workload }: { workload: string }) {
  const [hours, setHours] = useState("24");
  const q = useQuery({
    queryKey: ["workload-resources", workload, hours],
    queryFn: () => getWorkloadResources(workload, Number(hours)),
    refetchInterval: 60_000,
    retry: false,
  });
  if (q.isError) return null;
  const points = q.data?.points ?? [];
  const h = Number(hours);
  const labels = points.map((p) => pointLabel(p.time, h));
  const limit = points.length ? points[points.length - 1].memLimit : 0;
  const s = points.length ? summary(points) : null;

  return (
    <Band
      title="Resources"
      count={s ? `now ${s.last.cpu.toFixed(1)}% CPU · ${formatBytes(s.last.mem)}` : undefined}
      right={
        <Tabs variant="segmented" value={hours} onValueChange={setHours}>
          <Tabs.List>
            {RANGES.map((r) => (
              <Tabs.Trigger key={r.value} value={r.value}>{r.label}</Tabs.Trigger>
            ))}
          </Tabs.List>
        </Tabs>
      }
    >
      <div className="flex flex-col gap-[12px] p-[16px]">
        {q.data?.growth?.growing && (
          <Callout color="warning" title="Memory keeps growing">
            {growthText(q.data.growth)}
          </Callout>
        )}
        {q.isLoading ? (
          <Text size="sm" color="muted">Loading…</Text>
        ) : points.length < 2 ? (
          <Text size="sm" color="muted">
            No history for this range yet. The monitor records CPU and memory every few minutes while the workload runs.
          </Text>
        ) : (
          <div className="grid gap-[16px] lg:grid-cols-3">
            <div className="min-w-0">
              <Text size="xs" color="muted">CPU (100% = one core) · peak {s!.peakCpu.toFixed(1)}%</Text>
              <LineChart
                height={160}
                unit="%"
                labels={labels}
                series={[
                  { name: "Average", data: points.map((p) => p.cpu), color: "primary" },
                  { name: "Peak", data: points.map((p) => p.cpuMax), color: "amber" },
                ]}
              />
            </div>
            <div className="min-w-0">
              <Text size="xs" color="muted">
                Memory (MiB) · peak {formatBytes(s!.peakMem)}{limit ? ` · limit ${formatBytes(limit)}` : " · no limit"}
              </Text>
              <LineChart
                height={160}
                unit=""
                labels={labels}
                series={[
                  { name: "Average", data: points.map((p) => Math.round(p.mem / MiB)), color: "green" },
                  { name: "Peak", data: points.map((p) => Math.round(p.memMax / MiB)), color: "amber" },
                ]}
              />
            </div>
            <div className="min-w-0">
              <Text size="xs" color="muted">Network (KiB/s)</Text>
              <LineChart
                height={160}
                unit=""
                labels={labels}
                series={[
                  { name: "In", data: rates(points, "netRx").map((v) => Math.round(v / 1024)), color: "cyan" },
                  { name: "Out", data: rates(points, "netTx").map((v) => Math.round(v / 1024)), color: "purple" },
                ]}
              />
            </div>
          </div>
        )}
      </div>
    </Band>
  );
}
