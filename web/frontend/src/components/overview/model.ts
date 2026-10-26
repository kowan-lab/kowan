import type { Container, Deployment, Host } from "../../types";
import { appHref, appIdForContainer, superseded } from "../../lib/apps";
import { containerOrigin } from "../../lib/containerOrigin";
import { containerName } from "../../utils";
import type { Tone } from "./ui";

/** A container as the overview sees it: tagged with the host it runs on. */
export type HostContainer = Container & { _host: string };

/* ── time ─────────────────────────────────────────────────────────────── */
/** Compact age for mono columns: "now", "42s", "4m", "1h", "3d". */
export function age(input: string | number | Date | undefined | null): string {
  if (input == null || input === "") return "—";
  const d = typeof input === "number" ? new Date(input < 1e12 ? input * 1000 : input) : new Date(input);
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (Number.isNaN(s)) return "—";
  if (s < 10) return "now";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** "Exited (1) About an hour ago" / "Up 3 minutes (unhealthy)" → "1h" / "3m". */
export function statusAge(status: string): string {
  const m = /(\d+|an?|About an?|Less than a)\s+(second|minute|hour|day|week|month|year)/i.exec(status);
  if (!m) return "";
  const n = /^\d+$/.test(m[1]) ? Number(m[1]) : 1;
  const unit = { second: "s", minute: "m", hour: "h", day: "d", week: "w", month: "mo", year: "y" }[m[2].toLowerCase()] ?? "";
  return /less than/i.test(m[1]) ? `<1${unit}` : `${n}${unit}`;
}

export function clock(ts: string | number): string {
  const d = typeof ts === "number" ? new Date(ts) : new Date(ts);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/* ── containers ───────────────────────────────────────────────────────── */
export function exitCode(status: string): number | null {
  const m = /Exited \((-?\d+)\)/.exec(status);
  return m ? Number(m[1]) : null;
}

export type Trouble = "exited" | "dead" | "unhealthy" | "restarting";

/** What is wrong with a container, if anything. ``all`` is the list it came
 *  from, so an old Swarm task whose slot has moved on does not count. */
export function trouble(c: Container, all: Container[] = []): Trouble | null {
  const s = c.state.toLowerCase();
  if (s === "dead") return "dead";
  if (s === "restarting") return "restarting";
  if (/\(unhealthy\)/i.test(c.status)) return "unhealthy";
  if (s === "exited") {
    const code = exitCode(c.status);
    return code !== null && code !== 0 && !superseded(c, all) ? "exited" : null;
  }
  return null;
}

/** Tick colour for the hosts table's container meter. */
export function containerTone(c: Container, all: Container[] = []): Tone {
  const t = trouble(c, all);
  if (t === "exited" || t === "dead") return "red";
  if (t === "unhealthy" || t === "restarting") return "amber";
  return c.state.toLowerCase() === "running" ? "green" : "gray";
}

/** The app a container belongs to, by the Apps id contract (lib/apps). Compose
 *  deployments resolve to their deployment, not to the stack. */
export function appIdFor(c: HostContainer, deployments?: Deployment[]): string {
  return appIdForContainer(c, c._host, deployments);
}

export { appHref };

/** "shop: migrate" for a stack service, "blog" for a deployment, the name otherwise. */
export function containerLabel(c: Container): { app: string | null; part: string } {
  const o = containerOrigin(c.labels);
  const name = containerName(c.names);
  if (o.kind === "stack") return { app: o.name, part: o.service || name };
  if (o.kind === "deployment") return { app: o.name, part: name === o.name ? "" : name };
  return { app: null, part: name };
}

export const containerHref = (c: HostContainer, tab?: string) =>
  `/containers/${c.id}?host=${encodeURIComponent(c._host)}${tab ? `&tab=${tab}` : ""}`;

/* ── hosts ────────────────────────────────────────────────────────────── */
/** How the server reaches a host, in the operator's words. */
export function hostKind(h: Host): { kind: string; tunnel: string } {
  if (h.type === "local") return { kind: "Local socket", tunnel: "local socket" };
  if (h.mode === "edge") return { kind: "Edge agent", tunnel: "atos · agent dials server" };
  return { kind: "Direct agent", tunnel: "stoa · server dials agent" };
}
