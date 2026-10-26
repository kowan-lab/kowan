import { useState, type ReactNode } from "react";
import type { Alert, ContainerUpdateStatus, Deployment, Host } from "../../types";
import { containerName } from "../../utils";
import {
  age, appHref, appIdFor, containerHref, containerLabel, exitCode, statusAge, trouble,
  type HostContainer,
} from "./model";
import { Btn, FRAME, HostChip, OkIcon, SectHead, SevLabel, type Sev } from "./ui";

/*
 * "Needs you": everything in scope an operator should act on, worst first, as
 * calm cards in a strict equal-height grid. One card per problem; an open
 * alert about a container that already has a card is folded into that card.
 */

interface Act {
  label: string;
  to?: string;
  onClick?: () => void;
  busy?: boolean;
}

export interface Need {
  key: string;
  sev: Sev;
  /** Order inside a severity: offline hosts, crashes, failed deploys, alerts, updates. */
  rank: number;
  host?: Host;
  title: ReactNode;
  context: string;
  when: string;
  acts: Act[];
}

const SEV_ORDER: Record<Sev, number> = { critical: 0, warning: 1, waiting: 2 };

export interface NeedsInput {
  hosts: Host[];
  containers: HostContainer[];
  deployments: Deployment[];
  /** Every deployment, to resolve a compose stack to the deployment that owns it. */
  allDeployments: Deployment[];
  alerts: Alert[];
  updates: (ContainerUpdateStatus & { hostId: string })[];
  canOperate: boolean;
  restarting: string | null;
  resolving: string | null;
  onRestart: (c: HostContainer) => void;
  onResolve: (a: Alert) => void;
}

function plural(n: number, one: string, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export function buildNeeds(i: NeedsInput): Need[] {
  const out: Need[] = [];
  const host = (id: string) => i.hosts.find((h) => h.id === id);

  // 1. Offline hosts.
  for (const h of i.hosts.filter((x) => !x.online)) {
    out.push({
      key: `host:${h.id}`,
      sev: "critical",
      rank: 0,
      host: h,
      title: `${h.name || h.id} is offline`,
      context: h.lastSeen
        ? "The agent stopped answering. Its containers show no live state until it reconnects."
        : "The agent has never connected. Run the join command on the host to bring it online.",
      when: h.lastSeen ? age(h.lastSeen) : "",
      acts: [{ label: "Open host", to: `/hosts/${h.id}` }],
    });
  }

  // 2. Containers in trouble. Alerts about the same container fold into its card.
  const alertFor = (c: HostContainer) =>
    i.alerts.find((a) => a.hostId === c._host && a.resource === containerName(c.names));
  const folded = new Set<string>();
  for (const c of i.containers) {
    const t = trouble(c);
    if (!t) continue;
    const { app, part } = containerLabel(c);
    const label = app ? (part ? `${app}: ${part}` : app) : part;
    const siblings = app ? i.containers.filter((x) => x !== c && x._host === c._host && containerLabel(x).app === app) : [];
    const up = siblings.filter((x) => x.state.toLowerCase() === "running").length;
    const others = siblings.length
      ? up === siblings.length
        ? ` The other ${plural(siblings.length, "container")} of ${app} ${siblings.length === 1 ? "is" : "are"} running.`
        : ` ${up} of the other ${siblings.length} containers of ${app} are running.`
      : "";
    const alert = alertFor(c);
    if (alert) folded.add(alert.id);
    const alertNote = alert ? " An alert is open for it." : "";
    const appLink = app ? { label: "Open workload", to: appHref(appIdFor(c, i.allDeployments)) } : { label: "Open container", to: containerHref(c) };
    const restart: Act | null = i.canOperate
      ? { label: "Restart", onClick: () => i.onRestart(c), busy: i.restarting === c.id }
      : null;

    let n: Omit<Need, "key" | "host" | "when">;
    switch (t) {
      case "exited": {
        const code = exitCode(c.status);
        n = {
          sev: "critical",
          rank: 1,
          title: `${label} exited ${code}`,
          context: `Stopped with exit code ${code}.${others}${alertNote}`,
          acts: [{ label: "Open logs", to: containerHref(c, "logs") }, appLink],
        };
        break;
      }
      case "dead":
        n = {
          sev: "critical",
          rank: 1,
          title: `${label} is dead`,
          context: `Docker could not stop or remove it cleanly.${others}${alertNote}`,
          acts: [{ label: "Open logs", to: containerHref(c, "logs") }, appLink],
        };
        break;
      case "unhealthy":
        n = {
          sev: "warning",
          rank: 1,
          title: `${label} unhealthy`,
          context: `Its health check is failing while the process keeps running.${others}${alertNote}`,
          acts: [{ label: "Open logs", to: containerHref(c, "logs") }, restart ?? appLink],
        };
        break;
      default:
        n = {
          sev: "warning",
          rank: 1,
          title: `${label} is restarting`,
          context: `The restart policy keeps bringing it back after it exits.${others}${alertNote}`,
          acts: [{ label: "Open logs", to: containerHref(c, "logs") }, appLink],
        };
    }
    out.push({ ...n, key: `ctr:${c._host}:${c.id}`, host: host(c._host), when: statusAge(c.status) });
  }

  // 3. Failed deployments.
  for (const d of i.deployments.filter((x) => x.status.toLowerCase() === "error")) {
    const h = host(d.targets[0] ?? "");
    out.push({
      key: `dep:${d.id}`,
      sev: "critical",
      rank: 2,
      host: h,
      title: `${d.name}: deploy failed`,
      context: `The last ${d.type === "compose" ? "compose" : "container"} deploy ended in an error. Its logs say why; redeploy from the workload once fixed.`,
      when: age(d.updatedAt),
      acts: [{ label: "Open logs", to: `/workloads/dep~${d.id}?tab=logs` }, { label: "Open workload", to: appHref(`dep~${d.id}`) }],
    });
  }

  // 4. Open alerts not already shown on a container card.
  for (const a of i.alerts.filter((x) => !folded.has(x.id))) {
    const lvl = a.level.toLowerCase();
    out.push({
      key: `alert:${a.id}`,
      sev: lvl === "critical" ? "critical" : lvl === "warning" ? "warning" : "waiting",
      rank: 3,
      host: host(a.hostId),
      title: a.title,
      context: a.message || (a.resource ? `Raised for ${a.resource}.` : "Raised by an alert rule."),
      when: age(a.createdAt),
      acts: [
        { label: "Open alerts", to: "/monitoring" },
        ...(i.canOperate ? [{ label: "Resolve", onClick: () => i.onResolve(a), busy: i.resolving === a.id }] : []),
      ],
    });
  }

  // 5. Updates waiting: one card for the scope.
  if (i.updates.length) {
    const byApp = new Map<string, number>();
    for (const u of i.updates) {
      const c = i.containers.find((x) => x._host === u.hostId && x.id === u.containerId);
      const name = c ? (containerLabel(c).app ?? containerLabel(c).part) : u.containerName.replace(/^\//, "");
      byApp.set(name, (byApp.get(name) ?? 0) + 1);
    }
    const list = [...byApp].map(([n, k]) => (k > 1 ? `${n} (${k})` : n));
    const hostIds = new Set(i.updates.map((u) => u.hostId));
    const newest = i.updates.reduce((t, u) => (u.checkedAt > t ? u.checkedAt : t), "");
    out.push({
      key: "updates",
      sev: "waiting",
      rank: 4,
      host: hostIds.size === 1 ? host([...hostIds][0]) : undefined,
      title: `Updates waiting for ${plural(i.updates.length, "container")}`,
      context: `Newer image digests for ${list.slice(0, 6).join(", ")}${list.length > 6 ? ` and ${list.length - 6} more` : ""}.`,
      when: newest ? age(newest) : "",
      acts: [{ label: "Review", to: "/auto-update" }],
    });
  }

  return out.sort((a, b) => SEV_ORDER[a.sev] - SEV_ORDER[b.sev] || a.rank - b.rank);
}

/* ── view ─────────────────────────────────────────────────────────────── */
const CAP = 8;

export function NeedsYou({ needs, scopeName, loading, label }: { needs: Need[]; scopeName: string; loading: boolean; label?: string }) {
  const [all, setAll] = useState(false);
  const shown = all ? needs : needs.slice(0, CAP);
  return (
    <>
      <SectHead
        standalone
        title="Needs you"
        label={loading && !needs.length ? "checking…" : label ?? `${needs.length} open${needs.length > 1 ? " · worst first" : ""}`}
        right={needs.length > CAP && <Btn sm onClick={() => setAll((v) => !v)}>{all ? "Show fewer" : `Show all ${needs.length}`}</Btn>}
      />
      {needs.length === 0 ? (
        !loading && (
          <div className={`${FRAME} flex min-h-[52px] flex-wrap items-center gap-x-[8px] gap-y-[2px] px-[16px] py-[16px] text-[14px] leading-[20px]`}>
            <OkIcon className="text-[var(--tone-green-text)]" />
            <span className="font-semibold text-[var(--tone-green-text)]">Nothing needs you on {scopeName}.</span>
            <span className="text-muted-foreground">Every container is running, no deploy failed and no alert is open.</span>
          </div>
        )
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-[12px] sm:auto-rows-fr">
          {shown.map((n) => (
            <article key={n.key} className={`${FRAME} flex min-w-0 flex-col px-[16px] pt-[14px] pb-[16px]`}>
              <div className="flex min-w-0 items-center gap-[8px]">
                <SevLabel sev={n.sev} />
                {n.host && <HostChip name={n.host.name || n.host.id} online={n.host.online} />}
                {n.when && <span className="ml-auto shrink-0 font-mono text-[12px] leading-none font-medium text-muted-foreground">{n.when}</span>}
              </div>
              <h3 className="m-0 mt-[12px] font-sans text-[15px] leading-[20px] font-semibold [overflow-wrap:anywhere]">{n.title}</h3>
              <p className="m-0 mt-[4px] text-[14px] leading-[20px] text-muted-foreground">{n.context}</p>
              <div className="mt-auto flex flex-wrap gap-[8px] pt-[16px]">
                {n.acts.slice(0, 2).map((a, idx) => (
                  <Btn key={a.label} sm solid={idx === 0} to={a.to} onClick={a.onClick} busy={a.busy}>{a.label}</Btn>
                ))}
              </div>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
