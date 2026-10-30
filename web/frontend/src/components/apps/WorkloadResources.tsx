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

  const rx = rates(points, "netRx");
  const tx = rates(points, "netTx");
  const rate = (v: number | undefined) => (v === undefined ? "—" : `${formatBytes(v)}/s`);
  const history = points.length >= 2;
  const kpis = [
    {
      label: "CPU",
      value: s ? <>{s.last.cpu.toFixed(1)}<span className="text-[14px] text-muted-foreground"> %</span></> : "—",
      note: s ? `peak ${s.peakCpu.toFixed(1)}% · 100% = the whole host` : "",
      chart: (
        <LineChart
          height={160}
          unit="%"
          yMax={100}
          labels={labels}
          series={[
            { name: "Average", data: points.map((p) => p.cpu), color: "primary" },
            { name: "Peak", data: points.map((p) => p.cpuMax), color: "amber" },
          ]}
        />
      ),
    },
    {
      label: "Memory",
      value: s ? formatBytes(s.last.mem) : "—",
      note: s ? `peak ${formatBytes(s.peakMem)}${limit ? ` · limit ${formatBytes(limit)}` : " · no limit"} · chart in MiB` : "",
      chart: (
        <LineChart
          height={160}
          unit=""
          labels={labels}
          series={[
            { name: "Average", data: points.map((p) => Math.round(p.mem / MiB)), color: "green" },
            { name: "Peak", data: points.map((p) => Math.round(p.memMax / MiB)), color: "amber" },
          ]}
        />
      ),
    },
    {
      label: "Network",
      // The last bucket's rate, in plus out; the note splits it.
      value: history ? rate(rx[rx.length - 1] + tx[tx.length - 1]) : "—",
      note: history ? `in ${rate(rx[rx.length - 1])} · out ${rate(tx[tx.length - 1])} · chart in KiB/s` : "",
      chart: (
        <LineChart
          height={160}
          unit=""
          labels={labels}
          series={[
            { name: "In", data: rx.map((v) => Math.round(v / 102.4) / 10), color: "cyan" },
            { name: "Out", data: tx.map((v) => Math.round(v / 102.4) / 10), color: "purple" },
          ]}
        />
      ),
    },
  ];

  return (
    <Band
      card
      title="Resources"
      right={
        <Tabs variant="segmented" value={hours} onValueChange={setHours}>
          <Tabs.List>{RANGES.map((r) => <Tabs.Trigger key={r.value} value={r.value}>{r.label}</Tabs.Trigger>)}</Tabs.List>
        </Tabs>
      }
    >
      <div className="flex flex-col gap-[16px] p-[16px]">
        {q.data?.growth?.growing && (
          <Callout color="warning" title="Memory keeps growing">
            {growthText(q.data.growth)}
          </Callout>
        )}
        <dl className="m-0 grid gap-[24px] lg:grid-cols-3">
          {kpis.map((k) => (
            <div key={k.label} className="flex min-w-0 flex-col gap-[4px]">
              <dt className="text-[12px] leading-[16px] text-muted-foreground">{k.label}</dt>
              <dd className="m-0 flex min-w-0 flex-col gap-[4px]">
                <span className="text-[24px] leading-[32px] font-medium">{k.value}</span>
                <span className="min-h-[18px] text-[13px] leading-[18px] text-muted-foreground">{k.note}</span>
                {history && <div className="pt-[8px]">{k.chart}</div>}
              </dd>
            </div>
          ))}
        </dl>
        {!history && (
          <Text size="sm" color="muted">
            {q.isLoading ? "Loading…" : "No history for this range yet. The monitor records CPU, memory and network every few minutes while the workload runs."}
          </Text>
        )}
      </div>
    </Band>
  );
}
