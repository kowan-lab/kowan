import { useMemo, type ReactNode } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { listAudit, listHostEvents } from "../../api/client";
import type { Alert, AuditEntry, Deployment, DockerEvent, Host } from "../../types";
import { cn } from "@/lib/utils";
import { clock } from "./model";
import { Btn, FRAME, HostChip, SectHead, SevGlyph } from "./ui";

/*
 * "Recently": the last few things that happened in scope, merged newest first:
 * alerts (raised or resolved), Docker lifecycle events from each host, and the
 * audit log for admins. The full stream lives on the Activity page.
 */

const ROWS = 6;

/** Docker events worth a line on the overview; exec_*, attach, resize… are noise. */
const KEEP = new Set(["start", "die", "restart", "kill", "oom", "stop", "create", "destroy", "pause", "unpause", "health_status", "pull", "delete"]);

type Kind = "alert" | "event" | "audit";
interface Item {
  key: string;
  ts: number;
  kind: Kind;
  tone: "red" | "amber" | "muted";
  hostId?: string;
  what: ReactNode;
  detail?: string;
  who?: string;
}

function eventLine(e: DockerEvent): { what: ReactNode; detail: string; tone: Item["tone"] } | null {
  const a = e.action.toLowerCase();
  if (!KEEP.has(a)) return null;
  const name = e.actorName || e.actorId.slice(0, 12);
  if (e.type === "image") return { what: <><b className="font-semibold">{name}</b> {a === "pull" ? "pulled" : "removed"}</>, detail: "image", tone: "muted" };
  if (e.type !== "container") return null;
  if (a === "health_status") {
    const d = (e.attributes.detail ?? e.attributes.health_status ?? "").toLowerCase();
    if (d !== "unhealthy" && d !== "healthy") return null;
    return { what: <><b className="font-semibold">{name}</b> {d}</>, detail: "health check", tone: d === "unhealthy" ? "amber" : "muted" };
  }
  if (a === "die") {
    const code = e.attributes.exitCode ?? "";
    return { what: <><b className="font-semibold">{name}</b> exited {code}</>, detail: e.image, tone: code && code !== "0" ? "red" : "muted" };
  }
  const verb: Record<string, string> = { start: "started", restart: "restarted", kill: "killed", oom: "ran out of memory", stop: "stopped", create: "created", destroy: "removed", pause: "paused", unpause: "resumed" };
  return { what: <><b className="font-semibold">{name}</b> {verb[a] ?? a}</>, detail: e.image, tone: a === "oom" || a === "kill" ? "red" : "muted" };
}

const UUID = /^[0-9a-f]{8}-[0-9a-f-]{27}$/i;
const VERB: Record<string, string> = { POST: "created", PUT: "updated", PATCH: "updated", DELETE: "deleted" };
const DID: Record<string, string> = {
  deploy: "deployed", redeploy: "redeployed", stop: "stopped", start: "started", restart: "restarted", destroy: "destroyed",
  sync: "synced", resolve: "resolved", run: "ran", update: "updated", pull: "pulled", prune: "pruned", pause: "paused", unpause: "resumed",
};

/** "demo deployed blog" from POST /api/v1/deployments/<id>/deploy. Only mutating calls; logins and dry runs are skipped. */
function auditLine(e: AuditEntry, names: Map<string, string>): { what: ReactNode; detail: string; hostId?: string; failed: boolean } | null {
  if (!VERB[e.action]) return null;
  const parts = e.path.replace(/^\/api\/v1\//, "").split("/").filter(Boolean);
  if (!parts.length || parts[0] === "auth" || /validate|render|test|preview$/.test(parts[parts.length - 1])) return null;
  const hostId = parts[0] === "hosts" ? parts[1] : undefined;
  const segs = parts[0] === "hosts" ? parts.slice(2) : parts;
  const [collection, id, sub] = segs;
  const target = id ? names.get(id) ?? (UUID.test(id) ? id.slice(0, 8) : id) : "";
  const noun = (collection ?? "").replace(/-/g, " ").replace(/s$/, "");
  const verb = sub ? DID[sub] ?? `ran ${sub.replace(/[-_]/g, " ")} on` : VERB[e.action];
  const failed = e.status >= 400;
  const what = (
    <>
      <span>{e.username} {verb} {sub ? "" : noun}</span>
      {target && <> <b className="font-semibold">{target}</b></>}
    </>
  );
  return { what, detail: failed ? `failed · HTTP ${e.status}` : e.ip, hostId, failed };
}

export function RecentFeed({
  hosts, scopeIds, all, alerts, deployments, isAdmin,
}: {
  hosts: Host[];
  scopeIds: string[];
  all: boolean;
  alerts: Alert[];
  deployments: Deployment[];
  isAdmin: boolean;
}) {
  const online = scopeIds.filter((id) => hosts.find((h) => h.id === id)?.online);
  const events = useQueries({
    queries: online.map((id) => ({
      queryKey: ["host-events", id, "overview"],
      queryFn: () => listHostEvents(id, { since: Math.floor(Date.now() / 1000) - 6 * 3600 }),
      refetchInterval: 30_000,
    })),
  });
  const audit = useQuery({
    queryKey: ["audit", "overview"],
    queryFn: () => listAudit({ limit: 40 }),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const names = useMemo(() => {
    const m = new Map<string, string>();
    for (const d of deployments) m.set(d.id, d.name);
    for (const h of hosts) m.set(h.id, h.name || h.id);
    return m;
  }, [deployments, hosts]);

  const items: Item[] = useMemo(() => {
    const out: Item[] = [];
    for (const a of alerts) {
      if (!all && a.hostId !== scopeIds[0]) continue;
      const lvl = a.level.toLowerCase();
      out.push({
        key: `a:${a.id}`,
        ts: Date.parse(a.createdAt),
        kind: "alert",
        tone: a.resolved ? "muted" : lvl === "critical" ? "red" : "amber",
        hostId: a.hostId,
        what: <span>{a.title}{a.resolved && <span className="text-muted-foreground"> · resolved</span>}</span>,
        detail: a.message,
      });
    }
    events.forEach((q, i) => {
      for (const e of q.data ?? []) {
        const line = eventLine(e);
        if (line) out.push({ key: `e:${online[i]}:${e.id}`, ts: Date.parse(e.time), kind: "event", hostId: online[i], ...line });
      }
    });
    for (const e of audit.data?.items ?? []) {
      const line = auditLine(e, names);
      if (!line) continue;
      if (!all && line.hostId && line.hostId !== scopeIds[0]) continue;
      out.push({ key: `u:${e.id}`, ts: Date.parse(e.time), kind: "audit", tone: line.failed ? "red" : "muted", hostId: line.hostId, what: line.what, detail: line.detail, who: e.username });
    }
    return out.sort((a, b) => b.ts - a.ts).slice(0, ROWS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alerts, all, scopeIds.join(), events.map((q) => q.dataUpdatedAt).join(), audit.dataUpdatedAt, names]);

  const loading = events.some((q) => q.isLoading) || (isAdmin && audit.isLoading);
  const hostOf = (id?: string) => hosts.find((h) => h.id === id);

  return (
    <section className={FRAME}>
      <SectHead
        title="Recently"
        label={isAdmin ? "alerts, events, audit" : "alerts, events"}
        right={<Btn sm to="/activity">Open timeline</Btn>}
      />
      {items.length === 0 ? (
        <p className="m-0 px-[16px] py-[14px] text-[14px] leading-[20px] text-muted-foreground">
          {loading ? "Loading recent activity…" : "Nothing happened here in the last few hours."}
        </p>
      ) : (
        <ol className="m-0 list-none p-0">
          {items.map((it) => {
            const h = hostOf(it.hostId);
            return (
              <li
                key={it.key}
                className="grid min-h-[48px] grid-cols-[72px_84px_minmax(0,1fr)_auto] items-center gap-[12px] px-[16px] py-[10px] text-[14px] leading-[20px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none max-sm:grid-cols-[56px_minmax(0,1fr)] max-sm:[&>.typ]:hidden max-sm:[&>.who]:hidden"
              >
                <time className="font-mono text-[12px] leading-[16px] font-medium text-muted-foreground" dateTime={new Date(it.ts).toISOString()} title={new Date(it.ts).toLocaleString()}>
                  {clock(it.ts)}
                </time>
                <span
                  className={cn(
                    "typ inline-flex items-center gap-[6px] font-mono text-[12px] leading-none font-bold tracking-[0.06em] uppercase",
                    it.kind === "audit" ? "text-muted-foreground" : it.tone === "red" ? "text-[var(--tone-red-text)]" : it.tone === "amber" ? "text-[var(--tone-amber-text)]" : "text-muted-foreground",
                  )}
                >
                  {it.kind === "alert" && it.tone !== "muted" && <SevGlyph sev={it.tone === "red" ? "critical" : "warning"} />}
                  {it.kind}
                </span>
                <span className="flex min-w-0 flex-wrap items-center gap-x-[8px] gap-y-[2px]">
                  {all && h && <HostChip name={h.name || h.id} online={h.online} />}
                  <span className={cn("min-w-0 [overflow-wrap:anywhere]", it.kind !== "alert" && it.tone === "red" && "text-[var(--tone-red-text)]")}>{it.what}</span>
                  {it.detail && <small className="min-w-0 truncate text-[13px] text-muted-foreground">{it.detail}</small>}
                </span>
                <span className="who flex items-center justify-end gap-[8px] font-mono text-[12px] leading-[16px] font-medium text-muted-foreground">
                  {it.who ?? (it.kind === "alert" ? <Link to="/monitoring" className="text-muted-foreground no-underline hover:text-foreground hover:underline">alerts</Link> : null)}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
