// Shared pieces of the deployment detail page: the live queries every section
// reads (deduplicated by react-query through their keys), the lifecycle action
// mutation, and small formatters.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  deploymentServices,
  deploymentStatus,
  deployDeployment,
  destroyDeployment,
  startDeployment,
  stopDeployment,
} from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { LIVE_REFETCH_MS } from "../../lib/live";
import { notify } from "../../ui/toast";
import type { Deployment } from "../../types";
import { errorMessage } from "../../utils";

/** The single host a deployment runs on (standalone host or swarm manager). */
export function deploymentHost(d: Deployment): string | null {
  return d.targets?.[0] ?? null;
}

/** Per-target runtime state; carries the last deploy error when nothing runs. */
export function useDeploymentStatus(d: Deployment) {
  return useQuery({
    queryKey: ["deployment-status", d.id],
    queryFn: () => deploymentStatus(d.id),
    refetchInterval: LIVE_REFETCH_MS,
  });
}

/** The containers of the deployment on its host, one entry per service. */
export function useDeploymentServices(d: Deployment) {
  const host = deploymentHost(d);
  return useQuery({
    queryKey: ["deployment-services", d.id, host],
    queryFn: () => deploymentServices(d.id, host!),
    enabled: !!host,
    refetchInterval: LIVE_REFETCH_MS,
  });
}

/** Operators and admins may change a deployment; viewers only look. */
export function useCanOperate(): boolean {
  const { user } = useAuth();
  return user?.role === "admin" || user?.role === "operator";
}

/** A deploy is queued or running: the server rejects a second one (409). */
export function deployInFlight(d: Deployment): boolean {
  return d.status === "pending" || d.status === "deploying";
}

/** Never deployed: no status recorded yet. */
export function neverDeployed(d: Deployment): boolean {
  return !d.status;
}

export type LifecycleAction = "deploy" | "start" | "stop" | "destroy";

const ACTION_FN: Record<LifecycleAction, (id: string) => Promise<void>> = {
  deploy: deployDeployment,
  start: startDeployment,
  stop: stopDeployment,
  destroy: destroyDeployment,
};

const ACTION_DONE: Record<LifecycleAction, string> = {
  deploy: "Deploy queued",
  start: "Started",
  stop: "Stopped",
  destroy: "Resources removed",
};

/** One mutation for the lifecycle actions, shared by the header and callouts. */
export function useLifecycle(d: Deployment) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (action: LifecycleAction) => ACTION_FN[action](d.id),
    onSuccess: (_r, action) => {
      notify({
        color: action === "deploy" ? "blue" : "green",
        title: ACTION_DONE[action],
        message:
          action === "deploy"
            ? `${d.name} is deploying in the background; the status updates here.`
            : action === "destroy"
              ? `${d.name}'s containers were removed. The configuration is kept.`
              : `${d.name} ${action === "start" ? "is starting" : "was stopped"}.`,
      });
    },
    onError: (err, action) =>
      notify({
        color: "red",
        title: `${action[0].toUpperCase()}${action.slice(1)} failed`,
        message: errorMessage(err, `Could not ${action} ${d.name}`),
      }),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["deployment", d.id] });
      qc.invalidateQueries({ queryKey: ["deployments"] });
      qc.invalidateQueries({ queryKey: ["deployment-status", d.id] });
      qc.invalidateQueries({ queryKey: ["deployment-services", d.id] });
    },
  });
  return {
    run: (a: LifecycleAction) => m.mutate(a),
    runAsync: (a: LifecycleAction) => m.mutateAsync(a),
    acting: (a: LifecycleAction) => m.isPending && m.variables === a,
    busy: m.isPending,
  };
}
