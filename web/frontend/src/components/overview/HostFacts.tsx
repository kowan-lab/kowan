import type { ReactNode } from "react";
import type { DiskUsage, Host, MetricSample, SystemInfo } from "../../types";
import { LineChart, Text } from "@/ui/kit";
import { cn } from "@/lib/utils";
import { formatBytes } from "../../utils";
import { age, hostKind } from "./model";
import { Bar, Btn, FRAME, SectHead, SevLabel } from "./ui";

/**
 * One host's resources as the workload page shows a workload's: CPU, memory
 * and disk side by side, each with its value, a note and (CPU and memory) the
 * last hour from the monitor.
 */
export function HostResources({
  host, system, samples, disk, canOperate,
}: {
  host: Host;
  system?: SystemInfo;
  samples: MetricSample[];
  disk?: DiskUsage;
  canOperate: boolean;
}) {
  const last = samples[samples.length - 1];
  const memPct = last && last.memTotal ? (last.memUsed / last.memTotal) * 100 : null;
  const dockerData = disk ? disk.imagesSize + disk.containersSize + disk.volumesSize + disk.buildCacheSize : null;
  const reclaimable = disk ? disk.imagesReclaimable + disk.volumesReclaimable + disk.buildCacheSize : 0;
  const labels = samples.map((m) => new Date(m.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }));
  const chart = samples.length >= 2;
  const cols: { label: string; value: ReactNode; note: ReactNode; body: ReactNode }[] = [
    {
      label: "CPU",
      value: last ? <>{last.cpuPercent.toFixed(1)}<span className="text-[14px] text-muted-foreground"> %</span></> : "—",
      note: system ? `${system.ncpu} cores` : "",
      body: chart && <LineChart labels={labels} series={[{ name: "CPU", data: samples.map((m) => Number(m.cpuPercent.toFixed(1))), color: "primary" }]} yMax={100} unit="%" height={120} />,
    },
    {
      label: "Memory",
      value: last ? formatBytes(last.memUsed) : system ? formatBytes(system.memTotal) : "—",
      note: memPct != null ? `${Math.round(memPct)}% of ${formatBytes(last!.memTotal)}` : "",
      body: chart && <LineChart labels={labels} series={[{ name: "Memory", data: samples.map((m) => (m.memTotal ? Number(((m.memUsed / m.memTotal) * 100).toFixed(1)) : 0)), color: "green" }]} yMax={100} unit="%" height={120} />,
    },
    {
      label: "Disk",
      value: dockerData != null ? formatBytes(dockerData) : "—",
      note: dockerData != null ? (reclaimable > 0 ? `Docker data · ${formatBytes(reclaimable)} reclaimable` : "Docker data · nothing to reclaim") : "",
      body: disk && (
        <dl className="m-0 grid grid-cols-2 gap-x-[16px]">
          {([
            ["Images", disk.imagesSize, `${disk.imagesCount}`],
            ["Containers", disk.containersSize, `${disk.containersCount}`],
            ["Volumes", disk.volumesSize, `${disk.volumesCount}`],
            ["Build cache", disk.buildCacheSize, ""],
          ] as [string, number, string][]).map(([k, size, n], i) => (
            <div key={k} className={cn("flex min-w-0 flex-col gap-[6px] py-[8px] text-[13px] leading-[18px]", i < 2 && "shadow-[inset_0_-1px_0_var(--border)]")}>
              <dt className="text-muted-foreground">{k}{n && <span> · {n}</span>}</dt>
              <dd className="m-0 flex min-w-0 items-center gap-[8px] font-mono">
                <Bar share value={dockerData ? (size / dockerData) * 100 : 0} />
                {formatBytes(size)}
              </dd>
            </div>
          ))}
        </dl>
      ),
    },
  ];
  return (
    <section className={FRAME}>
      <SectHead
        title="Resources"
        label="last hour · every 15 s"
        right={canOperate && reclaimable > 0 && <Btn sm to={`/hosts/${encodeURIComponent(host.id)}/cleanup`}>Clean up</Btn>}
      />
      <dl className="m-0 grid gap-[24px] p-[16px] lg:grid-cols-3">
        {cols.map((c) => (
          <div key={c.label} className="flex min-w-0 flex-col gap-[4px]">
            <dt className="text-[12px] leading-[16px] text-muted-foreground">{c.label}</dt>
            <dd className="m-0 flex min-w-0 flex-col gap-[4px]">
              <span className="text-[24px] leading-[32px] font-medium">{c.value}</span>
              <span className="min-h-[18px] text-[13px] leading-[18px] text-muted-foreground">{c.note}</span>
              {c.body && <div className="pt-[8px]">{c.body}</div>}
            </dd>
          </div>
        ))}
      </dl>
      {!chart && <Text color="muted" size="sm" className="px-[16px] pb-[16px]">The monitor has not recorded samples yet.</Text>}
    </section>
  );
}

/** D's offline banner for a host that stopped answering. */
export function OfflineBanner({ host }: { host: Host }) {
  return (
    <section className="flex flex-wrap items-center gap-[16px] px-[16px] py-[14px] bg-[color-mix(in_srgb,var(--tone-red-solid)_7%,var(--card))] shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--tone-red-solid)_45%,var(--card))]">
      <SevLabel sev="critical" />
      <div className="min-w-0 flex-1">
        <h3 className="m-0 text-[15px] leading-[20px] font-semibold">
          Offline — {host.lastSeen ? `No heartbeat from ${host.name || host.id} for ${age(host.lastSeen)}` : `${host.name || host.id} has never connected`}
        </h3>
        <p className="m-0 pt-[2px] text-[14px] leading-[20px] text-muted-foreground">
          {hostKind(host).kind}. Containers, metrics and actions resume when the agent answers again.
        </p>
      </div>
      <Btn to={`/hosts/${host.id}`}>Open host</Btn>
    </section>
  );
}
