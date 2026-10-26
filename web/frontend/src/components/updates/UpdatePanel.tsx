import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Loader2, X } from "lucide-react";
import { Button, Callout, Checkbox, Code, Popup, Skeleton, StatePill, Table, TextInput, type Color, type Column } from "@/ui/kit";
import {
  getAgentUpdateStatus,
  getPlatformUpdate,
  getServerUpdateStatus,
  listAgentUpdates,
  listServerUpdates,
  rollbackAgent,
  rollbackServer,
  startAgentUpdate,
  startServerUpdate,
} from "@/api/client";
import type { ComponentUpdateStatus, PlatformUpdate, PlatformUpdateStatus } from "@/types";
import { errorMessage, timeAgo } from "@/utils";
import { notify } from "@/ui/toast";
import { cn } from "@/lib/utils";

/**
 * Update panel for the control plane or one agent: current vs latest version,
 * update / rollback with confirmation, a live phase timeline while it runs
 * (tolerating the server going away and coming back), the outcome with logs on
 * failure, and the update history. Contract: docs/updates-design.md.
 */

const PHASE_LABEL: Record<string, string> = {
  queued: "Queued",
  lock: "Reserve update slot",
  resolve: "Resolve image",
  pull: "Pull new image",
  preflight: "Smoke-test new image",
  snapshot: "Snapshot database",
  launch: "Start updater",
  wait: "Hand over to updater",
  inspect: "Read current container",
  stop: "Stop current version",
  park: "Keep current version aside",
  create: "Create new version",
  start: "Start new version",
  verify: "Health check",
  commit: "Finish",
  rollback: "Roll back",
  restore: "Restore database",
  reconcile: "Record result",
};

const RESULT: Record<PlatformUpdateStatus, { label: string; color: Color }> = {
  running: { label: "in progress", color: "info" },
  updated: { label: "updated", color: "success" },
  rolled_back: { label: "rolled back", color: "warning" },
  failed: { label: "failed", color: "danger" },
  aborted: { label: "aborted", color: "neutral" },
  unknown: { label: "unknown", color: "neutral" },
};

function short(d?: string) {
  if (!d) return "—";
  const h = d.includes(":") ? d.split(":")[1] : d;
  return h.slice(0, 12);
}
function versionOf(v?: string, digest?: string) {
  return v && v !== "dev" ? v : short(digest);
}
function duration(u: PlatformUpdate) {
  if (!u.finishedAt) return "—";
  const s = Math.max(0, Math.round((Date.parse(u.finishedAt) - Date.parse(u.startedAt)) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}

export function UpdateResultPill({ status }: { status: PlatformUpdateStatus }) {
  const r = RESULT[status] ?? RESULT.unknown;
  return <StatePill status={status} label={r.label} color={r.color} size="sm" />;
}

/** Vertical list of phases with done / failed / current markers. */
function PhaseTimeline({ update }: { update: PlatformUpdate }) {
  const running = update.status === "running";
  return (
    <ol className="m-0 flex list-none flex-col p-0">
      {update.phases.map((p, i) => {
        const last = i === update.phases.length - 1;
        const current = running && last && p.ok;
        return (
          <li key={`${p.phase}-${i}`} className="flex items-start gap-[8px] border-l py-[4px] pl-5 text-[13px]">
            <span className="mt-[2px] shrink-0">
              {current ? (
                <Loader2 className="size-[12px] animate-spin text-signal" />
              ) : p.ok ? (
                <Check className="size-[12px] text-[var(--tone-green-text)]" />
              ) : (
                <X className="size-[12px] text-[var(--tone-red-text)]" />
              )}
            </span>
            <span className="flex min-w-0 flex-1 flex-col">
              <span className={cn(!p.ok && "text-[var(--tone-red-text)]")}>{PHASE_LABEL[p.phase] ?? p.phase}</span>
              {p.detail && <span className="truncate text-[12px] text-muted-foreground" title={p.detail}>{p.detail}</span>}
            </span>
            <span className="shrink-0 text-[12px] text-muted-foreground">{p.at ? new Date(p.at).toLocaleTimeString() : ""}</span>
          </li>
        );
      })}
      {running && update.phases.length === 0 && (
        <li className="flex items-center gap-[8px] border-l py-[4px] pl-5 text-[13px] text-muted-foreground">
          <Loader2 className="size-[12px] animate-spin" /> Starting…
        </li>
      )}
    </ol>
  );
}

function UpdateDetail({ update }: { update: PlatformUpdate }) {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-[110px_1fr] gap-x-6 gap-y-[4px] text-[13px]">
        <span className="text-muted-foreground">Result</span>
        <span><UpdateResultPill status={update.status} /></span>
        <span className="text-muted-foreground">From</span>
        <span className="truncate font-mono">{update.fromImage} · {versionOf(update.fromVersion, update.fromDigest)}</span>
        <span className="text-muted-foreground">To</span>
        <span className="truncate font-mono">{update.toImage} · {versionOf(update.toVersion, update.toDigest)}</span>
        <span className="text-muted-foreground">Started</span>
        <span>{new Date(update.startedAt).toLocaleString()}{update.startedBy ? ` by ${update.startedBy}` : ""}</span>
        <span className="text-muted-foreground">Duration</span>
        <span>{duration(update)}</span>
      </div>
      {update.error && (
        <Callout
          color={update.status === "rolled_back" || update.status === "aborted" ? "warning" : "danger"}
          title={
            update.status === "aborted"
              ? "Stopped before changing anything"
              : update.status === "rolled_back"
                ? update.mode === "rollback"
                  ? "Rollback failed: the current version was kept"
                  : "Rolled back to the previous version"
                : "Failed: manual recovery may be needed"
          }
        >
          {update.error}
        </Callout>
      )}
      <PhaseTimeline update={update} />
      {update.logs.length > 0 && (
        <div className="flex flex-col gap-[4px]">
          <span className="text-[12px] font-medium tracking-[0.06em] text-muted-foreground font-mono uppercase">Logs of the failed version</span>
          <Code block className="max-h-96">{update.logs.join("\n")}</Code>
        </div>
      )}
    </div>
  );
}

export default function UpdatePanel({ component, hostId, isAdmin }: { component: "server" | "agent"; hostId?: string; isAdmin: boolean }) {
  const qc = useQueryClient();
  const isServer = component === "server";
  const statusKey = isServer ? ["server-update"] : ["agent-update", hostId];
  const historyKey = isServer ? ["server-updates"] : ["agent-updates", hostId];

  const status = useQuery({
    queryKey: statusKey,
    queryFn: () => (isServer ? getServerUpdateStatus() : getAgentUpdateStatus(hostId!)),
    refetchInterval: 60_000,
  });
  const history = useQuery({
    queryKey: historyKey,
    queryFn: () => (isServer ? listServerUpdates(20) : listAgentUpdates(hostId!, 20)),
    enabled: isAdmin,
  });

  const [activeId, setActiveId] = useState<string | null>(null);
  const inProgressId = status.data?.inProgress?.id ?? null;
  const watchId = activeId ?? inProgressId;

  // Poll the running update. Errors are expected while the server restarts:
  // keep polling and show "reconnecting" instead of failing.
  const live = useQuery({
    queryKey: ["platform-update", watchId],
    queryFn: () => getPlatformUpdate(watchId!),
    enabled: !!watchId,
    retry: false,
    refetchInterval: (q) => (q.state.data && q.state.data.status !== "running" ? false : 1500),
  });
  const reconnecting = !!watchId && live.isError;
  const current = live.data;

  useEffect(() => {
    if (!current || current.status === "running") return;
    const r = RESULT[current.status];
    notify({
      color: current.status === "updated" ? "green" : current.status === "rolled_back" ? "yellow" : "red",
      title: isServer ? `Server ${r.label}` : `Agent ${r.label}`,
      message: current.error || (current.status === "updated" ? `Now running ${versionOf(current.toVersion, current.toDigest)}.` : undefined),
    });
    void qc.invalidateQueries({ queryKey: statusKey });
    void qc.invalidateQueries({ queryKey: historyKey });
    void qc.invalidateQueries({ queryKey: ["hosts"] });
    setActiveId(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.status, current?.id]);

  const [confirm, setConfirm] = useState<null | "update" | "rollback">(null);
  const [pin, setPin] = useState("");
  const [restoreDb, setRestoreDb] = useState(true);
  const [detail, setDetail] = useState<PlatformUpdate | null>(null);

  const start = useMutation({
    mutationFn: () => (isServer ? startServerUpdate(pin.trim() || undefined) : startAgentUpdate(hostId!, pin.trim() || undefined)),
    onSuccess: (r) => {
      setActiveId(r.id);
      setConfirm(null);
      setPin("");
    },
    onError: (e) => notify({ color: "red", title: "Could not start the update", message: errorMessage(e, "The update did not start; nothing was changed.") }),
  });
  const rollback = useMutation({
    mutationFn: () => (isServer ? rollbackServer(restoreDb) : rollbackAgent(hostId!)),
    onSuccess: (r) => {
      setActiveId(r.id);
      setConfirm(null);
    },
    onError: (e) => notify({ color: "red", title: "Could not start the rollback", message: errorMessage(e, "Nothing was changed.") }),
  });

  const s: ComponentUpdateStatus | undefined = status.data;
  const busy = !!watchId && (!current || current.status === "running");
  const name = isServer ? "server" : "agent";

  const columns: Column<PlatformUpdate>[] = useMemo(
    () => [
      { key: "startedAt", header: "Started", sortable: true, accessor: (u) => u.startedAt, cell: (u) => <span title={new Date(u.startedAt).toLocaleString()}>{timeAgo(u.startedAt)}</span> },
      { key: "mode", header: "Action", accessor: (u) => u.mode, cell: (u) => (u.mode === "rollback" ? "Rollback" : "Update") },
      { key: "status", header: "Result", accessor: (u) => u.status, cell: (u) => <UpdateResultPill status={u.status} /> },
      { key: "change", header: "Change", grow: true, accessor: (u) => u.toVersion, cell: (u) => <span className="font-mono text-muted-foreground"><span className="text-foreground">{versionOf(u.fromVersion, u.fromDigest)}</span> → <span className="text-foreground">{versionOf(u.toVersion, u.toDigest)}</span></span> },
      { key: "duration", header: "Duration", align: "right", accessor: (u) => u.finishedAt, cell: (u) => duration(u) },
      { key: "by", header: "By", accessor: (u) => u.startedBy, cell: (u) => <span className="text-muted-foreground">{u.startedBy || "—"}</span> },
    ],
    [],
  );

  if (status.isLoading) return <div className="flex flex-col gap-3"><Skeleton height={14} width="40%" /><Skeleton height={14} width="60%" /></div>;
  if (status.isError) return <Callout color="danger" title="Could not read the update status">{errorMessage(status.error, "Try again in a moment.")}</Callout>;
  if (!s?.supported) {
    return (
      <Callout color="gray" title={`Self-update is not available for this ${name}`}>
        {s?.reason || s?.error || `The ${name} must run as a container with the Docker socket mounted.`}
      </Callout>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-x-12 gap-y-5 sm:grid-cols-[repeat(3,minmax(0,max-content))_1fr]">
        <div className="flex flex-col gap-[2px]">
          <span className="text-[12px] tracking-[0.06em] text-muted-foreground font-mono uppercase">Running</span>
          <span className="font-mono text-[17px] font-semibold">{versionOf(s.version, s.running)}</span>
          <span className="truncate font-mono text-[12px] text-muted-foreground" title={s.image}>{s.image}</span>
        </div>
        <div className="flex flex-col gap-[2px]">
          <span className="text-[12px] tracking-[0.06em] text-muted-foreground font-mono uppercase">Registry</span>
          <span className="font-mono text-[17px] font-semibold">{s.latest ? short(s.latest) : "—"}</span>
          <span className="text-[12px] text-muted-foreground">{s.error ? s.error : s.updateAvailable ? "newer image available" : "same as running"}</span>
        </div>
        <div className="flex flex-col gap-[2px]">
          <span className="text-[12px] tracking-[0.06em] text-muted-foreground font-mono uppercase">Previous</span>
          <span className="font-mono text-[17px] font-semibold">{s.previous ? versionOf(s.previous.version, s.previous.digest) : "—"}</span>
          <span className="text-[12px] text-muted-foreground">{s.canRollback ? "kept for rollback" : "none kept yet"}</span>
        </div>
        {isAdmin && (
          <div className="flex flex-wrap items-start justify-end gap-3">
            <Button variant="outline" size="sm" iconLeft="refresh-rounded" disabled={busy} loading={status.isFetching} onClick={() => void status.refetch()}>Check</Button>
            {s.canRollback && <Button variant="outline" size="sm" iconLeft="history-rounded" disabled={busy} onClick={() => setConfirm("rollback")}>Roll back</Button>}
            <Button size="sm" iconLeft="upgrade-rounded" disabled={busy} onClick={() => setConfirm("update")}>{s.updateAvailable ? "Update now" : "Update"}</Button>
          </div>
        )}
      </div>

      {busy && (
        <div className="flex flex-col gap-3 border bg-sunken/50 p-5">
          <div className="flex items-center gap-3 text-[13px]">
            <Loader2 className="size-[12px] animate-spin text-signal" />
            <span className="font-medium">{current?.mode === "rollback" ? "Rolling back" : "Updating"} the {name}</span>
            {reconnecting && <span className="text-muted-foreground">· {isServer ? "server is restarting, reconnecting…" : "waiting for the agent…"}</span>}
          </div>
          {current && <PhaseTimeline update={current} />}
        </div>
      )}

      {!busy && s.last && s.last.status !== "updated" && (
        <Callout color={s.last.status === "rolled_back" ? "warning" : s.last.status === "aborted" ? "gray" : "danger"} title={`Last ${s.last.mode} ${RESULT[s.last.status]?.label ?? s.last.status} ${timeAgo(s.last.finishedAt || s.last.startedAt)}`}>
          {s.last.error || "See the history for details."}{" "}
          <button type="button" className="cell-link" onClick={() => setDetail(s.last!)}>Details</button>
        </Callout>
      )}

      {isAdmin && (history.data?.length ?? 0) > 0 && (
        <Table<PlatformUpdate> columns={columns} data={history.data} rowKey={(u) => u.id} onRowClick={(u) => setDetail(u)} defaultSort={[{ key: "startedAt", dir: "desc" }]} />
      )}

      <Popup open={confirm === "update"} onOpenChange={(o) => !o && setConfirm(null)} size="sm">
        <Popup.Content>
          <Popup.Header><Popup.Title>Update the {name}</Popup.Title><Popup.Close /></Popup.Header>
          <Popup.Body>
            <p className="m-0 text-[13px]">
              Kowan pulls the new image, smoke-tests it, then swaps the container. The current version is kept aside{isServer ? " and the database is snapshotted" : ""}: if the new one does not pass its health check, everything is restored automatically.
            </p>
            {isServer && <Callout color="info">The console is unavailable for a few seconds while the server restarts. This page reconnects on its own.</Callout>}
            <TextInput label="Image (optional)" description={`Leave empty to update ${s.image} in place, or pin a version such as ${s.image.split(":")[0]}:1.2.0.`} value={pin} onChange={(e) => setPin(e.target.value)} placeholder={s.image} />
          </Popup.Body>
          <Popup.Footer>
            <Button variant="clear" onClick={() => setConfirm(null)}>Cancel</Button>
            <Button loading={start.isPending} onClick={() => start.mutate()}>Start update</Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>

      <Popup open={confirm === "rollback"} onOpenChange={(o) => !o && setConfirm(null)} size="sm">
        <Popup.Content>
          <Popup.Header><Popup.Title>Roll back the {name}</Popup.Title><Popup.Close /></Popup.Header>
          <Popup.Body>
            <p className="m-0 text-[13px]">
              Switch back to <strong className="font-mono">{s.previous ? versionOf(s.previous.version, s.previous.digest) : "the previous version"}</strong> with its original configuration. If the previous version does not come up healthy, the current one is restored.
            </p>
            {isServer && (
              <Checkbox
                checked={restoreDb}
                onChange={(e) => setRestoreDb(e.target.checked)}
                label="Restore the database as it was before the last update"
                description="Recommended: the previous version may not understand changes made by the newer one. Data written since that update is moved to backups/rolled-back-*, not deleted."
              />
            )}
          </Popup.Body>
          <Popup.Footer>
            <Button variant="clear" onClick={() => setConfirm(null)}>Cancel</Button>
            <Button color="warning" loading={rollback.isPending} onClick={() => rollback.mutate()}>Roll back</Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>

      <Popup open={!!detail} onOpenChange={(o) => !o && setDetail(null)} size="lg">
        <Popup.Content>
          <Popup.Header><Popup.Title>{detail?.mode === "rollback" ? "Rollback" : "Update"} · {detail ? new Date(detail.startedAt).toLocaleString() : ""}</Popup.Title><Popup.Close /></Popup.Header>
          <Popup.Body>{detail && <UpdateDetail update={detail} />}</Popup.Body>
        </Popup.Content>
      </Popup>
    </div>
  );
}
