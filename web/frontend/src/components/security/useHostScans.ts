import { useQuery } from "@tanstack/react-query";
import { getHostScans, listHosts } from "../../api/client";
import { isAllHosts } from "../../HostContext";
import { LIVE_REFETCH_MS } from "../../lib/live";
import type { HostScans, ImageScan } from "../../types";

export const HOST_SCANS_KEY = "host-scans";

/**
 * Latest scan results for every image on the scoped host(s) ("All hosts" fans
 * out to every online host). Results change only when a scan runs, so this
 * polls (at the live cadence) only while a scan is in flight.
 */
export function useHostScans(hostId: string, opts?: { enabled?: boolean }) {
  const aggregated = isAllHosts(hostId);
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: aggregated });
  const hostIds = aggregated ? (hostsQuery.data ?? []).filter((h) => h.online).map((h) => h.id).sort() : [hostId];

  const query = useQuery({
    queryKey: [HOST_SCANS_KEY, hostIds.join(",")],
    queryFn: async (): Promise<Map<string, HostScans>> => {
      const out = new Map<string, HostScans>();
      await Promise.all(
        hostIds.map(async (h) => {
          try {
            out.set(h, await getHostScans(h));
          } catch (err) {
            if (!aggregated) throw err;
          }
        }),
      );
      return out;
    },
    enabled: (opts?.enabled ?? true) && hostIds.length > 0 && !!hostIds[0] && (!aggregated || !hostsQuery.isLoading),
    staleTime: 30_000,
    refetchOnWindowFocus: false,
    retry: false,
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return false;
      for (const hs of data.values()) if (hs.images.some((i) => i.running)) return LIVE_REFETCH_MS();
      return false;
    },
  });

  const imageRow = (h: string, imageId: string) => query.data?.get(h)?.images.find((i) => i.imageId === imageId);
  const byImage = (h: string, imageId: string): { scan: ImageScan | null; running: boolean } | undefined => {
    const row = imageRow(h, imageId);
    return row ? { scan: row.result, running: row.running } : undefined;
  };
  const byContainer = (h: string, containerId: string) => {
    const iid = query.data?.get(h)?.containers[containerId];
    return iid ? byImage(h, iid) : undefined;
  };
  return { ...query, byImage, byContainer };
}
