import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Callout, Menu, Select, Table, Text, Tooltip, type Column } from "@/ui/kit";
import SectionCard from "../SectionCard";
import { LIVE_REFETCH_MS } from "../../lib/live";
import { notify } from "../../ui/toast";
import { errorMessage, formatBytes, formatDate, timeAgo } from "../../utils";
import JobEditor from "./JobEditor";
import RestoreDialog from "./RestoreDialog";
import RunDialog from "./RunDialog";
import RunsTable from "./RunsTable";
import SnapshotBrowser from "./SnapshotBrowser";
import { backupVolume, listRepos, listRuns, volumeSnapshots, type Snapshot } from "./api";

/**
 * Backup band for the volume detail page: snapshots of this volume across the
 * repositories in use, back up now, restore, browse, and recent runs.
 * Insert as `<VolumeBackupSection hostId={host} volume={name} />`.
 */
export default function VolumeBackupSection({ hostId, volume }: { hostId: string; volume: string }) {
  const qc = useQueryClient();
  const repos = useQuery({ queryKey: ["volbackups", "repos"], queryFn: listRepos });
  const snaps = useQuery({
    queryKey: ["volbackups", "volume-snapshots", hostId, volume],
    queryFn: () => volumeSnapshots(hostId, volume),
    staleTime: 30_000,
  });
  const runs = useQuery({
    queryKey: ["volbackups", "runs", "volume", hostId, volume],
    queryFn: () => listRuns({ volume, hostId, limit: 10 }),
    refetchInterval: LIVE_REFETCH_MS,
  });
  const usable = (repos.data ?? []).filter((r) => r.type !== "local" || r.hostId === hostId);
  const [repoId, setRepoId] = useState("");
  const chosen = repoId || usable[0]?.id || "";
  const [restore, setRestore] = useState<Snapshot | null>(null);
  const [browse, setBrowse] = useState<Snapshot | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [jobOpen, setJobOpen] = useState(false);
  const repoName = (id: string) => repos.data?.find((r) => r.id === id)?.name ?? id;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["volbackups", "runs"] });
    qc.invalidateQueries({ queryKey: ["volbackups", "volume-snapshots", hostId, volume] });
  };

  const now = useMutation({
    mutationFn: () => backupVolume(hostId, volume, chosen),
    onSuccess: (run) => { notify({ color: "green", title: "Backup started", message: `${volume} → ${repoName(chosen)}` }); setRunId(run.id); refresh(); },
    onError: (e) => notify({ color: "red", title: "Backup failed", message: errorMessage(e, "Could not start the backup") }),
  });

  const running = (runs.data ?? []).some((r) => r.status === "running");
  const cols: Column<Snapshot>[] = [
    { key: "time", header: "Taken", sortable: true, cell: (s) => <Tooltip content={formatDate(s.time)}><Text size="sm">{timeAgo(s.time)}</Text></Tooltip> },
    { key: "shortId", header: "Snapshot", cell: (s) => <Text size="sm" mono>{s.shortId}</Text> },
    { key: "repo", header: "Repository", cell: (s) => <Text size="sm">{repoName(s.repoId)}</Text> },
    { key: "size", header: "Size", align: "right", cell: (s) => <Text size="sm" mono>{s.sizeBytes ? formatBytes(s.sizeBytes) : "-"}</Text> },
    { key: "tags", header: "Tags", cell: (s) => <Text size="xs" color="muted">{s.tags.join(", ")}</Text> },
    {
      key: "actions", header: "", align: "right",
      cell: (s) => (
        <Box direction="row" gap={1} justify="end" wrap="nowrap">
          <Button size="xs" variant="outline" color="neutral" onClick={() => setBrowse(s)}>Browse</Button>
          <Button size="xs" variant="outline" color="neutral" onClick={() => setRestore(s)}>Restore</Button>
        </Box>
      ),
    },
  ];

  return (
    <SectionCard
      title="Backups"
      description="restic snapshots of this volume."
      noBodyPadding
      actions={
        <Box direction="row" gap={2} align="center" wrap>
          {usable.length > 0 && (
            <Select size="sm" width={180} aria-label="Repository" value={chosen} onChange={setRepoId}
              options={usable.map((r) => ({ value: r.id, label: r.name, trailing: r.type }))} />
          )}
          <Button size="sm" iconLeft="archive-outline-rounded" disabled={!chosen || running} loading={now.isPending} onClick={() => now.mutate()}>
            {running ? "Running…" : "Back up now"}
          </Button>
          <Menu>
            <Menu.Trigger>
              <Button size="sm" variant="outline" color="neutral" aria-label="More" iconLeft="menu-rounded" />
            </Menu.Trigger>
            <Menu.Content>
              <Menu.Item onSelect={() => setJobOpen(true)}>New scheduled job for this volume</Menu.Item>
              <Menu.Item onSelect={() => qc.invalidateQueries({ queryKey: ["volbackups", "volume-snapshots", hostId, volume] })}>Refresh snapshots</Menu.Item>
            </Menu.Content>
          </Menu>
        </Box>
      }
    >
      {usable.length === 0 && !repos.isLoading && (
        <div className="px-[var(--page-px,16px)] py-5">
          <Callout color="info">No backup repository yet. Add one on the Backups page.</Callout>
        </div>
      )}
      {(snaps.data?.errors ?? []).map((e) => (
        <div key={e.repoId} className="px-[var(--page-px,16px)] pt-5">
          <Callout color="warning">{repoName(e.repoId)}: {e.error}</Callout>
        </div>
      ))}
      <Table<Snapshot>
        data={snaps.data?.snapshots ?? []}
        columns={cols}
        rowKey={(s) => `${s.repoId}:${s.id}`}
        loading={snaps.isLoading}
        emptyText={<Text size="sm" color="muted">{snaps.isError ? errorMessage(snaps.error, "Could not list snapshots") : "No snapshots of this volume"}</Text>}
      />
      {(runs.data ?? []).length > 0 && (
        <>
          <div className="border-t bg-muted/50 px-[var(--page-px,16px)] py-3"><Text size="xs" color="muted" transform="uppercase">Recent runs</Text></div>
          <RunsTable runs={runs.data ?? []} onOpen={setRunId} compact />
        </>
      )}
      <RestoreDialog snapshot={restore} hostId={hostId} volume={volume} onClose={() => setRestore(null)} onStarted={(r) => { setRunId(r.id); refresh(); }} />
      <SnapshotBrowser snapshot={browse} hostId={hostId} onClose={() => setBrowse(null)} />
      <RunDialog runId={runId} onClose={() => { setRunId(null); refresh(); }} />
      <JobEditor open={jobOpen} onClose={() => setJobOpen(false)} onSaved={() => qc.invalidateQueries({ queryKey: ["volbackups"] })}
        initial={{ name: `${volume} nightly`, hostId, repoId: chosen, selector: { mode: "names", volumes: [volume] } }} />
    </SectionCard>
  );
}
