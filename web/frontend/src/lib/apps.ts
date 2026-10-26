/**
 * Apps: the unit an operator thinks in. Derived entirely on the client from the
 * existing APIs (container lists per host, the deployments list, the update
 * report); there is no /apps endpoint.
 *
 *  - "deployment": a Kowan deployment. Its containers carry the
 *    `kowan.deployment.id` label (container type) or the compose project label
 *    equal to the deployment's resource name (compose type, as the server
 *    matches them in kowan/deployment/service.py).
 *  - "stack": a compose project (`com.docker.compose.project`) or swarm stack
 *    (`com.docker.stack.namespace`) that no deployment owns.
 *  - "container": a lone container is an app of one.
 *
 * App ids are a URL contract (other pages link to them):
 *   dep~<deploymentId> · stack~<hostId>~<project> · ctr~<hostId>~<containerName>
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { listContainers, listDeployments, listHosts } from "../api/client";
import { isAllHosts } from "../HostContext";
import { useHostScopedList } from "../useHostScopedList";
import type { Container, ContainerUpdateStatus, Deployment, Host } from "../types";
import { LIVE_REFETCH_MS } from "./live";
import { containerOrigin, serviceOf, stackOf } from "./containerOrigin";
import { isKowanContainer, useFeatures } from "./features";
import { updateKey, useContainerUpdates } from "./updates";

export type AppKind = "deployment" | "stack" | "container";
/** g running · a degraded/unhealthy/transitional · r failed · n stopped/unknown */
export type AppTone = "g" | "a" | "r" | "n";

export interface AppContainer {
  container: Container;
  hostId: string;
  /** Docker name without the leading slash. */
  name: string;
  /** Compose service, or the name for a lone container. */
  service: string;
  tone: AppTone;
  /** Pill text: "Running · 13m", "Exited 1 · 14m", "Unhealthy · 2h". */
  label: string;
  health: "healthy" | "unhealthy" | "starting" | "";
  exitCode: number | null;
  update?: ContainerUpdateStatus;
}

export interface AppPort {
  publicPort: number;
  privatePort: number;
  protocol: string;
  /** Bound on every interface (0.0.0.0 / ::), as opposed to a loopback bind. */
  public: boolean;
  container: string;
}

export interface AppAttention {
  tone: "r" | "a" | "n";
  text: string;
  /** Container the problem is about, when there is one. */
  container?: string;
}

export interface App {
  id: string;
  kind: AppKind;
  name: string;
  /** Hosts the app runs on (one today; an array so the shape survives swarm). */
  hostIds: string[];
  containers: AppContainer[];
  running: number;
  total: number;
  tone: AppTone;
  /** App pill text: "Running · 13m", "Degraded · 5/6", "Stopped". */
  label: string;
  health: { healthy: number; unhealthy: number; starting: number };
  ports: AppPort[];
  /** Managed by Kowan (a deployment) or discovered from Docker labels. */
  managed: boolean;
  deployment?: Deployment;
  /** Compose project name (stack apps and compose deployments). */
  project?: string;
  /** Containers with a newer image in the registry. */
  updates: number;
  attention: AppAttention | null;
  /** The host is offline: containers are unknown, the app is shown from config. */
  stale: boolean;
  /** Unix ms of the oldest container, or the deployment's creation time. */
  createdAt: number | null;
}

// ---------------------------------------------------------------------------
// ids and links
// ---------------------------------------------------------------------------

const enc = (s: string) => encodeURIComponent(s);

export type AppIdParts =
  | { kind: "deployment"; deploymentId: string }
  | { kind: "stack"; hostId: string; project: string }
  | { kind: "container"; hostId: string; name: string };

export function appId(p: AppIdParts): string {
  switch (p.kind) {
    case "deployment":
      return `dep~${p.deploymentId}`;
    case "stack":
      return `stack~${p.hostId}~${p.project}`;
    default:
      return `ctr~${p.hostId}~${p.name}`;
  }
}

/** Parse an app id back into its parts; null for anything malformed. */
export function parseAppId(id: string): AppIdParts | null {
  const [kind, ...rest] = id.split("~");
  if (kind === "dep" && rest.length === 1 && rest[0]) return { kind: "deployment", deploymentId: rest[0] };
  if (kind === "stack" && rest.length === 2 && rest[0] && rest[1]) return { kind: "stack", hostId: rest[0], project: rest[1] };
  if (kind === "ctr" && rest.length === 2 && rest[0] && rest[1]) return { kind: "container", hostId: rest[0], name: rest[1] };
  return null;
}

/** `/workloads/<id>`, from an app, its parts or a ready id; `tab` adds `?tab=`. */
export function appHref(app: App | AppIdParts | string, tab?: string): string {
  const id = typeof app === "string" ? app : "id" in app ? app.id : appId(app);
  const q = tab && tab !== "overview" ? `?tab=${enc(tab)}` : "";
  return `/workloads/${enc(id)}${q}`;
}

/**
 * Docker-safe resource name of a deployment, mirroring
 * kowan/models/deployment.py::sanitize_resource_name. A compose deployment's
 * project label equals this value.
 */
export function resourceName(d: Pick<Deployment, "id" | "name">): string {
  const clean = (s: string) => {
    let out = "";
    let dash = false;
    for (const r of s.trim().toLowerCase()) {
      if ((r >= "a" && r <= "z") || (r >= "0" && r <= "9") || r === "_") {
        out += r;
        dash = false;
      } else if (!dash && out.length > 0) {
        out += "-";
        dash = true;
      }
    }
    return out.replace(/^[-_]+|[-_]+$/g, "");
  };
  const n = clean(d.name);
  if (n) return n;
  const r = clean(d.id);
  return r ? `app-${r}` : "app";
}

function ownerDeployment(
  c: Container,
  hostId: string,
  deployments: Deployment[] | undefined,
): Deployment | undefined {
  const labels = c.labels ?? {};
  const origin = containerOrigin(labels);
  if (origin.kind === "deployment") return deployments?.find((d) => d.id === origin.deploymentId);
  const project = stackOf(labels);
  if (!project || !deployments) return undefined;
  return deployments.find(
    (d) => d.type === "compose" && (d.targets?.[0] ?? "") === hostId && resourceName(d) === project,
  );
}

/**
 * The app a container belongs to. Pass the deployments list when you have it:
 * compose deployments label their containers only with the project name, so
 * without it they resolve to their stack id (the app page then redirects).
 */
export function appIdForContainer(c: Container, hostId: string, deployments?: Deployment[]): string {
  const labels = c.labels ?? {};
  const origin = containerOrigin(labels);
  if (origin.kind === "deployment" && origin.deploymentId) return appId({ kind: "deployment", deploymentId: origin.deploymentId });
  const owner = ownerDeployment(c, hostId, deployments);
  if (owner) return appId({ kind: "deployment", deploymentId: owner.id });
  const project = stackOf(labels);
  if (project) return appId({ kind: "stack", hostId, project });
  return appId({ kind: "container", hostId, name: nameOf(c) });
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

export function nameOf(c: Container): string {
  return (c.names?.[0] ?? c.id).replace(/^\//, "");
}

/** "Up 13 minutes (healthy)" → "13m"; "Exited (1) 2 hours ago" → "2h". */
export function statusAge(status: string): string {
  const s = status.toLowerCase();
  if (/less than a second|seconds?\b/.test(s) && !/\d+\s*(minute|hour|day|week|month|year)/.test(s)) {
    const n = /(\d+)\s*second/.exec(s);
    return n ? `${n[1]}s` : "now";
  }
  const m = /(\d+|an?|about an?)\s*(minute|hour|day|week|month|year)/.exec(s);
  if (!m) return "";
  const n = /\d+/.test(m[1]) ? Number(/\d+/.exec(m[1])![0]) : 1;
  const unit = { minute: "m", hour: "h", day: "d", week: "w", month: "mo", year: "y" }[m[2] as "minute"];
  return `${n}${unit}`;
}

function healthOf(status: string): AppContainer["health"] {
  const s = status.toLowerCase();
  if (s.includes("(unhealthy)")) return "unhealthy";
  if (s.includes("(healthy)")) return "healthy";
  if (s.includes("health: starting")) return "starting";
  return "";
}

function exitCodeOf(status: string): number | null {
  const m = /exited \((-?\d+)\)/i.exec(status);
  return m ? Number(m[1]) : null;
}

function describeContainer(c: Container): Pick<AppContainer, "tone" | "label" | "health" | "exitCode"> {
  const state = (c.state || "").toLowerCase();
  const health = healthOf(c.status || "");
  const exitCode = exitCodeOf(c.status || "");
  const age = statusAge(c.status || "");
  const dur = age ? ` · ${age}` : "";
  if (state === "running") {
    if (health === "unhealthy") return { tone: "a", label: `Unhealthy${dur}`, health, exitCode };
    if (health === "starting") return { tone: "a", label: `Starting${dur}`, health, exitCode };
    return { tone: "g", label: `Running${dur}`, health, exitCode };
  }
  if (state === "restarting") return { tone: "a", label: "Restarting", health, exitCode };
  if (state === "paused") return { tone: "a", label: "Paused", health, exitCode };
  if (state === "dead") return { tone: "r", label: "Dead", health, exitCode };
  if (state === "exited") {
    if (exitCode && exitCode !== 0) return { tone: "r", label: `Exited ${exitCode}${dur}`, health, exitCode };
    return { tone: "n", label: `Stopped${dur}`, health, exitCode };
  }
  if (state === "created") return { tone: "n", label: "Created", health, exitCode };
  return { tone: "n", label: state ? state[0].toUpperCase() + state.slice(1) : "Unknown", health, exitCode };
}

const TONE_RANK: Record<AppTone, number> = { r: 3, a: 2, n: 1, g: 0 };

function portsOf(cs: AppContainer[]): AppPort[] {
  const seen = new Map<string, AppPort>();
  for (const ac of cs) {
    for (const p of ac.container.ports ?? []) {
      if (!p.publicPort) continue;
      const proto = p.type || "tcp";
      const key = `${p.publicPort}/${proto}`;
      const isPublic = !p.ip || p.ip === "0.0.0.0" || p.ip === "::";
      const prev = seen.get(key);
      if (prev) {
        prev.public = prev.public || isPublic;
        continue;
      }
      seen.set(key, { publicPort: p.publicPort, privatePort: p.privatePort, protocol: proto, public: isPublic, container: ac.name });
    }
  }
  return [...seen.values()].sort((a, b) => a.publicPort - b.publicPort);
}

function summarize(base: Omit<App, "running" | "total" | "tone" | "label" | "health" | "ports" | "updates" | "attention" | "createdAt">, createdFallback: number | null): App {
  const cs = base.containers;
  const total = cs.length;
  const running = cs.filter((c) => c.container.state === "running").length;
  const health = {
    healthy: cs.filter((c) => c.health === "healthy").length,
    unhealthy: cs.filter((c) => c.health === "unhealthy").length,
    starting: cs.filter((c) => c.health === "starting").length,
  };
  const updates = cs.filter((c) => c.update?.updateAvailable).length;
  const worst = cs.reduce<AppTone>((w, c) => (TONE_RANK[c.tone] > TONE_RANK[w] ? c.tone : w), "g");

  let tone: AppTone;
  let label: string;
  let attention: AppAttention | null = null;
  const dep = base.deployment;

  if (base.stale) {
    tone = "n";
    label = "Unknown";
    attention = { tone: "n", text: "Host offline · last known config" };
  } else if (total === 0) {
    tone = dep && (dep.status === "failed" || dep.status === "error") ? "r" : dep && (dep.status === "pending" || dep.status === "deploying") ? "a" : "n";
    label = dep ? (dep.status ? (tone === "a" ? "Deploying" : tone === "r" ? "Deploy failed" : "No containers") : "Not deployed") : "Unknown";
    if (tone === "r") attention = { tone: "r", text: "The last deploy failed" };
  } else if (worst === "r") {
    tone = "r";
    label = `Degraded · ${running}/${total}`;
    const bad = cs.find((c) => c.tone === "r")!;
    attention = {
      tone: "r",
      text: bad.exitCode != null ? `${bad.service} exited with code ${bad.exitCode}` : `${bad.service} is ${bad.container.state}`,
      container: bad.name,
    };
  } else if (worst === "a") {
    tone = "a";
    const w = cs.find((c) => c.tone === "a")!;
    label = health.unhealthy ? `Unhealthy · ${total - health.unhealthy}/${total}` : `${w.label.split(" · ")[0]} · ${running}/${total}`;
    attention = { tone: "a", text: w.health === "unhealthy" ? `${w.service} is unhealthy` : `${w.service} is ${w.label.split(" · ")[0].toLowerCase()}`, container: w.name };
  } else if (running === 0) {
    tone = "n";
    label = "Stopped";
  } else if (running < total) {
    // Some containers stopped cleanly (exit 0): one-shot jobs, or stopped by hand.
    tone = "g";
    label = `Running · ${running}/${total}`;
  } else {
    tone = "g";
    const age = statusAge(cs[0].container.status || "");
    label = age ? `Running · ${age}` : "Running";
  }
  if (!attention && dep && (dep.status === "failed" || dep.status === "error")) {
    attention = { tone: "r", text: "The last deploy failed" };
    if (tone === "g") tone = "a";
  }

  const createdAt = cs.length
    ? Math.min(...cs.map((c) => (c.container.created || 0) * 1000).filter((n) => n > 0))
    : createdFallback;

  return {
    ...base,
    running,
    total,
    tone,
    label,
    health,
    ports: portsOf(cs),
    updates,
    attention,
    createdAt: Number.isFinite(createdAt as number) ? createdAt : createdFallback,
  };
}

export type ScopedContainer = Container & { _hostId?: string };

/**
 * Group containers into apps. `fallbackHost` is the host of an unaggregated
 * list; `hostsOnline` marks deployments on offline hosts as stale.
 */
export function buildApps(opts: {
  containers: ScopedContainer[];
  fallbackHost: string;
  deployments: Deployment[];
  /** Hosts in scope; deployments targeting other hosts are left out. */
  scopeHosts: Host[] | null;
  updates?: (hostId: string, containerId: string) => ContainerUpdateStatus | undefined;
}): App[] {
  const { containers, fallbackHost, deployments, scopeHosts, updates } = opts;
  const depApps = new Map<string, AppContainer[]>();
  const stacks = new Map<string, { hostId: string; project: string; cs: AppContainer[] }>();
  const singles: AppContainer[] = [];

  for (const c of containers) {
    const hostId = c._hostId || c.hostId || fallbackHost;
    const labels = c.labels ?? {};
    const name = nameOf(c);
    const ac: AppContainer = {
      container: c,
      hostId,
      name,
      service: serviceOf(labels) || name,
      ...describeContainer(c),
      update: updates?.(hostId, c.id),
    };
    const owner = ownerDeployment(c, hostId, deployments);
    if (owner) {
      depApps.set(owner.id, [...(depApps.get(owner.id) ?? []), ac]);
      continue;
    }
    const project = stackOf(labels);
    if (project) {
      const key = `${hostId}~${project}`;
      const s = stacks.get(key) ?? { hostId, project, cs: [] };
      s.cs.push(ac);
      stacks.set(key, s);
      continue;
    }
    singles.push(ac);
  }

  const apps: App[] = [];
  const inScope = (h: string) => !scopeHosts || scopeHosts.some((x) => x.id === h);
  for (const d of deployments) {
    const host = d.targets?.[0] ?? "";
    const cs = depApps.get(d.id) ?? [];
    if (!cs.length && !inScope(host)) continue;
    const hostInfo = scopeHosts?.find((h) => h.id === host);
    const sortCs = [...cs].sort((a, b) => a.service.localeCompare(b.service));
    apps.push(
      summarize(
        {
          id: appId({ kind: "deployment", deploymentId: d.id }),
          kind: "deployment",
          name: d.name,
          hostIds: host ? [host] : [],
          containers: sortCs,
          managed: true,
          deployment: d,
          project: d.type === "compose" ? resourceName(d) : undefined,
          stale: !!hostInfo && !hostInfo.online,
        },
        d.createdAt ? Date.parse(d.createdAt) : null,
      ),
    );
  }
  for (const s of stacks.values()) {
    apps.push(
      summarize(
        {
          id: appId({ kind: "stack", hostId: s.hostId, project: s.project }),
          kind: "stack",
          name: s.project,
          hostIds: [s.hostId],
          containers: [...s.cs].sort((a, b) => a.service.localeCompare(b.service)),
          managed: false,
          project: s.project,
          stale: false,
        },
        null,
      ),
    );
  }
  for (const ac of singles) {
    apps.push(
      summarize(
        {
          id: appId({ kind: "container", hostId: ac.hostId, name: ac.name }),
          kind: "container",
          name: ac.name,
          hostIds: [ac.hostId],
          containers: [ac],
          managed: false,
          stale: false,
        },
        null,
      ),
    );
  }
  return apps.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

/** One service of a stack (or compose deployment) and its containers: replicas
 *  for swarm, usually one for compose. */
export interface AppService {
  name: string;
  containers: AppContainer[];
  tone: AppTone;
  label: string;
}

/**
 * A stack's containers grouped by service, in service order; null for a lone
 * container or a container deployment, which have no services.
 */
export function servicesOf(app: App): AppService[] | null {
  if (app.kind === "container" || app.deployment?.type === "container") return null;
  const groups = new Map<string, AppContainer[]>();
  for (const c of app.containers) groups.set(c.service, [...(groups.get(c.service) ?? []), c]);
  return [...groups].map(([name, cs]) => {
    if (cs.length === 1) return { name, containers: cs, tone: cs[0].tone, label: cs[0].label };
    const running = cs.filter((c) => c.container.state === "running").length;
    const worst = cs.reduce<AppTone>((w, c) => (TONE_RANK[c.tone] > TONE_RANK[w] ? c.tone : w), "g");
    const of = `${running}/${cs.length}`;
    const label =
      worst === "r" ? `Degraded · ${of}`
        : worst === "a" ? `${cs.find((c) => c.tone === "a")!.label.split(" · ")[0]} · ${of}`
          : running === 0 ? "Stopped"
            : `Running · ${of}`;
    return { name, containers: cs, tone: running === 0 && worst === "g" ? "n" : worst, label };
  });
}

// ---------------------------------------------------------------------------
// hooks
// ---------------------------------------------------------------------------

export const KIND_LABEL: Record<AppKind, string> = {
  deployment: "Deployment",
  stack: "Compose stack",
  container: "Single container",
};

/** Short kind for card meta lines: "Compose · 6", "Container". */
export function kindShort(a: App): string {
  if (a.kind === "container" || a.deployment?.type === "container") return "Container";
  return `Compose · ${a.total}`;
}

/** Apps in a host scope (a host id, or ALL_HOSTS to fan out over online hosts). */
export function useApps(hostScope: string) {
  const aggregated = isAllHosts(hostScope);
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, refetchInterval: 15_000 });
  const containersQuery = useHostScopedList<Container>(
    "containers:all",
    hostScope,
    (h) => listContainers(h, true),
    { refetchInterval: LIVE_REFETCH_MS },
  );
  const deploymentsQuery = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, refetchInterval: 5000 });
  const updates = useContainerUpdates(hostScope);

  const hosts = hostsQuery.data;
  const showSystem = useFeatures().systemContainers;
  const apps = useMemo(() => {
    if (!containersQuery.data && !containersQuery.isError) return undefined;
    const scopeHosts = hosts ? (aggregated ? hosts : hosts.filter((h) => h.id === hostScope)) : null;
    const containers = containersQuery.data ?? [];
    return buildApps({
      containers: showSystem ? containers : containers.filter((c) => !isKowanContainer(c.labels)),
      fallbackHost: hostScope,
      deployments: deploymentsQuery.data ?? [],
      scopeHosts,
      updates: updates.data ? (h, id) => updates.data!.get(updateKey(h, id)) : undefined,
    });
  }, [containersQuery.data, containersQuery.isError, deploymentsQuery.data, updates.data, hosts, aggregated, hostScope, showSystem]);

  return {
    apps,
    hosts: hosts ?? [],
    isLoading: containersQuery.isLoading || deploymentsQuery.isLoading,
    error: containersQuery.error ?? deploymentsQuery.error ?? null,
    refetch: () => {
      void containersQuery.refetch();
      void deploymentsQuery.refetch();
    },
    updatesChecking: updates.checking,
  };
}
