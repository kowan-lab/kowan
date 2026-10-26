import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Table, Text, Tooltip, type Column } from "@/ui/kit";
import SectionCard from "../SectionCard";
import StatusBadge from "../StatusBadge";
import { LIVE_REFETCH_MS } from "../../lib/live";
import { notify } from "../../ui/toast";
import { errorMessage, timeAgo } from "../../utils";
import { inZone, untilText } from "../schedules/api";
import JobEditor from "./JobEditor";
import RunDialog from "./RunDialog";
import { listJobs, retentionText, runJob, type BackupJob } from "./api";

/**
 * Backup jobs whose selector is this deployment, plus "New backup job"
 * pre-filled with the deployment and its host.
 * Insert as `<DeploymentBackupSection deploymentId={id} deploymentName={name} hostId={targets[0]} />`.
 */
export default function DeploymentBackupSection({ deploymentId, deploymentName, hostId }: { deploymentId: string; deploymentName: string; hostId: string }) {
  const qc = useQueryClient();
  const jobs = useQuery({ queryKey: ["volbackups", "jobs", "deployment", deploymentId], queryFn: () => listJobs({ deploymentId }), refetchInterval: LIVE_REFETCH_MS });
  const [open, setOpen] = useState(false);
  const [edit, setEdit] = useState<BackupJob | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: (j: BackupJob) => runJob(j.id),
    onSuccess: (r) => { setRunId(r.id); qc.invalidateQueries({ queryKey: ["volbackups"] }); },
    onError: (e) => notify({ color: "red", title: "Backup failed", message: errorMessage(e, "Could not start the backup") }),
  });
  const cols: Column<BackupJob>[] = [
    { key: "name", header: "Job", cell: (j) => <Text size="sm" weight="semibold">{j.name}</Text> },
    { key: "schedule", header: "Schedule", cell: (j) => <Text size="sm" mono>{j.schedule || "manual"}</Text> },
    { key: "retention", header: "Retention", cell: (j) => <Text size="sm" color="muted">{retentionText(j.retention)}</Text> },
    { key: "next", header: "Next run", cell: (j) => j.nextRun ? <Tooltip content={inZone(j.nextRun, j.timezone)}><Text size="sm">{untilText(j.nextRun)}</Text></Tooltip> : <Text size="sm" color="muted">-</Text> },
    {
      key: "last", header: "Last result",
      cell: (j) => j.running ? <StatusBadge status="running" /> : j.lastRun ? (
        <Box direction="row" gap={1} align="center"><StatusBadge status={j.lastRun.status} /><Text size="xs" color="muted">{timeAgo(j.lastRun.startedAt)}</Text></Box>
      ) : <Text size="sm" color="muted">never</Text>,
    },
    {
      key: "actions", header: "", align: "right",
      cell: (j) => (
        <Box direction="row" gap={1} justify="end" wrap="nowrap">
          <Button size="xs" variant="outline" color="neutral" disabled={j.running} loading={run.isPending && run.variables?.id === j.id} onClick={() => run.mutate(j)}>Back up now</Button>
          <Button size="xs" variant="clear" aria-label="Edit" iconLeft="edit-outline-rounded" onClick={() => setEdit(j)} />
        </Box>
      ),
    },
  ];
  return (
    <SectionCard
      title="Volume backups"
      description="restic backup jobs covering this deployment's volumes."
      noBodyPadding
      actions={<Button size="sm" variant="outline" color="neutral" iconLeft="add-rounded" disabled={!hostId} onClick={() => setOpen(true)}>New backup job</Button>}
    >
      <Table<BackupJob> data={jobs.data ?? []} columns={cols} rowKey={(j) => j.id} loading={jobs.isLoading}
        emptyText={<Text size="sm" color="muted">No backup job for this deployment</Text>} />
      <JobEditor
        open={open || edit !== null}
        job={edit}
        onClose={() => { setOpen(false); setEdit(null); }}
        onSaved={() => qc.invalidateQueries({ queryKey: ["volbackups"] })}
        initial={{ name: `${deploymentName} volumes`, hostId, selector: { mode: "deployment", deploymentId }, hooks: { stopContainers: true, pre: [], post: [] } }}
      />
      <RunDialog runId={runId} onClose={() => setRunId(null)} />
    </SectionCard>
  );
}
