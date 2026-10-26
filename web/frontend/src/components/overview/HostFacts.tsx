import type { ReactNode } from "react";
import type { DiskUsage, Host, MetricSample, SystemInfo } from "../../types";
import { cn } from "@/lib/utils";
import { formatBytes } from "../../utils";
import { age, hostKind } from "./model";
import { Bar, Btn, FRAME, SevLabel } from "./ui";

/** One host's vitals as D's four-column key/value band. */
export function HostFacts({
  host, system, last, disk, canOperate,
}: {
  host: Host;
  system?: SystemInfo;
  last?: MetricSample;
  disk?: DiskUsage;
  canOperate: boolean;
}) {
  const { tunnel } = hostKind(host);
  const memPct = last && last.memTotal ? (last.memUsed / last.memTotal) * 100 : null;
  const dockerData = disk ? disk.imagesSize + disk.containersSize + disk.volumesSize + disk.buildCacheSize : null;
  const reclaimable = disk ? disk.imagesReclaimable + disk.volumesReclaimable + disk.buildCacheSize : 0;
  const cells: [string, ReactNode][] = [
    ["Containers", system ? `${system.containersRunning} running / ${system.containers}` : "—"],
    ["CPU", last ? <><Bar value={last.cpuPercent} />{Math.round(last.cpuPercent)}%{system ? <span className="text-muted-foreground">· {system.ncpu} cores</span> : null}</> : system ? `${system.ncpu} cores` : "—"],
    ["Tunnel", tunnel],
    ["Memory", memPct != null ? <><Bar value={memPct} />{formatBytes(last!.memUsed)} / {formatBytes(last!.memTotal)}</> : system ? formatBytes(system.memTotal) : "—"],
    ["Docker", system ? `${system.serverVersion} · ${system.arch} · seen ${age(host.lastSeen)}${age(host.lastSeen) === "now" ? "" : " ago"}` : `seen ${age(host.lastSeen)} ago`],
    [
      "Disk",
      dockerData != null ? (
        <>
          <span>{formatBytes(dockerData)} Docker data</span>
          {reclaimable > 0 && <span className="text-muted-foreground">· {formatBytes(reclaimable)} reclaimable</span>}
          {canOperate && reclaimable > 0 && <span className="ml-auto"><Btn sm to={`/hosts/${encodeURIComponent(host.id)}/cleanup`}>Clean up</Btn></span>}
        </>
      ) : "—",
    ],
  ];
  return (
    <section className={FRAME}>
      <dl className="m-0 grid grid-cols-[132px_minmax(0,1fr)] lg:grid-cols-[132px_minmax(0,1fr)_132px_minmax(0,1fr)]">
        {cells.map(([k, v], i) => {
          const lastRow = i >= cells.length - 2;
          const rule = lastRow ? "lg:shadow-none" : "";
          return [
            <dt key={`${k}-k`} className={cn("flex min-h-[40px] items-center px-[16px] py-[10px] font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] uppercase text-muted-foreground shadow-[inset_0_-1px_0_var(--border)]", rule, i === cells.length - 1 && "max-lg:shadow-none")}>{k}</dt>,
            <dd key={`${k}-v`} className={cn("m-0 flex min-h-[40px] flex-wrap items-center gap-[8px] py-[10px] pr-[16px] font-mono text-[13px] leading-[20px] shadow-[inset_0_-1px_0_var(--border)]", rule, i === cells.length - 1 && "max-lg:shadow-none")}>{v}</dd>,
          ];
        })}
      </dl>
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
          {host.lastSeen ? `No heartbeat from ${host.name || host.id} for ${age(host.lastSeen)}` : `${host.name || host.id} has never connected`}
        </h3>
        <p className="m-0 mt-[2px] text-[14px] leading-[20px] text-muted-foreground">
          {hostKind(host).kind}. Containers, metrics and actions resume when the agent answers again.
        </p>
      </div>
      <Btn to={`/hosts/${host.id}`}>Open host</Btn>
    </section>
  );
}
