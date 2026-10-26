import { useQuery } from "@tanstack/react-query";
import { Box, Callout, Code, Popup, Spinner, Text } from "@/ui/kit";
import StatusBadge from "../StatusBadge";
import { LIVE_REFETCH_MS } from "../../lib/live";
import { formatBytes, formatDate } from "../../utils";
import { durationText, getRun } from "./api";

/** One run: status, stats, snapshots, error and the (redacted) log. */
export default function RunDialog({ runId, onClose }: { runId: string | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: ["volbackups", "run", runId],
    queryFn: () => getRun(runId!),
    enabled: !!runId,
    refetchInterval: (query) => (query.state.data?.status === "running" ? LIVE_REFETCH_MS() : false),
  });
  const r = q.data;
  return (
    <Popup open={!!runId} onOpenChange={(o) => { if (!o) onClose(); }} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>
            {r ? `${r.kind === "restore" ? "Restore" : "Backup"}: ${r.jobName}` : "Run"} {r && <StatusBadge status={r.status} />}
          </Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {!r ? <Spinner /> : (
            <>
              <Box direction="row" wrap gap={5}>
                <Text size="sm"><Text as="span" color="muted">Started </Text>{formatDate(r.startedAt)}</Text>
                <Text size="sm"><Text as="span" color="muted">Duration </Text>{durationText(r.durationSec)}</Text>
                {r.kind === "backup" ? (
                  <>
                    <Text size="sm"><Text as="span" color="muted">Added </Text>{formatBytes(r.bytesAdded)}</Text>
                    <Text size="sm"><Text as="span" color="muted">Files </Text>{r.stats.filesTotal ?? 0} ({r.stats.filesNew ?? 0} new, {r.stats.filesChanged ?? 0} changed)</Text>
                  </>
                ) : (
                  <Text size="sm"><Text as="span" color="muted">Restored </Text>{r.stats.filesRestored ?? 0} files, {formatBytes(r.stats.bytesRestored ?? 0)}</Text>
                )}
              </Box>
              <Text size="sm"><Text as="span" color="muted">Volumes </Text>{r.volumes.join(", ") || "-"}</Text>
              {r.snapshots.length > 0 && (
                <Text size="sm"><Text as="span" color="muted">Snapshots </Text>{r.snapshots.map((s) => `${s.volume}@${s.id.slice(0, 8)}`).join(", ")}</Text>
              )}
              {r.error && <Callout color={r.status === "warning" ? "warning" : "danger"}><pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{r.error}</pre></Callout>}
              <Code block>{r.log || (r.status === "running" ? "running…" : "(no log)")}</Code>
            </>
          )}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}
