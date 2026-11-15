// What uses a host: each workload's CPU and memory over the chosen range, the
// biggest ones stacked on a chart (the rest as "Other"), and a table of all of
// them, from the monitor's 5-minute workload history.

import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { AUTO, LineChart, Tabs, Text, toneVars, type Color } from "@/ui/kit";
import { cn } from "@/lib/utils";
import { getHostResources, listDeployments } from "../../api/client";
import { appHref } from "../../lib/apps";
import { formatBytes } from "../../utils";
import { pointLabel, RANGES } from "../apps/WorkloadResources";
import { Bar, FRAME, SectHead } from "./ui";

const MiB = 1024 * 1024;
type Metric = "mem" | "cpu";

/** A workload id as people know it: the deployment, project or container name. */
export function workloadLabel(id: string, deployments: Map<string, string>): string {
  const [kind, ...rest] = id.split("~");
  if (kind === "dep") return deployments.get(rest[0]) ?? rest[0];
  return rest[rest.length - 1] ?? id;
}

export default function HostBreakdown({ hostId, memTotal }: { hostId: string; memTotal?: number }) {
  const [hours, setHours] = useState("24");
  const [metric, setMetric] = useState<Metric>("mem");
  const q = useQuery({
    queryKey: ["host-resources", hostId, hours],
    queryFn: () => getHostResources(hostId, Number(hours)),
    refetchInterval: 60_000,
    retry: false,
  });
  const deps = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, staleTime: 30_000 });
  const names = useMemo(() => new Map((deps.data ?? []).map((d) => [d.id, d.name])), [deps.data]);
  if (q.isError) return null;

  // Tolerate a partial answer: an empty breakdown rather than a crash.
  const data = q.data && Array.isArray(q.data.times) ? q.data : undefined;
  const h = Number(hours);
  const labels = (data?.times ?? []).map((t) => pointLabel(t, h));
  // The charted workloads for this metric: the ones the server picked, biggest first.
  const charted = (data?.series ?? [])
    .map((s) => ({ ...s, total: (s[metric] ?? []).reduce((a, v) => a + v, 0) }))
    .filter((s) => s.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, 6);
  const color = new Map<string, Color>(charted.map((s, i) => [s.workload, AUTO[i % AUTO.length]]));
  const scale = (v: number) => (metric === "mem" ? Math.round(v / MiB) : Math.round(v * 10) / 10);
  const otherData = (data?.other?.[metric] ?? []).map((v, i) =>
    v + (data?.series ?? []).filter((s) => !color.has(s.workload)).reduce((a, s) => a + (s[metric]?.[i] ?? 0), 0),
  );
  const series = [
    ...charted.map((s) => ({ name: workloadLabel(s.workload, names), data: s[metric].map(scale), color: color.get(s.workload) })),
    ...(otherData.some((v) => v > 0) ? [{ name: "Other", data: otherData.map(scale), color: "gray" as Color }] : []),
  ];
  const rows = [...(data?.workloads ?? [])].sort((a, b) => (metric === "mem" ? b.mem - a.mem : b.cpu - a.cpu));
  const biggest = Math.max(1, ...rows.map((r) => (metric === "mem" ? r.memMax : r.cpuMax)));

  return (
    <section className={FRAME}>
      <SectHead
        title="What uses this host"
        label="by workload · every 5 min"
        right={
          <div className="flex flex-wrap items-center gap-[8px]">
            <Tabs variant="segmented" value={metric} onValueChange={(v) => setMetric(v as Metric)}>
              <Tabs.List>
                <Tabs.Trigger value="mem">Memory</Tabs.Trigger>
                <Tabs.Trigger value="cpu">CPU</Tabs.Trigger>
              </Tabs.List>
            </Tabs>
            <Tabs variant="segmented" value={hours} onValueChange={setHours}>
              <Tabs.List>{RANGES.map((r) => <Tabs.Trigger key={r.value} value={r.value}>{r.label}</Tabs.Trigger>)}</Tabs.List>
            </Tabs>
          </div>
        }
      />
      {labels.length >= 2 ? (
        <div className="flex flex-col gap-[16px] p-[16px]">
          <LineChart labels={labels} series={series} stacked legend={false} height={200} unit={metric === "mem" ? "" : "%"} />
          <Text size="xs" color="muted">
            {metric === "mem" ? "Stacked, in MiB" : "Stacked, as a share of the host (100% = every core busy)"}
            {metric === "mem" && memTotal ? ` · the host has ${formatBytes(memTotal)}` : ""}. Hover the chart for each workload's value.
          </Text>
          <div className="-mx-[16px] overflow-x-auto">
            <table className="w-full min-w-[560px] border-collapse text-[13px] leading-[18px]">
              <thead>
                <tr className="text-left text-[12px] text-muted-foreground shadow-[inset_0_-1px_0_var(--border)]">
                  <th className="px-[16px] py-[8px] font-normal">Workload</th>
                  <th className="px-[8px] py-[8px] text-right font-normal">Now</th>
                  <th className="px-[8px] py-[8px] text-right font-normal">Average</th>
                  <th className="px-[8px] py-[8px] text-right font-normal">Peak</th>
                  <th className="px-[16px] py-[8px] font-normal"><span className="sr-only">Share of the biggest peak</span></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const c = color.get(r.workload);
                  const fmt = (v: number) => (metric === "mem" ? formatBytes(v) : `${v.toFixed(1)}%`);
                  return (
                    <tr key={r.workload} className="shadow-[inset_0_-1px_0_var(--border)]">
                      <td className="max-w-[320px] px-[16px] py-[8px]">
                        <span className="flex min-w-0 items-center gap-[8px]">
                          <span aria-hidden className={cn("size-[10px] shrink-0", !c && "bg-muted shadow-[inset_0_0_0_1px_var(--border)]")} style={c ? { background: (toneVars(c) as Record<string, string>)["--t-solid"] } : undefined} />
                          <Link to={appHref(r.workload)} className="truncate font-medium hover:underline">{workloadLabel(r.workload, names)}</Link>
                          <span className="shrink-0 text-muted-foreground">{r.containers > 1 ? `${r.containers} containers` : ""}</span>
                        </span>
                      </td>
                      <td className="px-[8px] py-[8px] text-right font-mono tabular-nums">{fmt(metric === "mem" ? r.memNow : r.cpuNow)}</td>
                      <td className="px-[8px] py-[8px] text-right font-mono tabular-nums">{fmt(metric === "mem" ? r.mem : r.cpu)}</td>
                      <td className="px-[8px] py-[8px] text-right font-mono tabular-nums">{fmt(metric === "mem" ? r.memMax : r.cpuMax)}</td>
                      <td className="px-[16px] py-[8px]"><Bar value={((metric === "mem" ? r.memMax : r.cpuMax) / biggest) * 100} share /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <Text size="sm" color="muted" className="block p-[16px]">
          {q.isLoading ? "Loading…" : "No history for this range yet. The monitor records each workload every 5 minutes."}
        </Text>
      )}
    </section>
  );
}
