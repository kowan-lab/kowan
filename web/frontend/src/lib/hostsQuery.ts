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

/**
 * Resolves a host id to what the user reads: the host's display name (the
 * local host's id is always `local`, its name is whatever it was renamed to).
 * While the list loads the label is empty rather than the id, so a renamed
 * host never flashes its key; the id shows only for a host the list does not
 * know, or when the list cannot be read. Reads the shared list without
 * refetching on mount, like the breadcrumb did.
 */
export function useHostName(): (id: string) => string {
  const { data, isError } = useQuery({ ...hostsQuery, refetchOnMount: false, retryOnMount: false });
  return (id) => (data ? data.find((h) => h.id === id)?.name || id : isError ? id : "");
}
