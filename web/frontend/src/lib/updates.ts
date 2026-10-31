import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { getHostUpdates, listHosts, updateContainer } from "../api/client";
import { isAllHosts } from "../HostContext";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";
import type { ContainerUpdateResult, ContainerUpdateStatus } from "../types";

// Image update detection: one query per host scope ("All hosts" fans out to every
// online host, like useHostScopedList). Registry lookups are slow and cached
// server-side, so this does NOT live-poll; "Check for updates" refetches with
// refresh=1 to bypass the server cache.

export const UPDATES_KEY = "container-updates";

/** Stable lookup key for a container's update row. */
export function updateKey(hostId: string, containerId: string): string {
  return `${hostId}/${containerId}`;
}

/** Tooltip text for an update row: digests, or why it couldn't be checked. */
export function describeUpdate(u: ContainerUpdateStatus): string {
  const short = (d: string) => (d ? d.replace(/^sha256:/, "").slice(0, 12) : "—");
  if (u.updateAvailable) {
    return `Newer image for ${u.image}: running ${short(u.localDigest)}, registry ${short(u.remoteDigest)}`;
  }
  if (u.error) return u.error;
  return `Up to date (${short(u.localDigest)})`;
}

export type ImageUpdateState = "update" | "current" | "error" | "none";

/** Whether an image has an update, from the report rows of the containers
 *  that run it (their ids): any newer digest wins, then a failed check,
 *  then up to date; "none" when no checked container runs it. */
export function imageUpdate(rows: ContainerUpdateStatus[], containerIds: string[]): { state: ImageUpdateState; rows: ContainerUpdateStatus[] } {
  const want = new Set(containerIds);
  const mine = rows.filter((r) => want.has(r.containerId));
  const state: ImageUpdateState = !mine.length ? "none"
    : mine.some((r) => r.updateAvailable) ? "update"
      : mine.some((r) => r.error) ? "error"
        : "current";
  return { state, rows: mine };
}

export function useContainerUpdates(hostId: string, opts?: { enabled?: boolean }) {
  const aggregated = isAllHosts(hostId);
  const refreshNext = useRef(false);
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: aggregated });
  const onlineHosts = (hostsQuery.data ?? []).filter((h) => h.online);
  const hostIds = aggregated ? onlineHosts.map((h) => h.id).sort() : [hostId];

  const query = useQuery({
    queryKey: [UPDATES_KEY, hostIds.join(",")],
    queryFn: async (): Promise<Map<string, ContainerUpdateStatus>> => {
      const refresh = refreshNext.current;
      refreshNext.current = false;
      const out = new Map<string, ContainerUpdateStatus>();
      const batches = await Promise.all(
        hostIds.map(async (h) => {
          try {
            return { h, rows: await getHostUpdates(h, refresh) };
          } catch (err) {
            // A single host failing in the aggregated view must not hide the rest.
            if (!aggregated) throw err;
            return { h, rows: [] as ContainerUpdateStatus[] };
          }
        }),
      );
      for (const { h, rows } of batches) {
        for (const r of rows) out.set(updateKey(h, r.containerId), r);
      }
      return out;
    },
    enabled: (opts?.enabled ?? true) && hostIds.length > 0 && (!aggregated || !hostsQuery.isLoading),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  /** Re-check every container, bypassing the server's digest cache. */
  const checkNow = async () => {
    refreshNext.current = true;
    const res = await query.refetch();
    if (res.isError) {
      notify({ color: "red", title: "Update check failed", message: errorMessage(res.error, "Could not check for updates") });
      return;
    }
    const count = Array.from(res.data?.values() ?? []).filter((u) => u.updateAvailable).length;
    notify({
      color: count ? "blue" : "green",
      title: "Update check",
      message: count ? `${count} container${count === 1 ? "" : "s"} can be updated` : "All checked containers are up to date",
    });
  };

  const get = (h: string, containerId: string) => query.data?.get(updateKey(h, containerId));
  /** The report rows of one host's containers. */
  const forHost = (h: string) =>
    Array.from(query.data?.entries() ?? []).filter(([k]) => k.startsWith(`${h}/`)).map(([, v]) => v);
  const count = Array.from(query.data?.values() ?? []).filter((u) => u.updateAvailable).length;

  return { ...query, get, forHost, count, checkNow, checking: query.isFetching };
}

/** Pull + recreate mutation; refreshes container lists and the update report. */
export function useUpdateContainer(onDone?: (res: ContainerUpdateResult, vars: { host: string; id: string }) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ host, id }: { host: string; id: string }) => updateContainer(host, id),
    onSuccess: (res, vars) => {
      notify(
        res.deploymentId
          ? { color: "green", title: `${res.deployment} updated`, message: res.message || "Its deployment was redeployed with the newer images." }
          : { color: "green", title: "Container updated", message: `Recreated from the latest ${res.image}` },
      );
      queryClient.invalidateQueries({
        predicate: (q) => {
          const key = String(q.queryKey[0]);
          return key.startsWith("containers") || key === UPDATES_KEY || key === "container-inspect" || key === "deployment";
        },
      });
      onDone?.(res, vars);
    },
    onError: (err) =>
      notify({ color: "red", title: "Update failed", message: errorMessage(err, "Could not update the container") }),
  });
}
