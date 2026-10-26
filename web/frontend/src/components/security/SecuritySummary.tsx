import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Button, Table, Text, type Column } from "@/ui/kit";
import { getScanSummary, listHosts } from "../../api/client";
import { SLOW_REFETCH_MS } from "../../lib/live";
import { timeAgo } from "../../utils";
import SectionCard from "../SectionCard";
import type { HostScanSummary } from "../../types";
import { SeverityChips } from "./SeverityChips";

type Row = HostScanSummary & { hostName: string };

/** Dashboard band: vulnerability totals per host over the stored scans. */
export default function SecuritySummary() {
  const navigate = useNavigate();
  const summary = useQuery({ queryKey: ["scan-summary"], queryFn: getScanSummary, refetchInterval: SLOW_REFETCH_MS * 4 });
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const names = new Map((hosts.data ?? []).map((h) => [h.id, h.name]));
  const rows: Row[] = (summary.data ?? []).map((s) => ({ ...s, hostName: names.get(s.hostId) ?? s.hostId }));
  if (!summary.isLoading && rows.length === 0) return null;
  const columns: Column<Row>[] = [
    { key: "hostName", header: "Host", sortable: true },
    { key: "scanned", header: "Images scanned", numeric: true, align: "right" },
    { key: "counts", header: "Vulnerabilities", cell: (r) => <SeverityChips counts={r.counts} /> },
    { key: "lastScanAt", header: "Last scan", cell: (r) => <Text size="xs" color="muted">{timeAgo(r.lastScanAt)}</Text> },
  ];
  return (
    <SectionCard
      title="Security"
      description="Known vulnerabilities in scanned images (Trivy)."
      actions={<Button variant="outline" size="sm" onClick={() => navigate("/images")}>Images</Button>}
      noBodyPadding
    >
      <Table<Row> columns={columns} data={rows} rowKey={(r) => r.hostId} loading={summary.isLoading} />
    </SectionCard>
  );
}
