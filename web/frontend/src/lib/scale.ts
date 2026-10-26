import { parse as parseYaml } from "yaml";
import { applyComposePatch } from "../components/compose/applyPatch";
import { deploymentFormFromDeployment, deploymentFormToInput } from "../components/DeploymentForm";
import type { DeploymentInput } from "../api/client";
import type { Deployment } from "../types";

/** Most replicas the scale control offers. */
export const MAX_REPLICAS = 50;

export interface ScaleRule {
  /** Replicas the deployment asks for now. */
  replicas: number;
  /** Most it can run; 1 when a fixed name or host port allows one container. */
  max: number;
  /** Why it can't scale here at all ("" when it can), or why max is 1. */
  locked: string;
  limit: string;
}

type Svc = { container_name?: unknown; ports?: unknown; scale?: unknown; deploy?: { replicas?: unknown } };

/** A port entry that binds a fixed host port ("8080:80", "127.0.0.1:8080:80",
 *  {published: 8080}): two containers of one service would collide on it. */
function fixedHostPort(p: unknown): boolean {
  if (p && typeof p === "object") return (p as { published?: unknown }).published != null && (p as { published?: unknown }).published !== "";
  const s = String(p ?? "").split("/")[0];
  const parts = s.split(":");
  if (parts.length < 2) return false;
  return parts[parts.length - 2] !== "";
}

/**
 * How a service of a Kowan deployment can be scaled from the workload page, or
 * null when scaling means nothing (a lone standalone container, an unknown
 * service). Compose replicas live in the file (deploy.replicas, else scale,
 * else 1); a swarm container deployment keeps its own count.
 */
export function scaleRule(d: Deployment, service: string): ScaleRule | null {
  if (d.type === "container") {
    return d.strategy === "swarm" ? { replicas: d.replicas || 1, max: MAX_REPLICAS, locked: "", limit: "" } : null;
  }
  let svc: Svc | undefined;
  try {
    const doc = parseYaml(d.compose || "") as { services?: Record<string, Svc> } | null;
    svc = doc?.services?.[service];
  } catch {
    return null;
  }
  if (!svc || typeof svc !== "object") return null;
  const replicas = typeof svc.deploy?.replicas === "number" ? svc.deploy.replicas : typeof svc.scale === "number" ? svc.scale : 1;
  if (d.git?.repoUrl) return { replicas, max: replicas, locked: "The compose file comes from git: change the replicas in the repository.", limit: "" };
  if (d.strategy !== "swarm") {
    if (svc.container_name) return { replicas, max: 1, locked: "", limit: "container_name gives it one fixed name, so it runs one container." };
    if (Array.isArray(svc.ports) && svc.ports.some(fixedHostPort)) return { replicas, max: 1, locked: "", limit: "A fixed host port fits one container." };
  }
  return { replicas, max: MAX_REPLICAS, locked: "", limit: "" };
}

/** The update that sets ``service`` to ``n`` replicas: deploy.replicas in the
 *  compose file (comments kept; a ``scale`` key is dropped so the two can't
 *  disagree), or the swarm container deployment's count. Null when the
 *  compose text no longer parses. */
export function scaledInput(d: Deployment, service: string, n: number): DeploymentInput | null {
  const input = deploymentFormToInput(deploymentFormFromDeployment(d)) as DeploymentInput;
  if (d.type === "container") return { ...input, replicas: n };
  const compose = applyComposePatch(input.compose, [
    { op: "set", path: ["services", service, "deploy", "replicas"], value: n },
    { op: "remove", path: ["services", service, "scale"] },
  ]);
  return compose == null ? null : { ...input, compose };
}
