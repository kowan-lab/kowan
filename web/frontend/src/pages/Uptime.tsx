// Uptime: HTTP and TCP checks the server runs on a schedule, with response
// times, incidents and alerts, plus the settings of the public status page.

import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Box,
  Button,
  Callout,
  Icon,
  InputToggle,
  LineChart,
  NumberInput,
  Popup,
  Select,
  Skeleton,
  StatePill,
  Table,
  Tabs,
  Text,
  TextArea,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import {
  createUptimeCheck,
  deleteUptimeCheck,
  getStatusPage,
  listChannels,
  listDeployments,
  listUptimeChecks,
  listUptimeIncidents,
  putStatusPage,
  runUptimeCheck,
  updateUptimeCheck,
  uptimeResults,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { LIVE_REFETCH_MS } from "../lib/live";
import type { StatusPageSettings, UptimeCheck, UptimeCheckInput, UptimeResult, UptimeStatus } from "../types";
import { notify } from "../ui/toast";
import { errorMessage, formatDate, timeAgo } from "../utils";

const STATUS: Record<UptimeStatus, { label: string; color: "green" | "red" | "gray" | "amber" }> = {
  up: { label: "Up", color: "green" },
  down: { label: "Down", color: "red" },
  unknown: { label: "Pending", color: "gray" },
  paused: { label: "Paused", color: "gray" },
};

const INTERVALS = [
  { value: "30", label: "Every 30 seconds" },
  { value: "60", label: "Every minute" },
  { value: "300", label: "Every 5 minutes" },
  { value: "900", label: "Every 15 minutes" },
  { value: "3600", label: "Every hour" },
];

export function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 99.995 ? "100%" : `${v.toFixed(2)}%`;
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
}

/** Days until an RFC3339 date (negative once past). */
export function daysLeft(iso: string): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : Math.floor((t - Date.now()) / 86_400_000);
}

/** One square per recent probe, oldest first: green ok, red failed. */
export function Beats({ results, label }: { results: UptimeResult[]; label: string }) {
  if (results.length === 0) return <Text size="xs" color="muted">no probes yet</Text>;
  const failed = results.filter((r) => !r.ok).length;
  return (
    <span role="img" aria-label={`${label}: ${results.length - failed} of ${results.length} recent probes passed`} className="inline-flex items-end gap-[2px]">
      {results.map((r, i) => (
        <span
          key={i}
          title={`${formatDate(r.at)} · ${r.ok ? ms(r.latencyMs) : r.error || "failed"}`}
          className="h-[14px] w-[4px]"
          style={{ background: r.ok ? "var(--tone-green-solid)" : "var(--tone-red-solid)" }}
        />
      ))}
    </span>
  );
}

const EMPTY: UptimeCheckInput = {
  name: "", kind: "http", target: "", method: "GET", expectStatus: "200-399", keyword: "", verifyTls: true,
  intervalSec: 60, timeoutSec: 10, failThreshold: 2, deploymentId: "", channelId: "", level: "critical",
  enabled: true, public: false,
};

function CheckForm({ initial, onClose, onSaved }: { initial: UptimeCheck | null; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState<UptimeCheckInput>(() => (initial ? { ...EMPTY, ...initial } : EMPTY));
  const [error, setError] = useState<string | null>(null);
  const set = (p: Partial<UptimeCheckInput>) => setF((v) => ({ ...v, ...p }));
  const channels = useQuery({ queryKey: ["channels"], queryFn: listChannels, retry: false });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const save = useMutation({
    mutationFn: () => {
      const body = { ...f, name: f.name.trim(), target: f.target.trim() };
      return initial ? updateUptimeCheck(initial.id, body) : createUptimeCheck(body);
    },
    onSuccess: (c) => {
      notify({ color: "green", title: initial ? "Check saved" : "Check created", message: `${c.name} is checked ${INTERVALS.find((i) => Number(i.value) === c.intervalSec)?.label.toLowerCase() ?? `every ${c.intervalSec}s`}.` });
      onSaved();
      onClose();
    },
    onError: (err) => setError(errorMessage(err, "Could not save the check")),
  });
  const submit = () => {
    if (!f.name.trim()) return setError("Name is required");
    if (!f.target.trim()) return setError(f.kind === "http" ? "URL is required" : "Host and port are required");
    setError(null);
    save.mutate();
  };
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{initial ? `Edit ${initial.name}` : "New uptime check"}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <Tabs variant="segmented" value={f.kind} onValueChange={(v) => set({ kind: v as "http" | "tcp", target: "" })}>
            <Tabs.List>
              <Tabs.Trigger value="http">HTTP(S)</Tabs.Trigger>
              <Tabs.Trigger value="tcp">TCP port</Tabs.Trigger>
            </Tabs.List>
          </Tabs>
          <TextInput label="Name" value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Shop website" />
          <TextInput
            label={f.kind === "http" ? "URL" : "Host and port"}
            value={f.target}
            onChange={(e) => set({ target: e.target.value })}
            placeholder={f.kind === "http" ? "https://shop.example.com/health" : "db.internal:5432"}
            description="Checked from the Kowan server."
          />
          {f.kind === "http" && (
            <div className="grid gap-4 sm:grid-cols-3">
              <Select label="Method" value={f.method} options={[{ value: "GET", label: "GET" }, { value: "HEAD", label: "HEAD" }]} onChange={(v) => set({ method: v as "GET" | "HEAD" })} />
              <TextInput label="Expected status" value={f.expectStatus} onChange={(e) => set({ expectStatus: e.target.value })} placeholder="200-399" />
              <TextInput label="Page contains" value={f.keyword} onChange={(e) => set({ keyword: e.target.value })} placeholder="optional text" />
            </div>
          )}
          <div className="grid gap-4 sm:grid-cols-3">
            <Select label="Interval" value={String(f.intervalSec)} options={INTERVALS.some((i) => Number(i.value) === f.intervalSec) ? INTERVALS : [...INTERVALS, { value: String(f.intervalSec), label: `Every ${f.intervalSec}s` }]} onChange={(v) => set({ intervalSec: Number(v) })} />
            <NumberInput label="Timeout (s)" min={1} max={60} value={f.timeoutSec} onChange={(v) => set({ timeoutSec: Number(v) || 10 })} />
            <NumberInput label="Down after failures" min={1} max={10} value={f.failThreshold} onChange={(v) => set({ failThreshold: Number(v) || 1 })} />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Select
              label="Notify"
              value={f.channelId}
              options={[{ value: "", label: "Every enabled channel" }, ...(channels.data ?? []).map((c) => ({ value: c.id, label: c.name }))]}
              onChange={(v) => set({ channelId: v ?? "" })}
            />
            <Select
              label="Alert level"
              value={f.level}
              options={[{ value: "critical", label: "Critical" }, { value: "warning", label: "Warning" }, { value: "info", label: "Info" }]}
              onChange={(v) => set({ level: v as UptimeCheckInput["level"] })}
            />
          </div>
          <Select
            label="Workload"
            description="Optional: show this check on the workload's page."
            value={f.deploymentId}
            searchable
            options={[{ value: "", label: "None" }, ...(deployments.data ?? []).map((d) => ({ value: d.id, label: d.name }))]}
            onChange={(v) => set({ deploymentId: v ?? "" })}
          />
          <div className="flex flex-wrap gap-6">
            {f.kind === "http" && <InputToggle label="Verify TLS certificate" checked={f.verifyTls} onChange={(e) => set({ verifyTls: e.target.checked })} />}
            <InputToggle label="Show on the status page" checked={f.public} onChange={(e) => set({ public: e.target.checked })} />
            <InputToggle label="Enabled" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          </div>
          {error && <Callout color="danger">{error}</Callout>}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button loading={save.isPending} onClick={submit}>{initial ? "Save" : "Create check"}</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

function CheckDetail({ check, onClose }: { check: UptimeCheck; onClose: () => void }) {
  const [hours, setHours] = useState("24");
  const results = useQuery({ queryKey: ["uptime-results", check.id, hours], queryFn: () => uptimeResults(check.id, Number(hours)), refetchInterval: LIVE_REFETCH_MS });
  const incidents = useQuery({ queryKey: ["uptime-incidents", check.id], queryFn: () => listUptimeIncidents(check.id) });
  const data = results.data ?? [];
  const cert = daysLeft(check.certExpiresAt);
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{check.name}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <div className="flex flex-wrap items-center gap-6 text-[13px]">
            <StatePill status={check.status} label={STATUS[check.status].label} color={STATUS[check.status].color} />
            <span className="font-mono text-muted-foreground">{check.target}</span>
            <span>24h {pct(check.uptime24h)}</span>
            <span>7d {pct(check.uptime7d)}</span>
            <span>30d {pct(check.uptime30d)}</span>
            {cert !== null && <span className={cert < 14 ? "text-[var(--tone-red-text)]" : ""}>certificate {cert < 0 ? "expired" : `${cert} days left`}</span>}
          </div>
          <Box direction="row" align="center" justify="between">
            <Text size="sm" weight="semibold">Response time</Text>
            <Select aria-label="Period" size="sm" width={140} value={hours}
              options={[{ value: "1", label: "Last hour" }, { value: "24", label: "Last 24 hours" }, { value: "168", label: "Last 7 days" }]}
              onChange={(v) => setHours(v ?? "24")} />
          </Box>
          {results.isLoading ? (
            <Skeleton height={180} />
          ) : data.length < 2 ? (
            <Text size="sm" color="muted">Not enough probes in this period yet.</Text>
          ) : (
            <LineChart
              height={180}
              unit=" ms"
              labels={data.map((r) => new Date(r.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))}
              series={[{ name: "Response time", data: data.map((r) => (r.ok ? r.latencyMs : 0)), color: "primary" }]}
            />
          )}
          <Text size="sm" weight="semibold">Incidents</Text>
          {(incidents.data ?? []).length === 0 ? (
            <Text size="sm" color="muted">No outages recorded.</Text>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px]">
              {(incidents.data ?? []).map((i) => (
                <li key={i.id} className="flex flex-wrap gap-x-4">
                  <span className={i.endedAt ? "" : "font-semibold text-[var(--tone-red-text)]"}>{i.endedAt ? "Resolved" : "Ongoing"}</span>
                  <span>{formatDate(i.startedAt)}</span>
                  <span className="text-muted-foreground">{i.endedAt ? `lasted ${duration(i.startedAt, i.endedAt)}` : `for ${duration(i.startedAt, new Date().toISOString())}`}</span>
                  {i.error && <span className="font-mono text-muted-foreground">{i.error}</span>}
                </li>
              ))}
            </ul>
          )}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}

export function duration(from: string, to: string): string {
  const s = Math.max(0, Math.round((Date.parse(to) - Date.parse(from)) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

function StatusPageCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["status-page"], queryFn: getStatusPage });
  const [draft, setDraft] = useState<StatusPageSettings | null>(null);
  const value = draft ?? q.data ?? { enabled: false, title: "", description: "" };
  const save = useMutation({
    mutationFn: (v: StatusPageSettings) => putStatusPage(v),
    onSuccess: (v) => {
      qc.setQueryData(["status-page"], v);
      setDraft(null);
      notify({ color: "green", title: "Status page saved", message: v.enabled ? "It is public at /status." : "It is turned off." });
    },
    onError: (err) => notify({ color: "red", title: "Not saved", message: errorMessage(err, "") }),
  });
  if (q.isLoading) return <Skeleton height={120} />;
  const url = `${window.location.origin}/status`;
  return (
    <SectionCard
      title="Status page"
      description="A public page with the checks marked “Show on the status page”: their state, 30 days of uptime and recent outages. Targets and error details stay private."
      actions={value.enabled && !draft ? <Link to="/status" target="_blank" className="text-[13px] text-[var(--link)]">Open {url}</Link> : undefined}
    >
      <div className="grid max-w-[640px] gap-4">
        <InputToggle label="Public status page" checked={value.enabled} onChange={(e) => setDraft({ ...value, enabled: e.target.checked })} />
        <TextInput label="Title" value={value.title} placeholder="Service status" onChange={(e) => setDraft({ ...value, title: e.target.value })} />
        <TextArea label="Description" rows={2} value={value.description} onChange={(e) => setDraft({ ...value, description: e.target.value })} />
        <Box direction="row" justify="end">
          <Button disabled={!draft} loading={save.isPending} onClick={() => save.mutate(value)}>Save</Button>
        </Box>
      </div>
    </SectionCard>
  );
}

export default function Uptime() {
  const { user } = useAuth();
  const canOperate = user?.role === "admin" || user?.role === "operator";
  const isAdmin = user?.role === "admin";
  const qc = useQueryClient();
  const checks = useQuery({ queryKey: ["uptime-checks"], queryFn: listUptimeChecks, refetchInterval: LIVE_REFETCH_MS });
  const [editing, setEditing] = useState<UptimeCheck | "new" | null>(null);
  const [detail, setDetail] = useState<UptimeCheck | null>(null);
  const [removing, setRemoving] = useState<UptimeCheck | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["uptime-checks"] });

  const run = useMutation({
    mutationFn: (c: UptimeCheck) => runUptimeCheck(c.id),
    onSuccess: (r, c) => {
      notify({ color: r.ok ? "green" : "red", title: r.ok ? `${c.name} is up` : `${c.name} failed`, message: r.ok ? `Answered in ${ms(r.latencyMs)}.` : r.error });
      refresh();
    },
    onError: (err) => notify({ color: "red", title: "Check failed", message: errorMessage(err, "") }),
  });
  const remove = useMutation({
    mutationFn: (c: UptimeCheck) => deleteUptimeCheck(c.id),
    onSuccess: () => {
      setRemoving(null);
      refresh();
    },
    onError: (err) => notify({ color: "red", title: "Delete failed", message: errorMessage(err, "") }),
  });

  const list = useMemo(() => checks.data ?? [], [checks.data]);
  const down = list.filter((c) => c.status === "down").length;
  const columns: Column<UptimeCheck>[] = [
    {
      key: "status", header: "Status", sortable: true, accessor: (c) => c.status,
      cell: (c) => (
        <Box direction="column" gap={0}>
          <StatePill status={c.status} label={STATUS[c.status].label} color={STATUS[c.status].color} size="sm" />
          <Tooltip content={c.lastError || (c.lastCheckedAt ? formatDate(c.lastCheckedAt) : "not yet")}>
            <span tabIndex={0}><Text size="xs" color={c.lastError ? "danger" : "muted"}>{c.lastCheckedAt ? timeAgo(c.lastCheckedAt) : "not checked yet"}</Text></span>
          </Tooltip>
        </Box>
      ),
    },
    {
      key: "name", header: "Check", sortable: true, grow: true, accessor: (c) => c.name,
      cell: (c) => (
        <Box direction="column" gap={0} style={{ minWidth: 0 }}>
          <button type="button" className="cell-link text-left" onClick={() => setDetail(c)}>{c.name}</button>
          <Text size="xs" color="muted" mono truncate>{c.kind === "tcp" ? `tcp ${c.target}` : c.target}</Text>
        </Box>
      ),
    },
    { key: "recent", header: "Recent", cell: (c) => <Beats results={c.recent} label={c.name} /> },
    { key: "uptime24h", header: "24h", align: "right", sortable: true, accessor: (c) => c.uptime24h ?? -1, cell: (c) => <Text size="sm" mono>{pct(c.uptime24h)}</Text> },
    { key: "uptime30d", header: "30d", align: "right", sortable: true, accessor: (c) => c.uptime30d ?? -1, cell: (c) => <Text size="sm" mono>{pct(c.uptime30d)}</Text> },
    { key: "latency", header: "Response", align: "right", sortable: true, accessor: (c) => c.latency24h ?? 0, cell: (c) => <Text size="sm" mono>{ms(c.latency24h)}</Text> },
    {
      key: "cert", header: "Certificate",
      cell: (c) => {
        const d = daysLeft(c.certExpiresAt);
        if (d === null) return <Text size="xs" color="muted">—</Text>;
        return <Badge size="sm" variant="outline" color={d < 7 ? "red" : d < 21 ? "amber" : "gray"}>{d < 0 ? "expired" : `${d} days`}</Badge>;
      },
    },
    ...(canOperate
      ? [{
          key: "actions", header: "", align: "right" as const,
          cell: (c: UptimeCheck) => (
            <Box direction="row" gap={1} justify="end" wrap="nowrap">
              <Button size="xs" variant="outline" color="neutral" loading={run.isPending && run.variables?.id === c.id} onClick={() => run.mutate(c)}>Check now</Button>
              <Button size="sm" variant="clear" aria-label={`Edit ${c.name}`} iconLeft="edit-outline-rounded" onClick={() => setEditing(c)} />
              <Button size="sm" variant="clear" color="danger" aria-label={`Delete ${c.name}`} iconLeft="delete-outline-rounded" onClick={() => setRemoving(c)} />
            </Box>
          ),
        }]
      : []),
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Uptime"
        description="HTTP and TCP checks the server runs on a schedule. A check that fails in a row raises an alert on your notification channels, and another one when it recovers."
        actions={canOperate && <Button iconLeft="add-rounded" onClick={() => setEditing("new")}>New check</Button>}
      />
      {down > 0 && <Callout color="danger" title={`${down} check${down === 1 ? " is" : "s are"} down`}>Open a check to see since when and what it answered.</Callout>}
      <SectionCard plain noBodyPadding>
        {checks.isError ? (
          <Callout color="danger" title="Could not load the checks">{errorMessage(checks.error, "")}</Callout>
        ) : (
          <Table<UptimeCheck>
            data={list}
            columns={columns}
            rowKey={(c) => c.id}
            loading={checks.isLoading}
            globalSearch
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="uptime-rounded" size="xl" />}
                title="No checks yet"
                description="Watch a website, an API endpoint or a database port: Kowan probes it on a schedule and alerts you when it stops answering."
                action={canOperate ? <Button iconLeft="add-rounded" onClick={() => setEditing("new")}>New check</Button> : undefined}
              />
            }
          />
        )}
      </SectionCard>
      {isAdmin && <StatusPageCard />}
      {editing && <CheckForm initial={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={refresh} />}
      {detail && <CheckDetail check={detail} onClose={() => setDetail(null)} />}
      <ConfirmModal
        opened={removing !== null}
        onClose={() => setRemoving(null)}
        title="Delete check"
        confirmLabel="Delete"
        loading={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
        message={<>Delete <b>{removing?.name}</b> and its history? An open alert for it is resolved.</>}
      />
    </Box>
  );
}
