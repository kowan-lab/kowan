/**
 * Where a container came from, derived entirely from its Docker labels (the
 * backend stamps these; compose stamps its own). No server round-trip needed.
 *
 *  - "deployment" — created by a Kowan deployment (label `kowan.deployment.id`).
 *  - "stack"      — part of a docker-compose project (`com.docker.compose.project`),
 *    a swarm stack (`com.docker.stack.namespace`) or a swarm service outside any
 *    stack (`com.docker.swarm.service.name`), even if created outside Kowan.
 *  - "plain"      — a standalone container, ours to edit/recreate freely.
 */
type ContainerOriginKind = "deployment" | "stack" | "plain";

export interface ContainerOrigin {
  kind: ContainerOriginKind;
  /** Deployment or stack name; empty for plain containers. */
  name: string;
  /** For deployment origin, the Kowan deployment id (used to link to it). */
  deploymentId?: string;
  /** For stack origin, the compose or swarm service name, when present. */
  service?: string;
}

const LABEL_DEPLOYMENT_ID = "kowan.deployment.id";
const LABEL_DEPLOYMENT_NAME = "kowan.deployment.name";
const LABEL_COMPOSE_PROJECT = "com.docker.compose.project";
const LABEL_COMPOSE_SERVICE = "com.docker.compose.service";
const LABEL_SWARM_STACK = "com.docker.stack.namespace";
const LABEL_SWARM_SERVICE = "com.docker.swarm.service.name";

/** The compose project or swarm stack of a container, or "" (same rule as the
 *  server). A swarm service outside any stack is its own stack, so its tasks,
 *  old ones included, group into one workload. */
export function stackOf(labels: Record<string, string> | undefined): string {
  const l = labels ?? {};
  return l[LABEL_COMPOSE_PROJECT] || l[LABEL_SWARM_STACK] || l[LABEL_SWARM_SERVICE] || "";
}

/**
 * The service a container runs inside its stack, or "": the compose service,
 * or the swarm service without the "<stack>_" prefix swarm gives it.
 */
export function serviceOf(labels: Record<string, string> | undefined): string {
  const l = labels ?? {};
  if (l[LABEL_COMPOSE_SERVICE]) return l[LABEL_COMPOSE_SERVICE];
  const svc = l[LABEL_SWARM_SERVICE] ?? "";
  const ns = l[LABEL_SWARM_STACK];
  return ns && svc.startsWith(`${ns}_`) ? svc.slice(ns.length + 1) : svc;
}

export function containerOrigin(
  labels: Record<string, string> | undefined,
): ContainerOrigin {
  const l = labels ?? {};
  const deploymentId = l[LABEL_DEPLOYMENT_ID];
  if (deploymentId) {
    return {
      kind: "deployment",
      name: l[LABEL_DEPLOYMENT_NAME] || deploymentId,
      deploymentId,
    };
  }
  const project = stackOf(l);
  if (project) {
    return { kind: "stack", name: project, service: serviceOf(l) || undefined };
  }
  return { kind: "plain", name: "" };
}

/**
 * Managed containers (from a deployment or a compose stack) must not be edited
 * or recreated ad-hoc — their spec lives in the deployment / compose file, and
 * a manual recreate would drift or be reverted on the next redeploy.
 */
export function isManaged(origin: ContainerOrigin): boolean {
  return origin.kind !== "plain";
}
