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
  const count = Array.from(query.data?.values() ?? []).filter((u) => u.updateAvailable).length;

  return { ...query, get, count, checkNow, checking: query.isFetching };
}

/** Pull + recreate mutation; refreshes container lists and the update report. */
export function useUpdateContainer(onDone?: (res: ContainerUpdateResult, vars: { host: string; id: string }) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ host, id }: { host: string; id: string }) => updateContainer(host, id),
    onSuccess: (res, vars) => {
      notify({ color: "green", title: "Container updated", message: `Recreated from the latest ${res.image}` });
      queryClient.invalidateQueries({
        predicate: (q) => {
          const key = String(q.queryKey[0]);
          return key.startsWith("containers") || key === UPDATES_KEY || key === "container-inspect";
        },
      });
      onDone?.(res, vars);
    },
    onError: (err) =>
      notify({ color: "red", title: "Update failed", message: errorMessage(err, "Could not update the container") }),
  });
}
