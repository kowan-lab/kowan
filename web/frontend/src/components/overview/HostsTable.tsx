import type { KeyboardEvent } from "react";
import type { Host, MetricSample, SystemInfo } from "../../types";
import { cn } from "@/lib/utils";
import { formatBytes } from "../../utils";
import { age, containerTone, hostKind, trouble, type HostContainer } from "./model";
import { Bar, Btn, Dot, FRAME, Pill, RULE_B, SectHead, Seg } from "./ui";

export interface HostRow {
  host: Host;
  system?: SystemInfo;
  last?: MetricSample;
  containers: HostContainer[];
  /** False while this host's container list is still loading. */
  loaded: boolean;
}

const MAX_TICKS = 32;

function attention(r: HostRow): { text: string; tone: "red" | "amber" | "muted" } | null {
  if (!r.host.online) return { text: "Unreachable", tone: "red" };
  let crashed = 0, unhealthy = 0;
  for (const c of r.containers) {
    const t = trouble(c, r.containers);
    if (t === "exited" || t === "dead") crashed += 1;
    else if (t) unhealthy += 1;
  }
  const parts = [crashed && `${crashed} crashed`, unhealthy && `${unhealthy} unhealthy`].filter(Boolean);
  if (!parts.length) return null;
  return { text: parts.join(" · "), tone: crashed ? "red" : "amber" };
}

function severity(r: HostRow): number {
  if (!r.host.online) return 0;
  const a = attention(r);
  return a ? (a.tone === "red" ? 1 : 2) : 3;
}

const TH = "h-[36px] px-[12px] text-left align-middle font-mono text-[12px] leading-none font-semibold tracking-[0.06em] whitespace-nowrap uppercase text-muted-foreground bg-card first:pl-[16px] last:pr-[16px] shadow-[inset_0_-1px_0_var(--border)]";
const TD = "h-[40px] px-[12px] align-middle text-[13px] leading-[18px] whitespace-nowrap first:pl-[16px] last:pr-[16px]";

export function HostsTable({ rows, scopeId, onPick }: { rows: HostRow[]; scopeId: string; onPick: (id: string) => void }) {
  const sorted = [...rows].sort((a, b) => severity(a) - severity(b) || (a.host.name || a.host.id).localeCompare(b.host.name || b.host.id));
  const key = (e: KeyboardEvent, id: string) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(id); }
  };
  return (
    <section className={FRAME}>
      <SectHead
        title="Hosts"
        label="sorted by severity"
        right={<span className="hidden text-[13px] leading-[18px] text-muted-foreground lg:inline">Click a host to focus every page on it.</span>}
      />
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr>
              <th className={TH}>Host</th>
              <th className={TH}>State</th>
              <th className={TH}>Containers</th>
              <th className={TH}>CPU</th>
              <th className={TH}>Memory</th>
              <th className={TH}>Attention</th>
              <th className={cn(TH, "text-right max-[1360px]:hidden")}>Last seen</th>
              <th className={TH}><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              const h = r.host;
              const off = !h.online;
              const { kind, tunnel } = hostKind(h);
              const gb = (b: number) => (b / 1024 ** 3).toFixed(1);
              const rule = i < sorted.length - 1 ? RULE_B : "shadow-none";
              const run = r.system?.containersRunning ?? r.containers.filter((c) => c.state.toLowerCase() === "running").length;
              const total = r.system?.containers ?? r.containers.length;
              const ticks = off ? Array.from({ length: Math.min(total, MAX_TICKS) }, () => "gray" as const) : r.containers.slice(0, MAX_TICKS).map((c) => containerTone(c, r.containers));
              const memPct = r.last && r.last.memTotal ? (r.last.memUsed / r.last.memTotal) * 100 : null;
              const att = attention(r);
              return (
                <tr
                  key={h.id}
                  tabIndex={0}
                  aria-current={h.id === scopeId ? "true" : undefined}
                  aria-label={`${h.name || h.id}: focus every page on this host`}
                  className="group cursor-pointer outline-none focus-visible:[&>td]:bg-popover hover:[&>td]:bg-popover"
                  onClick={() => onPick(h.id)}
                  onKeyDown={(e) => key(e, h.id)}
                >
                  <td className={cn(TD, rule, "group-focus-visible:shadow-[inset_2px_0_0_var(--ring)]")}>
                    <div className="flex items-center gap-[10px]">
                      <Dot tone={off ? "red" : att?.tone === "amber" ? "amber" : "green"} />
                      <div className="min-w-0">
                        <div className="font-semibold text-foreground">{h.name || h.id}</div>
                        <div className="text-muted-foreground" title={tunnel}>{kind}</div>
                      </div>
                    </div>
                  </td>
                  <td className={cn(TD, rule)}>
                    {off ? <Pill tone="red">Offline · {age(h.lastSeen)}</Pill> : <Pill tone="green">Online</Pill>}
                  </td>
                  <td className={cn(TD, rule)}>
                    {r.loaded || off ? (
                      <span className="inline-flex items-center">
                        {total > 0 && <Seg states={ticks} />}
                        {total > MAX_TICKS && <span className="ml-[2px] font-mono text-[12px] text-muted-foreground">+</span>}
                        <span className={cn("font-mono text-[13px] leading-none font-medium", total > 0 && "ml-[8px]")}>{off ? `? / ${total || "?"}` : `${run} / ${total}`}</span>
                      </span>
                    ) : <span className="text-muted-foreground">…</span>}
                  </td>
                  <td className={cn(TD, rule, "font-mono")}>
                    {r.last && !off ? (
                      <span className="inline-flex items-center"><Bar value={r.last.cpuPercent} /><span className="ml-[8px] text-[13px] leading-none font-medium">{Math.round(r.last.cpuPercent)}%</span></span>
                    ) : <span className="text-muted-foreground">—</span>}
                  </td>
                  <td className={cn(TD, rule, "font-mono")}>
                    {memPct != null && !off ? (
                      <span className="inline-flex items-center" title={`${formatBytes(r.last!.memUsed)} of ${formatBytes(r.last!.memTotal)}`}><Bar value={memPct} /><span className="ml-[8px] text-[13px] leading-none font-medium">{gb(r.last!.memUsed)} / {gb(r.last!.memTotal)} GB</span></span>
                    ) : <span className="text-muted-foreground">—</span>}
                  </td>
                  <td className={cn(TD, rule, "whitespace-normal")}>
                    {att ? (
                      <span className={att.tone === "red" ? "text-[var(--tone-red-text)]" : "text-[var(--tone-amber-text)]"}>{att.text}</span>
                    ) : <span className="text-muted-foreground">—</span>}
                  </td>
                  <td className={cn(TD, rule, "text-right font-mono max-[1360px]:hidden")}>{age(h.lastSeen)}{age(h.lastSeen) === "now" ? "" : " ago"}</td>
                  <td className={cn(TD, rule, "w-px text-right")}><Btn sm to={`/hosts/${h.id}`}>Manage</Btn></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
