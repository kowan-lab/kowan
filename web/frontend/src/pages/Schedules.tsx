import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Box, Button, Icon, Table, Text, Tooltip, type Column } from "@/ui/kit";
import { Link } from "react-router";
import { listHosts } from "../api/client";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import ScheduleEditor from "../components/schedules/ScheduleEditor";
import {
  deleteSchedule, inZone, listScheduleRuns, listSchedules, rescanScheduleLabels, runSchedule, untilText, updateSchedule,
  type Schedule, type ScheduleRun,
} from "../components/schedules/api";
import { LIVE_REFETCH_MS, SLOW_REFETCH_MS } from "../lib/live";
import { notify } from "../ui/toast";
import { errorMessage, formatDate, timeAgo } from "../utils";

function targetLink(s: { targetType: string; targetId: string; hostId: string }) {
  if (s.targetType === "deployment") return `/workloads/dep~${s.targetId}`;
  if (s.targetType === "container") return `/containers/${encodeURIComponent(s.targetId)}?host=${encodeURIComponent(s.hostId)}`;
  return "";
}

export default function Schedules() {
  const qc = useQueryClient();
  const schedules = useQuery({ queryKey: ["schedules", "all"], queryFn: () => listSchedules(), refetchInterval: SLOW_REFETCH_MS });
  const runs = useQuery({ queryKey: ["schedules", "runs"], queryFn: () => listScheduleRuns("", 200), refetchInterval: LIVE_REFETCH_MS });
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hostName = useMemo(() => new Map((hosts.data ?? []).map((h) => [h.id, h.name])), [hosts.data]);
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [creating, setCreating] = useState(false);
  const [del, setDel] = useState<Schedule | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["schedules"] });

  const run = useMutation({
    mutationFn: (s: Schedule) => runSchedule(s.id),
    onSuccess: (r) => { notify({ color: r.status === "failed" ? "red" : "green", title: `Run ${r.status}`, message: r.message }); refresh(); },
    onError: (e) => notify({ color: "red", title: "Run failed", message: errorMessage(e, "Could not run") }),
  });
  const toggle = useMutation({
    mutationFn: (s: Schedule) => updateSchedule(s.id, { enabled: !s.enabled }),
    onSuccess: refresh,
    onError: (e) => notify({ color: "red", title: "Update failed", message: errorMessage(e, "Could not update") }),
  });
  const remove = useMutation({
    mutationFn: (s: Schedule) => deleteSchedule(s.id),
    onSuccess: () => { setDel(null); refresh(); },
    onError: (e) => notify({ color: "red", title: "Delete failed", message: errorMessage(e, "Could not delete") }),
  });
  const rescan = useMutation({ mutationFn: rescanScheduleLabels, onSuccess: refresh });

  const cols: Column<Schedule>[] = [
    {
      key: "name", header: "Schedule", sortable: true, accessor: (s) => s.name,
      cell: (s) => (
        <Box direction="column">
          <Box direction="row" gap={1} align="center">
            <Text size="sm" weight="semibold">{s.name}</Text>
            {s.source === "label" && <Badge size="sm" variant="outline" color="gray" title="Derived from kowan.schedule.* container labels">label</Badge>}
          </Box>
          {s.error && <Text size="xs" color="danger">{s.error}</Text>}
        </Box>
      ),
    },
    {
      key: "target", header: "Target", sortable: true, accessor: (s) => `${s.targetType} ${s.targetId}`,
      cell: (s) => {
        const to = targetLink(s);
        const label = `${s.targetType} · ${s.targetId}`;
        return (
          <Box direction="column">
            {to ? <Link to={to}><Text size="sm">{label}</Text></Link> : <Text size="sm">{label}</Text>}
            {s.hostId && <Text size="xs" color="muted">{hostName.get(s.hostId) ?? s.hostId}</Text>}
          </Box>
        );
      },
    },
    { key: "action", header: "Action", sortable: true, cell: (s) => <Text size="sm" mono>{s.action}</Text> },
    { key: "cron", header: "Cron", cell: (s) => <Text size="sm" mono>{s.cron}{s.timezone ? `  ${s.timezone}` : ""}</Text> },
    {
      key: "next", header: "Next run", sortable: true, accessor: (s) => s.nextRun || "~",
      cell: (s) => s.enabled && s.nextRun ? (
        <Tooltip content={inZone(s.nextRun, s.timezone)}><Text size="sm">{untilText(s.nextRun)}</Text></Tooltip>
      ) : <StatusBadge status="disabled" />,
    },
    {
      key: "last", header: "Last run",
      cell: (s) => s.lastStatus ? (
        <Box direction="row" gap={1} align="center"><StatusBadge status={s.lastStatus} /><Text size="xs" color="muted">{timeAgo(s.lastRunAt)}</Text></Box>
      ) : <Text size="sm" color="muted">never</Text>,
    },
    {
      key: "actions", header: "", align: "right",
      cell: (s) => (
        <Box direction="row" gap={1} justify="end" wrap="nowrap">
          <Tooltip content="Run now">
            <Button variant="clear" size="sm" aria-label="Run now" iconLeft="play-arrow-rounded"
              loading={run.isPending && run.variables?.id === s.id} onClick={() => run.mutate(s)} />
          </Tooltip>
          {s.source !== "label" && (
            <>
              <Tooltip content={s.enabled ? "Disable" : "Enable"}>
                <Button variant="clear" size="sm" aria-label={s.enabled ? "Disable" : "Enable"}
                  iconLeft={s.enabled ? "pause-rounded" : "play-circle-outline-rounded"}
                  onClick={() => toggle.mutate(s)} />
              </Tooltip>
              <Tooltip content="Edit"><Button variant="clear" size="sm" aria-label="Edit" iconLeft="edit-outline-rounded" onClick={() => setEditing(s)} /></Tooltip>
              <Tooltip content="Delete"><Button variant="clear" size="sm" color="danger" aria-label="Delete" iconLeft="delete-outline-rounded" onClick={() => setDel(s)} /></Tooltip>
            </>
          )}
        </Box>
      ),
    },
  ];

  const runCols: Column<ScheduleRun>[] = [
    { key: "startedAt", header: "Time", sortable: true, cell: (r) => <Tooltip content={formatDate(r.startedAt)}><Text size="sm">{timeAgo(r.startedAt)}</Text></Tooltip> },
    { key: "scheduleName", header: "Schedule", sortable: true, cell: (r) => <Text size="sm">{r.scheduleName}</Text> },
    { key: "action", header: "Action", cell: (r) => <Text size="sm" mono>{r.action} {r.targetId}</Text> },
    { key: "trigger", header: "Trigger", cell: (r) => <Text size="sm" color="muted">{r.trigger}</Text> },
    { key: "status", header: "Result", cell: (r) => <StatusBadge status={r.status} /> },
    { key: "message", header: "Message", grow: true, truncate: true, cell: (r) => <Text size="sm" color="muted">{r.message}</Text> },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Schedules"
        description="Start, stop and restart containers, deployments and stacks on a cron schedule. Container labels kowan.schedule.start / kowan.schedule.stop are honoured; a schedule here overrides the label for the same action."
        actions={
          <Box direction="row" gap={2}>
            <Button variant="outline" color="neutral" iconLeft="refresh-rounded" loading={rescan.isPending} onClick={() => rescan.mutate()}>
              Rescan labels
            </Button>
            <Button iconLeft="add-rounded" onClick={() => setCreating(true)}>New schedule</Button>
          </Box>
        }
      />
      <SectionCard title="Schedules" noBodyPadding actions={<Badge variant="light" color="neutral">{schedules.data?.length ?? 0}</Badge>}>
        {schedules.isError ? (
          <Text color="danger" style={{ padding: 16 }}>{errorMessage(schedules.error, "Failed to load schedules")}</Text>
        ) : (
          <Table<Schedule>
            data={schedules.data ?? []}
            columns={cols}
            rowKey={(s) => s.id}
            loading={schedules.isLoading}
            globalSearch
            stickyHeader
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="power-outline-rounded" size="xl" />}
                title="No schedules yet"
                description="Stop dev containers at night and start them in the morning, or restart a stack weekly."
                action={<Button iconLeft="add-rounded" onClick={() => setCreating(true)}>New schedule</Button>}
              />
            }
          />
        )}
      </SectionCard>
      <SectionCard title="Run history" description="Runs more than 10 minutes late are skipped, not executed late." noBodyPadding>
        <Table<ScheduleRun>
          data={runs.data ?? []}
          columns={runCols}
          rowKey={(r) => r.id}
          loading={runs.isLoading}
          pagination
          defaultPageSize={25}
          emptyText={<Text size="sm" color="muted">No runs yet</Text>}
        />
      </SectionCard>
      <ScheduleEditor
        open={creating || editing !== null}
        schedule={editing}
        onClose={() => { setCreating(false); setEditing(null); }}
        onSaved={refresh}
      />
      <ConfirmModal
        opened={del !== null}
        onClose={() => setDel(null)}
        title="Delete schedule"
        confirmLabel="Delete"
        loading={remove.isPending}
        onConfirm={() => del && remove.mutate(del)}
        message={<>Delete <b>{del?.name}</b> and its run history?</>}
      />
    </Box>
  );
}
