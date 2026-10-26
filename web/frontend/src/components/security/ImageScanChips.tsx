import { useQuery } from "@tanstack/react-query";
import { getImageScan } from "../../api/client";
import { ScanChips } from "./SeverityChips";
import { IMAGE_SCAN_KEY } from "./ImageVulnerabilities";

/** Severity chips for one image (container detail header). No polling. */
export default function ImageScanChips({ hostId, imageId }: { hostId: string; imageId: string }) {
  const q = useQuery({
    queryKey: [IMAGE_SCAN_KEY, hostId, imageId],
    queryFn: () => getImageScan(hostId, imageId),
    enabled: Boolean(hostId && imageId),
    staleTime: 60_000,
    retry: false,
  });
  if (!q.data) return null;
  return <ScanChips scan={q.data.result} running={q.data.running} />;
}
