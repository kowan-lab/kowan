import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Callout, Icon, Table, Text, type Column } from "@/ui/kit";
import { getImageScan, startImageScan } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { liveUnlessGone } from "../../lib/live";
import { notify } from "../../ui/toast";
import { errorMessage, formatDate, timeAgo } from "../../utils";
import SectionCard from "../SectionCard";
import type { ScanFinding, ScanSeverity } from "../../types";
import { SeverityChips, SeverityPill } from "./SeverityChips";
import { HOST_SCANS_KEY } from "./useHostScans";

const SEV_ORDER: Record<ScanSeverity, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, UNKNOWN: 4 };

export const IMAGE_SCAN_KEY = "image-scan";

/**
 * Image detail "Vulnerabilities" band: severity counts, scan metadata, a
 * "Scan now" action and a searchable, paginated findings table. Polls only
 * while a scan is running on the server.
 */
export default function ImageVulnerabilities({ hostId, imageId }: { hostId: string; imageId: string }) {
  const { user } = useAuth();
  const canScan = user?.role === "admin" || user?.role === "operator";
  const queryClient = useQueryClient();

  const q = useQuery({
    queryKey: [IMAGE_SCAN_KEY, hostId, imageId],
    queryFn: () => getImageScan(hostId, imageId),
    enabled: Boolean(hostId && imageId),
    refetchInterval: (query) => (query.state.data?.running ? liveUnlessGone(query) : false),
  });

  const scan = useMutation({
    mutationFn: (force: boolean) => startImageScan(hostId, imageId, force),
    onSuccess: (res) => {
      queryClient.setQueryData([IMAGE_SCAN_KEY, hostId, imageId], (old: unknown) => ({ ...(old as object), ...res }));
      queryClient.invalidateQueries({ queryKey: [IMAGE_SCAN_KEY, hostId, imageId] });
      queryClient.invalidateQueries({ predicate: (x) => x.queryKey[0] === HOST_SCANS_KEY });
      if (!res.running) notify({ color: "green", title: "Scan", message: "A fresh result (< 24 h) already exists" });
    },
    onError: (err) => notify({ color: "red", title: "Scan failed", message: errorMessage(err, "Could not start the scan") }),
  });

  const data = q.data;
  const result = data?.result ?? null;
  const running = Boolean(data?.running) || scan.isPending;
  const findings = result?.findings ?? [];

  const columns: Column<ScanFinding>[] = [
    {
      key: "id",
      header: "CVE",
      sortable: true,
      cell: (f) =>
        f.url ? (
          <a href={f.url} target="_blank" rel="noreferrer" className="cell-link" title={f.title}>
            {f.id}
          </a>
        ) : (
          <span title={f.title}>{f.id}</span>
        ),
    },
    { key: "pkg", header: "Package", sortable: true, truncate: true, grow: true },
    { key: "installed", header: "Installed", truncate: true },
    {
      key: "fixed",
      header: "Fixed in",
      truncate: true,
      cell: (f) => (f.fixed ? f.fixed : <Text size="xs" color="muted">no fix</Text>),
    },
    {
      key: "severity",
      header: "Severity",
      sortable: true,
      sortFn: (a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity],
      filter: {
        kind: "select",
        options: (["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"] as ScanSeverity[]).map((s) => ({ value: s, label: s.toLowerCase() })),
      },
      filterFn: (f, v) => !Array.isArray(v) || v.length === 0 || v.includes(f.severity),
      cell: (f) => <SeverityPill severity={f.severity} />,
    },
  ];

  const description = result
    ? result.status === "ok"
      ? `Trivy ${result.scannerVersion} · scanned ${timeAgo(result.scannedAt)}${result.os ? ` · ${result.os}` : ""} · ${result.total} findings, ${result.fixable} fixable`
      : `Last scan failed ${timeAgo(result.scannedAt)}`
    : "Not scanned yet";

  return (
    <SectionCard
      title="Vulnerabilities"
      description={running ? "Scanning with Trivy on this host… the first scan downloads the vulnerability DB." : description}
      noBodyPadding
      actions={
        <Box direction="row" align="center" gap={2}>
          {result?.status === "ok" && !running && <SeverityChips counts={result.counts} />}
          {canScan && (
            <Button
              variant="outline"
              size="sm"
              loading={running}
              onClick={() => scan.mutate(Boolean(result))}
            >
              {result ? "Re-scan" : "Scan now"}
            </Button>
          )}
        </Box>
      }
    >
      {q.isError ? (
        <Box padding={4}>
          <Text color="danger" size="sm">{errorMessage(q.error, "Could not load the scan result.")}</Text>
        </Box>
      ) : result?.status === "error" ? (
        <Box padding={4}>
          <Callout color="danger" iconLeft={<Icon name="error-outline-rounded" />} title="Scan failed">
            {result.error}
          </Callout>
        </Box>
      ) : (
        <Table<ScanFinding>
          data={findings}
          columns={columns}
          rowKey={(f) => `${f.id}/${f.pkg}/${f.installed}`}
          globalSearch
          searchPlaceholder="Search CVE, package…"
          pagination
          defaultPageSize={25}
          defaultSort={[{ key: "severity", dir: "asc" }]}
          loading={q.isLoading}
          emptyText={
            result
              ? "No known vulnerabilities."
              : running
                ? "Scanning…"
                : canScan
                  ? "Not scanned yet. Run a scan to list known vulnerabilities."
                  : "Not scanned yet."
          }
        />
      )}
      {result && result.total > findings.length && (
        <Box padding={3}>
          <Text size="xs" color="muted">
            Showing the {findings.length} most severe of {result.total} findings · {formatDate(result.scannedAt)}
          </Text>
        </Box>
      )}
    </SectionCard>
  );
}
