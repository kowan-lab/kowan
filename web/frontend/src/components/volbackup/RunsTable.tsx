import { useMemo } from "react";
import { Box, Table, Text, Tooltip, type Column } from "@/ui/kit";
import StatusBadge from "../StatusBadge";
import { formatBytes, formatDate, timeAgo } from "../../utils";
import { durationText, type BackupRun } from "./api";

/** Run history table shared by the Backups page and the detail sections. */
export default function RunsTable({ runs, loading, onOpen, hostName, compact = false }: {
  runs: BackupRun[]; loading?: boolean; onOpen: (id: string) => void; hostName?: (id: string) => string; compact?: boolean;
}) {
  const cols = useMemo<Column<BackupRun>[]>(() => {
    const c: Column<BackupRun>[] = [
      { key: "startedAt", header: "Started", sortable: true, cell: (r) => <Tooltip content={formatDate(r.startedAt)}><Text size="sm">{timeAgo(r.startedAt)}</Text></Tooltip> },
      {
        key: "jobName", header: "Job", sortable: true,
        cell: (r) => (
          <Box direction="column">
            <Text size="sm">{r.kind === "restore" ? "↺ " : ""}{r.jobName}</Text>
            {!compact && <Text size="xs" color="muted">{r.volumes.join(", ")}{hostName && r.hostId ? ` · ${hostName(r.hostId)}` : ""}</Text>}
          </Box>
        ),
      },
      { key: "status", header: "Result", cell: (r) => <StatusBadge status={r.status} /> },
      { key: "bytesAdded", header: "Added", align: "right", sortable: true, cell: (r) => <Text size="sm" mono>{r.kind === "backup" && r.status !== "skipped" ? formatBytes(r.bytesAdded) : "-"}</Text> },
      { key: "durationSec", header: "Duration", align: "right", sortable: true, cell: (r) => <Text size="sm" mono>{durationText(r.durationSec)}</Text> },
      { key: "trigger", header: "Trigger", cell: (r) => <Text size="sm" color="muted">{r.trigger}</Text> },
    ];
    if (!compact) c.push({ key: "error", header: "Message", grow: true, truncate: true, cell: (r) => <Text size="sm" color="muted">{r.error.split("\n")[0]}</Text> });
    return c;
  }, [compact, hostName]);
  return (
    <Table<BackupRun>
      data={runs}
      columns={cols}
      rowKey={(r) => r.id}
      loading={loading}
      onRowClick={(r) => onOpen(r.id)}
      hoverable
      pagination={!compact}
      defaultPageSize={25}
      emptyText={<Text size="sm" color="muted">No runs yet</Text>}
    />
  );
}
