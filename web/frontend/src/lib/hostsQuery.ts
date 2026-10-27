import { queryOptions, useQuery } from "@tanstack/react-query";
import { listHosts } from "@/api/client";

/**
 * The polled `["hosts"]` query, shared by the shell (HostSwitcher) and the
 * Docker events hook. One definition means one key and one set of timing
 * options: React Query keeps a single cache entry, and since every observer's
 * interval restarts when that entry updates, the observers fire together and
 * the fetches dedupe into one poll.
 */
export const HOSTS_POLL_MS = 15_000;

export const hostsQuery = queryOptions({
  queryKey: ["hosts"],
  queryFn: listHosts,
  refetchInterval: HOSTS_POLL_MS,
  // Fresh for most of a poll period: mounting a second observer does not refetch.
  staleTime: HOSTS_POLL_MS - 5_000,
});

export function useHosts() {
  return useQuery(hostsQuery);
}
