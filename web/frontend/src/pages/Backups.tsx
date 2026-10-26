import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Box, Button, Callout, Icon, Popup, Table, Tabs, Text, Tooltip, type Column } from "@/ui/kit";
import { listDeployments, listHosts } from "../api/client";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import { inZone, untilText } from "../components/schedules/api";
import JobEditor from "../components/volbackup/JobEditor";
import RepoEditor from "../components/volbackup/RepoEditor";
import RunDialog from "../components/volbackup/RunDialog";
import RunsTable from "../components/volbackup/RunsTable";
import {
  deleteJob, deleteRepo, listJobs, listRepos, listRuns, retentionText, runJob, selectorText, testRepo, updateJob,
  type BackupJob, type BackupRepo,
} from "../components/volbackup/api";
import { LIVE_REFETCH_MS, SLOW_REFETCH_MS } from "../lib/live";
import { notify } from "../ui/toast";
import { errorMessage, formatBytes, timeAgo } from "../utils";

function repoTarget(r: BackupRepo): string {
  const c = r.config;
  switch (r.type) {
    case "local": return c.path ?? "";
    case "s3": return `${c.endpoint || "s3.amazonaws.com"}/${c.bucket ?? ""}${c.prefix ? "/" + c.prefix : ""}`;
    case "sftp": return `${c.user}@${c.host}:${c.path}`;
    case "rest": return c.url ?? "";
  }
}

export default function Backups() {
  const qc = useQueryClient();
  const [tab, setTab] = useState("jobs");
  const repos = useQuery({ queryKey: ["volbackups", "repos"], queryFn: listRepos, refetchInterval: SLOW_REFETCH_MS });
  const jobs = useQuery({ queryKey: ["volbackups", "jobs"], queryFn: () => listJobs(), refetchInterval: LIVE_REFETCH_MS });
  const runs = useQuery({ queryKey: ["volbackups", "runs", "all"], queryFn: () => listRuns({ limit: 300 }), refetchInterval: LIVE_REFETCH_MS });
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const hostName = useMemo(() => {
    const m = new Map((hosts.data ?? []).map((h) => [h.id, h.name]));
    return (id: string) => m.get(id) ?? id;
  }, [hosts.data]);
  const depName = useMemo(() => {
    const m = new Map((deployments.data ?? []).map((d) => [d.id, d.name]));
    return (id: string) => m.get(id) ?? id;
  }, [deployments.data]);
  const repoName = (id: string) => repos.data?.find((r) => r.id === id)?.name ?? id;

  const [repoEdit, setRepoEdit] = useState<BackupRepo | null>(null);
  const [repoNew, setRepoNew] = useState(false);
  const [jobEdit, setJobEdit] = useState<BackupJob | null>(null);
  const [jobNew, setJobNew] = useState(false);
  const [delRepo, setDelRepo] = useState<BackupRepo | null>(null);
  const [delJob, setDelJob] = useState<BackupJob | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [testLog, setTestLog] = useState<{ name: string; ok: boolean; log: string } | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["volbackups"] });

  const run = useMutation({
    mutationFn: (j: BackupJob) => runJob(j.id),
    onSuccess: (r) => { notify({ color: "green", title: "Backup started", message: r.jobName }); setRunId(r.id); refresh(); },
    onError: (e) => notify({ color: "red", title: "Backup failed", message: errorMessage(e, "Could not start the backup") }),
  });
  const toggle = useMutation({ mutationFn: (j: BackupJob) => updateJob(j.id, { enabled: !j.enabled }), onSuccess: refresh });
  const test = useMutation({
    mutationFn: (r: BackupRepo) => testRepo(r.id),
    onSuccess: (res, r) => { setTestLog({ name: r.name, ok: res.ok, log: res.log }); refresh(); },
    onError: (e) => notify({ color: "red", title: "Test failed", message: errorMessage(e, "Could not reach the repository") }),
  });
  const rmRepo = useMutation({
    mutationFn: (r: BackupRepo) => deleteRepo(r.id), onSuccess: () => { setDelRepo(null); refresh(); },
    onError: (e) => notify({ color: "red", title: "Delete failed", message: errorMessage(e, "Could not delete") }),
  });
  const rmJob = useMutation({
    mutationFn: (j: BackupJob) => deleteJob(j.id), onSuccess: () => { setDelJob(null); refresh(); },
    onError: (e) => notify({ color: "red", title: "Delete failed", message: errorMessage(e, "Could not delete") }),
  });

  const failing = (jobs.data ?? []).filter((j) => j.lastRun?.status === "failed").length;

  const jobCols: Column<BackupJob>[] = [
    {
      key: "name", header: "Job", sortable: true, accessor: (j) => j.name,
      cell: (j) => (
        <Box direction="column">
          <Text size="sm" weight="semibold">{j.name}</Text>
          <Text size="xs" color="muted">{selectorText(j.selector, depName)} · {hostName(j.hostId)}</Text>
        </Box>
      ),
    },
    { key: "repo", header: "Repository", cell: (j) => <Text size="sm">{repoName(j.repoId)}</Text> },
    { key: "schedule", header: "Schedule", cell: (j) => <Text size="sm" mono>{j.schedule || "manual"}{j.timezone ? `  ${j.timezone}` : ""}</Text> },
    { key: "retention", header: "Retention", cell: (j) => <Text size="sm" color="muted">{retentionText(j.retention)}</Text> },
    {
      key: "next", header: "Next run", sortable: true, accessor: (j) => j.nextRun || "~",
      cell: (j) => !j.enabled ? <StatusBadge status="disabled" /> : j.nextRun ? (
        <Tooltip content={inZone(j.nextRun, j.timezone)}><Text size="sm">{untilText(j.nextRun)}</Text></Tooltip>
      ) : <Text size="sm" color="muted">-</Text>,
    },
    {
      key: "last", header: "Last result",
      cell: (j) => j.running ? <StatusBadge status="running" /> : j.lastRun ? (
        <Box direction="row" gap={1} align="center">
          <StatusBadge status={j.lastRun.status} detail={j.lastRun.status !== "skipped" ? formatBytes(j.lastRun.bytesAdded) : undefined} />
          <Text size="xs" color="muted">{timeAgo(j.lastRun.startedAt)}</Text>
        </Box>
      ) : <Text size="sm" color="muted">never</Text>,
    },
    {
      key: "actions", header: "", align: "right",
      cell: (j) => (
        <Box direction="row" gap={1} justify="end" wrap="nowrap">
          <Tooltip content="Back up now">
            <Button variant="clear" size="sm" aria-label="Back up now" iconLeft="play-arrow-rounded" disabled={j.running}
              loading={run.isPending && run.variables?.id === j.id} onClick={() => run.mutate(j)} />
          </Tooltip>
          {j.lastRun && <Tooltip content="Last log"><Button variant="clear" size="sm" aria-label="Last log" iconLeft="description-outline-rounded" onClick={() => setRunId(j.lastRun!.id)} /></Tooltip>}
          <Tooltip content={j.enabled ? "Disable" : "Enable"}>
            <Button variant="clear" size="sm" aria-label={j.enabled ? "Disable" : "Enable"}
              iconLeft={j.enabled ? "pause-rounded" : "play-circle-outline-rounded"} onClick={() => toggle.mutate(j)} />
          </Tooltip>
          <Tooltip content="Edit"><Button variant="clear" size="sm" aria-label="Edit" iconLeft="edit-outline-rounded" onClick={() => setJobEdit(j)} /></Tooltip>
          <Tooltip content="Delete"><Button variant="clear" size="sm" color="danger" aria-label="Delete" iconLeft="delete-outline-rounded" onClick={() => setDelJob(j)} /></Tooltip>
        </Box>
      ),
    },
  ];

  const repoCols: Column<BackupRepo>[] = [
    { key: "name", header: "Repository", sortable: true, cell: (r) => <Text size="sm" weight="semibold">{r.name}</Text> },
    { key: "type", header: "Type", cell: (r) => <Badge size="sm" variant="outline" color="gray">{r.type}</Badge> },
    {
      key: "target", header: "Location",
      cell: (r) => <Box direction="column"><Text size="sm" mono>{repoTarget(r)}</Text>{r.type === "local" && <Text size="xs" color="muted">on {hostName(r.hostId)}</Text>}</Box>,
    },
    { key: "init", header: "State", cell: (r) => <StatusBadge status={r.initialized ? "ok" : "new"} value={r.initialized ? "initialised" : "not initialised"} /> },
    { key: "jobs", header: "Jobs", align: "right", cell: (r) => <Text size="sm" mono>{(jobs.data ?? []).filter((j) => j.repoId === r.id).length}</Text> },
    {
      key: "actions", header: "", align: "right",
      cell: (r) => (
        <Box direction="row" gap={1} justify="end" wrap="nowrap">
          <Button size="xs" variant="outline" color="neutral" loading={test.isPending && test.variables?.id === r.id} onClick={() => test.mutate(r)}>Test</Button>
          <Tooltip content="Edit"><Button variant="clear" size="sm" aria-label="Edit" iconLeft="edit-outline-rounded" onClick={() => setRepoEdit(r)} /></Tooltip>
          <Tooltip content="Delete"><Button variant="clear" size="sm" color="danger" aria-label="Delete" iconLeft="delete-outline-rounded" onClick={() => setDelRepo(r)} /></Tooltip>
        </Box>
      ),
    },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Backups"
        description="Volume backups with restic: encrypted, deduplicated snapshots to S3, SFTP, a REST server or a local path. Failures alert through your notification channels."
        actions={
          <Box direction="row" gap={2}>
            <Button variant="outline" color="neutral" iconLeft="add-rounded" onClick={() => setRepoNew(true)}>Repository</Button>
            <Button iconLeft="add-rounded" disabled={(repos.data ?? []).length === 0} onClick={() => setJobNew(true)}>New job</Button>
          </Box>
        }
      />
      {failing > 0 && <Callout color="danger">{failing} job{failing > 1 ? "s" : ""} failed on the last run.</Callout>}
      <Tabs value={tab} onValueChange={setTab}>
        <Tabs.List>
          <Tabs.Trigger value="jobs">Jobs {jobs.data ? `(${jobs.data.length})` : ""}</Tabs.Trigger>
          <Tabs.Trigger value="repos">Repositories {repos.data ? `(${repos.data.length})` : ""}</Tabs.Trigger>
          <Tabs.Trigger value="runs">Run history</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Panel value="jobs">
          <SectionCard plain noBodyPadding>
            <Table<BackupJob>
              data={jobs.data ?? []} columns={jobCols} rowKey={(j) => j.id} loading={jobs.isLoading} globalSearch hoverable stickyHeader
              emptyState={
                <EmptyState
                  icon={<Icon name="archive-outline-rounded" size="xl" />}
                  title={(repos.data ?? []).length ? "No backup jobs yet" : "Start with a repository"}
                  description={(repos.data ?? []).length ? "Pick volumes (by name, deployment, stack or the kowan.backup=true label), a schedule and a retention." : "A repository is where restic stores snapshots: an S3 bucket, an SFTP server, a REST server or a path on a host."}
                  action={(repos.data ?? []).length
                    ? <Button iconLeft="add-rounded" onClick={() => setJobNew(true)}>New job</Button>
                    : <Button iconLeft="add-rounded" onClick={() => setRepoNew(true)}>Add repository</Button>}
                />
              }
            />
          </SectionCard>
        </Tabs.Panel>
        <Tabs.Panel value="repos">
          <SectionCard plain noBodyPadding>
            <Table<BackupRepo> data={repos.data ?? []} columns={repoCols} rowKey={(r) => r.id} loading={repos.isLoading} hoverable
              emptyText={<Text size="sm" color="muted">No repositories</Text>} />
          </SectionCard>
        </Tabs.Panel>
        <Tabs.Panel value="runs">
          <SectionCard plain noBodyPadding>
            <RunsTable runs={runs.data ?? []} loading={runs.isLoading} onOpen={setRunId} hostName={hostName} />
          </SectionCard>
        </Tabs.Panel>
      </Tabs>

      <RepoEditor open={repoNew || repoEdit !== null} repo={repoEdit} onClose={() => { setRepoNew(false); setRepoEdit(null); }} onSaved={refresh} />
      <JobEditor open={jobNew || jobEdit !== null} job={jobEdit} onClose={() => { setJobNew(false); setJobEdit(null); }} onSaved={refresh} />
      <RunDialog runId={runId} onClose={() => { setRunId(null); refresh(); }} />
      <ConfirmModal opened={delRepo !== null} onClose={() => setDelRepo(null)} title="Delete repository" confirmLabel="Delete"
        loading={rmRepo.isPending} onConfirm={() => delRepo && rmRepo.mutate(delRepo)}
        message={<>Remove <b>{delRepo?.name}</b> from kowan? The snapshots stored in it are not deleted.</>} />
      <ConfirmModal opened={delJob !== null} onClose={() => setDelJob(null)} title="Delete backup job" confirmLabel="Delete"
        loading={rmJob.isPending} onConfirm={() => delJob && rmJob.mutate(delJob)}
        message={<>Delete <b>{delJob?.name}</b>? Existing snapshots stay in the repository.</>} />
      <Popup open={testLog !== null} onOpenChange={(o) => { if (!o) setTestLog(null); }} size="md">
        <Popup.Content>
          <Popup.Header><Popup.Title>{testLog?.name}: {testLog?.ok ? "reachable" : "failed"}</Popup.Title><Popup.Close /></Popup.Header>
          <Popup.Body>
            <Callout color={testLog?.ok ? "success" : "danger"}>{testLog?.ok ? "The repository is open and ready." : "restic could not open or initialise the repository."}</Callout>
            <pre className="m-0 max-h-120 overflow-auto whitespace-pre-wrap text-xs">{testLog?.log}</pre>
          </Popup.Body>
        </Popup.Content>
      </Popup>
    </Box>
  );
}
