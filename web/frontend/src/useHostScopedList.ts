import { useQuery } from "@tanstack/react-query";

import { listHosts } from "./api/client";
import { isAllHosts } from "./HostContext";

// WithHost tags a list row with the host it came from, populated only in the
// aggregated "All hosts" view. Pages spread it onto their row type and render a
// Host column when isAllHosts(hostId) is true.
export interface WithHost {
  _hostId?: string;
  _hostName?: string;
}

// useHostScopedList runs a host-scoped list query for a single host, OR, when the
// "All hosts" view is selected, fans the same query out to every ONLINE host in
// parallel and merges the results, tagging each row with its host. A failing
// host is skipped (its rows are simply absent) so one unreachable host never
// blanks the whole list.
//
// fetchOne must be a stable reference (module-level api client function) so the
// query key — not the function identity — drives refetching.
export function useHostScopedList<T>(
  base: string,
  hostId: string,
  fetchOne: (hostId: string) => Promise<T[]>,
  opts?: { refetchInterval?: number | (() => number) },
) {
  const aggregated = isAllHosts(hostId);

  const hostsQuery = useQuery({
    queryKey: ["hosts"],
    queryFn: listHosts,
    enabled: aggregated,
    // Re-evaluate the online-host set on a cadence so hosts that come online or
    // go offline after the view is shown enter/leave the fan-out.
    refetchInterval: aggregated ? (opts?.refetchInterval ?? 15000) : undefined,
  });

  const onlineHosts = (hostsQuery.data ?? []).filter((h) => h.online);
  const hostKey = aggregated
    ? onlineHosts
        .map((h) => h.id)
        .sort()
        .join(",")
    : hostId;

  // The aggregated key carries an explicit "all" segment: with a single online
  // host, hostKey alone would equal that host's id and the tagged (aggregated)
  // rows would share a cache entry with the untagged single-host rows.
  const listQuery = useQuery({
    queryKey: aggregated ? [base, "host", "all", hostKey] : [base, "host", hostKey],
    queryFn: async (): Promise<(T & WithHost)[]> => {
      if (!aggregated) {
        return (await fetchOne(hostId)) as (T & WithHost)[];
      }
      const batches = await Promise.all(
        onlineHosts.map(async (h) => {
          try {
            const rows = await fetchOne(h.id);
            return rows.map((row) => ({
              ...row,
              _hostId: h.id,
              _hostName: h.name || h.id,
            })) as (T & WithHost)[];
          } catch {
            return [] as (T & WithHost)[];
          }
        }),
      );
      return batches.flat();
    },
    // In aggregated mode, wait until the host list has loaded so the fan-out has
    // its targets.
    enabled: !aggregated || !hostsQuery.isLoading,
    refetchInterval: opts?.refetchInterval,
  });

  return { ...listQuery, aggregated };
}
