import { Box, StatePill, Table, Text, Tooltip, type Column } from "@/ui/kit";
import type { AutoUpdateHistoryEntry } from "../../types";
import { formatDate, timeAgo } from "../../utils";
import { SeverityChips } from "../security/SeverityChips";
import { outcomeColor, outcomeText } from "./labels";

const short = (d: string) => (d ? d.replace(/^sha256:/, "").slice(0, 12) : "");

/** Auto-update history rows (object detail and the global page). */
export default function HistoryTable({
  items, loading, showObject = false, hostName,
}: {
  items: AutoUpdateHistoryEntry[];
  loading?: boolean;
  showObject?: boolean;
  hostName?: (id: string) => string;
}) {
  const columns: Column<AutoUpdateHistoryEntry>[] = [
    {
      key: "startedAt",
      header: "When",
      cell: (h) => (
        <Tooltip content={formatDate(h.startedAt)}>
          <span tabIndex={0}><Text size="xs">{timeAgo(h.startedAt)}</Text></span>
        </Tooltip>
      ),
    },
    ...(showObject
      ? [
          {
            key: "targetName",
            header: "Object",
            cell: (h: AutoUpdateHistoryEntry) => (
              <Box direction="column" gap={0}>
                <Text size="sm">{h.targetName || h.target}</Text>
                <Text size="xs" color="muted">{h.kind}{hostName && h.hostId ? ` · ${hostName(h.hostId)}` : ""}</Text>
              </Box>
            ),
          } as Column<AutoUpdateHistoryEntry>,
        ]
      : []),
    {
      key: "outcome",
      header: "Outcome",
      cell: (h) => <StatePill status={h.outcome} label={outcomeText(h.outcome)} color={outcomeColor(h.outcome)} size="sm" />,
    },
    {
      key: "message",
      header: "Details",
      grow: true,
      cell: (h) => (
        <Box direction="column" gap={0} style={{ minWidth: 0 }}>
          <Text size="xs" style={{ whiteSpace: "normal", wordBreak: "break-word" }}>{h.message}</Text>
          {(h.fromDigest || h.toDigest) && (
            <Text size="xs" color="muted" mono>
              {h.image} {short(h.fromDigest)}{h.toDigest ? ` → ${short(h.toDigest)}` : ""}
            </Text>
          )}
        </Box>
      ),
    },
    {
      key: "scan",
      header: "Scan",
      cell: (h) =>
        h.scan ? (
          <SeverityChips
            compact
            counts={{ critical: h.scan.critical ?? 0, high: h.scan.high ?? 0, medium: h.scan.medium ?? 0, low: h.scan.low ?? 0, unknown: 0 }}
          />
        ) : null,
    },
    { key: "trigger", header: "Trigger", cell: (h) => <Text size="xs" color="muted">{h.trigger}</Text> },
  ];
  return (
    <Table<AutoUpdateHistoryEntry>
      data={items}
      columns={columns}
      rowKey={(h) => h.id}
      loading={loading}
      emptyText="No auto-update activity yet."
    />
  );
}
