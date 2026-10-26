import { useState } from "react";
import { useNavigate, useParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  Button,
  Callout,
  CopyButton,
  Icon,
  Popup,
  Skeleton,
  StatePill,
  Table,
  Text,
  Tooltip,
  type Column,
} from "@/ui/kit";
import {
  applyGitOpsSync,
  createGitOpsSync,
  deleteGitOpsSync,
  getGitOpsPlan,
  getGitOpsRun,
  listGitOpsManaged,
  listGitOpsRuns,
  listGitOpsSyncs,
  refreshGitOpsSync,
  rotateGitOpsWebhook,
  updateGitOpsSync,
} from "../api/client";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { ExportDialog } from "../components/gitops/ExportDialog";
import { PlanView } from "../components/gitops/PlanView";
import { ActionPill, SyncStatusPill, shortSha } from "../components/gitops/status";
import { SyncFormDialog, emptySyncInput, syncToInput } from "../components/gitops/SyncFormDialog";
import { SLOW_REFETCH_MS } from "../lib/live";
import type { GitOpsRun, GitOpsSync, GitOpsSyncInput } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";

function summaryText(s: GitOpsSync): string {
  if (!s.summary) return "";
  const parts = [];
  if (s.summary.create) parts.push(`+${s.summary.create}`);
  if (s.summary.update) parts.push(`~${s.summary.update}`);
  if (s.summary.delete) parts.push(`−${s.summary.delete}`);
  return parts.join(" ");
}

function intervalText(sec: number): string {
  if (!sec) return "manual";
  if (sec % 3600 === 0) return `${sec / 3600}h`;
  if (sec % 60 === 0) return `${sec / 60}m`;
  return `${sec}s`;
}

export default function GitOps() {
  const { id } = useParams();
  return id ? <SyncDetail id={id} /> : <SyncList />;
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------

function SyncList() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const syncs = useQuery({ queryKey: ["gitops-syncs"], queryFn: listGitOpsSyncs, refetchInterval: SLOW_REFETCH_MS });
  const [creating, setCreating] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [secret, setSecret] = useState<GitOpsSync | null>(null);

  const create = useMutation({
    mutationFn: (v: GitOpsSyncInput) => createGitOpsSync(v),
    onSuccess: (s) => {
      setCreating(false);
      setSecret(s);
      qc.invalidateQueries({ queryKey: ["gitops-syncs"] });
    },
  });

  const columns: Column<GitOpsSync>[] = [
    {
      key: "name",
      header: "Sync",
      type: "link",
      sortable: true,
      accessor: (s) => s.name,
      href: (s) => `/gitops/${s.id}`,
      link: {
        label: (s) => (
          <>
            {s.name}
            <Text size="xs" color="muted" mono className="block">
              {s.repoUrl.replace(/^https?:\/\//, "")} @ {s.ref}
              {s.path ? ` : ${s.path}` : ""}
            </Text>
          </>
        ),
        onClick: (s, e) => {
          e.preventDefault();
          navigate(`/gitops/${s.id}`);
        },
      },
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      accessor: (s) => s.status,
      cell: (s) => <SyncStatusPill status={s.status} detail={s.status === "drift" ? summaryText(s) : s.status === "error" && s.planErrors ? `${s.planErrors}` : undefined} />,
    },
    {
      key: "commit",
      header: "Last commit",
      accessor: (s) => s.lastCommit,
      cell: (s) => <Text size="sm" mono>{shortSha(s.lastCommit) || "—"}</Text>,
    },
    {
      key: "applied",
      header: "Applied",
      accessor: (s) => s.lastAppliedAt,
      cell: (s) =>
        s.lastAppliedAt ? (
          <Text size="sm">
            <span className="font-mono">{shortSha(s.lastAppliedCommit)}</span>{" "}
            <Text as="span" size="xs" color="muted">{formatDate(s.lastAppliedAt)}</Text>
          </Text>
        ) : (
          <Text size="sm" color="muted">never</Text>
        ),
    },
    {
      key: "mode",
      header: "Mode",
      accessor: (s) => (s.autoApply ? "auto" : "review"),
      cell: (s) => (
        <Text size="sm" color={s.enabled ? "default" : "muted"}>
          {s.enabled ? (s.autoApply ? "auto-apply" : "review") : "disabled"} · {intervalText(s.intervalSec)}
        </Text>
      ),
    },
    {
      key: "checked",
      header: "Checked",
      accessor: (s) => s.lastCheckedAt,
      cell: (s) => <Text size="sm" color="muted">{s.lastCheckedAt ? formatDate(s.lastCheckedAt) : "—"}</Text>,
    },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="GitOps"
        description="Keep Kowan's configuration in git. A sync reads apiVersion: kowan/v1 files from a repository, shows what would change, and applies it on approval or automatically."
        actions={
          <>
            <Button variant="outline" iconLeft="download-rounded" onClick={() => setExporting(true)}>
              Export
            </Button>
            <Button iconLeft="add-rounded" onClick={() => { create.reset(); setCreating(true); }}>
              New sync
            </Button>
          </>
        }
      />

      {secret?.webhookSecret && (
        <WebhookSecretCallout sync={secret} onDismiss={() => setSecret(null)} />
      )}

      <SectionCard plain noBodyPadding>
        {syncs.isError ? (
          <Text color="danger" style={{ padding: 16 }}>{errorMessage(syncs.error, "Failed to load syncs")}</Text>
        ) : (
          <Table<GitOpsSync>
            data={syncs.data ?? []}
            columns={columns}
            rowKey={(s) => s.id}
            onRowClick={(s) => navigate(`/gitops/${s.id}`)}
            loading={syncs.isLoading}
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="data-object-rounded" size="xl" />}
                title="No syncs yet"
                description="Point a sync at a git repository with kowan/v1 resource files, or export what you already have to start one."
                action={<Button iconLeft="add-rounded" onClick={() => setCreating(true)}>New sync</Button>}
              />
            }
          />
        )}
      </SectionCard>

      <SyncFormDialog
        open={creating}
        title="New sync"
        initial={emptySyncInput()}
        submitting={create.isPending}
        error={create.isError ? errorMessage(create.error, "Could not create the sync") : null}
        onClose={() => setCreating(false)}
        onSubmit={(v) => create.mutate(v)}
      />
      <ExportDialog open={exporting} onClose={() => setExporting(false)} syncs={syncs.data ?? []} />
    </Box>
  );
}

function WebhookSecretCallout({ sync, onDismiss }: { sync: GitOpsSync; onDismiss: () => void }) {
  const url = `${window.location.origin}${sync.webhookUrl}`;
  return (
    <Callout color="info" title="Push webhook" onDismiss={onDismiss}>
      <Box direction="column" gap={2}>
        <Text size="sm">
          Add this webhook to the repository (GitHub, Gitea, GitLab) so pushes refresh the sync right away. The secret is shown only now.
        </Text>
        <Box direction="row" gap={2} align="center" wrap>
          <Text size="sm" mono className="break-all">{url}</Text>
          <CopyButton value={url} label="Copy URL" />
        </Box>
        <Box direction="row" gap={2} align="center" wrap>
          <Text size="sm" mono className="break-all">{sync.webhookSecret}</Text>
          <CopyButton value={sync.webhookSecret ?? ""} label="Copy secret" />
        </Box>
      </Box>
    </Callout>
  );
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function SyncDetail({ id }: { id: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const planQ = useQuery({ queryKey: ["gitops-plan", id], queryFn: () => getGitOpsPlan(id), refetchInterval: SLOW_REFETCH_MS });
  const runsQ = useQuery({ queryKey: ["gitops-runs", id], queryFn: () => listGitOpsRuns(id), refetchInterval: SLOW_REFETCH_MS });
  const managedQ = useQuery({ queryKey: ["gitops-managed", id], queryFn: () => listGitOpsManaged(id) });
  const [editing, setEditing] = useState(false);
  const [confirmApply, setConfirmApply] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [secret, setSecret] = useState<GitOpsSync | null>(null);
  const [runId, setRunId] = useState<string | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["gitops-plan", id] });
    qc.invalidateQueries({ queryKey: ["gitops-runs", id] });
    qc.invalidateQueries({ queryKey: ["gitops-managed", id] });
    qc.invalidateQueries({ queryKey: ["gitops-syncs"] });
  };

  const refresh = useMutation({
    mutationFn: () => refreshGitOpsSync(id),
    onSuccess: (s) => {
      invalidate();
      if (s.status === "error") notify({ color: "red", title: "Sync has problems", message: s.lastError || "See the plan" });
    },
    onError: (err) => notify({ color: "red", title: "Refresh failed", message: errorMessage(err, "Could not fetch the repository") }),
  });
  const apply = useMutation({
    mutationFn: (commit: string) => applyGitOpsSync(id, commit),
    onSuccess: ({ run }) => {
      setConfirmApply(false);
      invalidate();
      const failed = (run.result ?? []).filter((r) => r.status === "failed").length;
      notify({
        color: failed ? "warning" : "green",
        title: failed ? "Applied with failures" : "Applied",
        message: failed ? `${failed} resource(s) failed; see the run` : `Commit ${shortSha(run.commit)} is live`,
      });
    },
    onError: (err) => {
      setConfirmApply(false);
      invalidate();
      notify({ color: "red", title: "Apply failed", message: errorMessage(err, "Could not apply the plan") });
    },
  });
  const save = useMutation({
    mutationFn: (v: GitOpsSyncInput) => updateGitOpsSync(id, v),
    onSuccess: () => {
      setEditing(false);
      invalidate();
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteGitOpsSync(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["gitops-syncs"] });
      navigate("/gitops");
    },
    onError: (err) => notify({ color: "red", title: "Delete failed", message: errorMessage(err, "Could not delete") }),
  });
  const rotate = useMutation({
    mutationFn: () => rotateGitOpsWebhook(id),
    onSuccess: (s) => setSecret(s),
  });

  if (planQ.isLoading) return <Skeleton height={240} />;
  if (planQ.isError || !planQ.data) {
    return <Text color="danger">{errorMessage(planQ.error, "Sync not found")}</Text>;
  }
  const { sync, plan } = planQ.data;
  const canApply = Boolean(plan && plan.errors.length === 0 && sync.status === "drift");
  const s = plan?.summary;

  const runColumns: Column<GitOpsRun>[] = [
    { key: "startedAt", header: "When", accessor: (r) => r.startedAt, cell: (r) => <Text size="sm">{formatDate(r.startedAt)}</Text> },
    { key: "action", header: "Action", accessor: (r) => r.action, cell: (r) => <Text size="sm">{r.action} · <Text as="span" color="muted" size="sm">{r.trigger}</Text></Text> },
    { key: "status", header: "Result", accessor: (r) => r.status, cell: (r) => <RunStatus status={r.status} /> },
    { key: "commit", header: "Commit", accessor: (r) => r.commit, cell: (r) => <Text size="sm" mono>{shortSha(r.commit) || "—"}</Text> },
    {
      key: "summary",
      header: "Changes",
      accessor: (r) => r.summary?.create ?? 0,
      cell: (r) => (
        <Text size="sm" color="muted">
          {r.summary ? `+${r.summary.create} ~${r.summary.update} −${r.summary.delete}` : "—"}
        </Text>
      ),
    },
    { key: "error", header: "", accessor: (r) => r.error, truncate: true, cell: (r) => <Text size="xs" color={r.error ? "danger" : "muted"} truncate>{r.error}</Text> },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            {sync.name} <SyncStatusPill status={sync.status} />
          </span>
        }
        breadcrumbs={[{ label: "GitOps", to: "/gitops" }, { label: sync.name }]}
        description={
          <span className="font-mono text-[13px]">
            {sync.repoUrl} @ {sync.ref}
            {sync.path ? ` : ${sync.path}` : ""} · {sync.autoApply ? "auto-apply" : "review"} · {intervalText(sync.intervalSec)}
            {!sync.enabled ? " · disabled" : ""}
          </span>
        }
        actions={
          <>
            <Button variant="outline" iconLeft="edit-outline-rounded" onClick={() => { save.reset(); setEditing(true); }}>
              Edit
            </Button>
            <Button variant="outline" iconLeft="refresh-rounded" loading={refresh.isPending} onClick={() => refresh.mutate()}>
              Refresh
            </Button>
            <Tooltip content={canApply ? "Review and apply the plan" : plan?.errors.length ? "Fix the repository problems first" : "Nothing to apply"}>
              <Button iconLeft="play-arrow-rounded" disabled={!canApply} onClick={() => setConfirmApply(true)}>
                Apply
              </Button>
            </Tooltip>
          </>
        }
      />

      {sync.lastError && sync.status === "error" && (
        <Callout color="danger" title="Last check failed">
          <Text size="sm" className="break-words">{sync.lastError}</Text>
        </Callout>
      )}
      {(secret ?? null)?.webhookSecret && <WebhookSecretCallout sync={secret as GitOpsSync} onDismiss={() => setSecret(null)} />}

      <SectionCard
        title="Plan"
        description={plan ? `Computed ${formatDate(plan.generatedAt)} from the repository` : "Refresh to fetch the repository and compute a plan."}
      >
        {plan ? <PlanView plan={plan} /> : <Text size="sm" color="muted">No plan yet.</Text>}
      </SectionCard>

      <SectionCard title="Runs" description="Refreshes and applies, newest first." noBodyPadding>
        <Table<GitOpsRun>
          data={runsQ.data ?? []}
          columns={runColumns}
          rowKey={(r) => r.id}
          onRowClick={(r) => setRunId(r.id)}
          loading={runsQ.isLoading}
          pagination
          defaultPageSize={10}
          emptyText="No runs yet."
        />
      </SectionCard>

      <SectionCard title="Managed resources" description="Created or adopted by this sync; only these can be deleted by it." noBodyPadding>
        <Table
          data={managedQ.data ?? []}
          rowKey={(m) => `${m.kind}/${m.resourceId}`}
          loading={managedQ.isLoading}
          emptyText="Nothing managed yet."
          columns={[
            { key: "kind", header: "Kind", cell: (m) => <Text size="sm" mono>{m.kind}</Text> },
            { key: "name", header: "Name" },
            { key: "commit", header: "Commit", cell: (m) => <Text size="sm" mono>{shortSha(m.commit)}</Text> },
            { key: "updatedAt", header: "Applied", cell: (m) => <Text size="sm" color="muted">{formatDate(m.updatedAt)}</Text> },
          ]}
        />
      </SectionCard>

      <SectionCard
        title="Webhook"
        description="Pushes to the branch that touch the sync path refresh it (and apply when auto-apply is on)."
        actions={<Button size="sm" variant="outline" loading={rotate.isPending} onClick={() => rotate.mutate()}>Rotate secret</Button>}
      >
        <Box direction="row" gap={2} align="center" wrap>
          <Text size="sm" mono className="break-all">{window.location.origin}{sync.webhookUrl}</Text>
          <CopyButton value={`${window.location.origin}${sync.webhookUrl}`} label="Copy URL" />
        </Box>
        <Box direction="row">
          <Button size="sm" variant="clear" color="danger" iconLeft="delete-outline-rounded" onClick={() => setConfirmDelete(true)}>
            Delete sync
          </Button>
        </Box>
      </SectionCard>

      <SyncFormDialog
        open={editing}
        title="Edit sync"
        initial={syncToInput(sync)}
        submitting={save.isPending}
        error={save.isError ? errorMessage(save.error, "Could not save") : null}
        onClose={() => setEditing(false)}
        onSubmit={(v) => save.mutate(v)}
      />

      <Popup open={confirmApply} onOpenChange={(o) => { if (!o) setConfirmApply(false); }} size="sm">
        <Popup.Content>
          <Popup.Header><Popup.Title>Apply commit {shortSha(plan?.commit ?? "")}</Popup.Title></Popup.Header>
          <Popup.Body>
            <Text size="sm">
              {s ? `${s.create} to create, ${s.update} to update, ${s.delete} to delete${s.orphan ? `, ${s.orphan} to release` : ""}${s.adopt ? `, ${s.adopt} to adopt` : ""}.` : ""}
            </Text>
            {s && s.delete > 0 && (
              <Callout color="danger">Deleting a deployment tears down its containers on the host.</Callout>
            )}
          </Popup.Body>
          <Popup.Footer>
            <Button variant="outline" onClick={() => setConfirmApply(false)} disabled={apply.isPending}>Cancel</Button>
            <Button color={s && s.delete > 0 ? "danger" : undefined} loading={apply.isPending} onClick={() => apply.mutate(plan?.commit ?? "")}>
              Apply
            </Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>

      <Popup open={confirmDelete} onOpenChange={(o) => { if (!o) setConfirmDelete(false); }} size="sm">
        <Popup.Content>
          <Popup.Header><Popup.Title>Delete sync</Popup.Title></Popup.Header>
          <Popup.Body>
            <Text size="sm">
              Delete <b>{sync.name}</b>? The resources it manages are kept and become regular, unmanaged resources.
            </Text>
          </Popup.Body>
          <Popup.Footer>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>Cancel</Button>
            <Button color="danger" loading={remove.isPending} onClick={() => remove.mutate()}>Delete</Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>

      <RunDialog syncId={id} runId={runId} onClose={() => setRunId(null)} />
    </Box>
  );
}

function RunStatus({ status }: { status: string }) {
  const map: Record<string, { label: string; color: "green" | "amber" | "red" | "gray" }> = {
    applied: { label: "Applied", color: "green" },
    in_sync: { label: "In sync", color: "green" },
    partial: { label: "Partial", color: "amber" },
    drift: { label: "Drift", color: "amber" },
    failed: { label: "Failed", color: "red" },
    error: { label: "Error", color: "red" },
  };
  const m = map[status] ?? { label: status || "—", color: "gray" as const };
  return <StatePill status={status || "unknown"} label={m.label} color={m.color} size="sm" />;
}

function RunDialog({ syncId, runId, onClose }: { syncId: string; runId: string | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["gitops-run", syncId, runId], queryFn: () => getGitOpsRun(syncId, runId ?? ""), enabled: Boolean(runId) });
  const run = q.data;
  return (
    <Popup open={Boolean(runId)} onOpenChange={(o) => { if (!o) onClose(); }} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>
            {run ? `${run.action} · ${shortSha(run.commit)} · ${formatDate(run.startedAt)}` : "Run"}
          </Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {q.isLoading || !run ? (
            <Skeleton height={120} />
          ) : (
            <>
              <Box direction="row" gap={2} align="center">
                <RunStatus status={run.status} />
                <Text size="sm" color="muted">trigger: {run.trigger}</Text>
              </Box>
              {run.error && <Callout color="danger"><Text size="sm" className="break-words">{run.error}</Text></Callout>}
              {run.result && run.result.length > 0 && (
                <div className="border">
                  {run.result.map((r, i) => (
                    <div key={i} className="flex items-center gap-5 border-b px-5 py-[4px] text-[13px] last:border-b-0">
                      <span className="w-42 shrink-0"><ActionPill action={r.action} /></span>
                      <span className="w-54 shrink-0 font-mono text-[12px] text-muted-foreground">{r.kind}</span>
                      <span className="min-w-0 flex-1 truncate">{r.name}</span>
                      <RunStatus status={r.status === "ok" ? "applied" : r.status === "unchanged" ? "in_sync" : r.status} />
                      {r.error && <Text size="xs" color="danger" className="max-w-[40%] break-words">{r.error}</Text>}
                    </div>
                  ))}
                </div>
              )}
              {run.plan && <PlanView plan={run.plan} />}
            </>
          )}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}
