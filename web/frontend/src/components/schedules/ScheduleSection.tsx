import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Box, Button, Table, Text, Tooltip, type Column } from "@/ui/kit";
import SectionCard from "../SectionCard";
import StatusBadge from "../StatusBadge";
import ConfirmModal from "../ConfirmModal";
import { SLOW_REFETCH_MS } from "../../lib/live";
import { notify } from "../../ui/toast";
import { errorMessage, timeAgo } from "../../utils";
import ScheduleEditor from "./ScheduleEditor";
import { deleteSchedule, inZone, listSchedules, runSchedule, untilText, type Schedule, type ScheduleTargetType } from "./api";

/**
 * "Schedules" band for a container or deployment detail page: the schedules
 * (stored and label-derived) on this target, with next-run preview, run now,
 * edit and delete. Insert as `<ScheduleSection targetType=… targetId=… hostId=… />`.
 */
export default function ScheduleSection({
  targetType, targetId, hostId = "", label,
}: { targetType: ScheduleTargetType; targetId: string; hostId?: string; label?: string }) {
  const qc = useQueryClient();
  const key = ["schedules", targetType, targetId, hostId];
  const q = useQuery({
    queryKey: key,
    queryFn: () => listSchedules({ targetType, targetId, ...(hostId ? { hostId } : {}) }),
    enabled: !!targetId,
    refetchInterval: SLOW_REFETCH_MS,
  });
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [creating, setCreating] = useState(false);
  const [del, setDel] = useState<Schedule | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["schedules"] });

  const run = useMutation({
    mutationFn: (s: Schedule) => runSchedule(s.id),
    onSuccess: (r) => {
      notify({ color: r.status === "failed" ? "red" : "green", title: `Run ${r.status}`, message: r.message });
      refresh();
    },
    onError: (e) => notify({ color: "red", title: "Run failed", message: errorMessage(e, "Could not run the schedule") }),
  });
  const remove = useMutation({
    mutationFn: (s: Schedule) => deleteSchedule(s.id),
    onSuccess: () => { setDel(null); refresh(); },
    onError: (e) => notify({ color: "red", title: "Delete failed", message: errorMessage(e, "Could not delete") }),
  });

  const cols: Column<Schedule>[] = [
    {
      key: "action", header: "Action",
      cell: (s) => (
        <Box direction="row" gap={1} align="center">
          <Text size="sm" weight="semibold">{s.action}</Text>
          {s.source === "label" && <Badge size="sm" variant="outline" color="gray" title="From kowan.schedule.* labels">label</Badge>}
        </Box>
      ),
    },
    { key: "cron", header: "When", cell: (s) => <Text size="sm" mono>{s.cron}{s.timezone ? `  ${s.timezone}` : ""}</Text> },
    {
      key: "next", header: "Next run",
      cell: (s) => s.error ? <Text size="sm" color="danger">{s.error}</Text> : s.nextRun ? (
        <Tooltip content={inZone(s.nextRun, s.timezone)}><Text size="sm">{untilText(s.nextRun)}</Text></Tooltip>
      ) : <Text size="sm" color="muted">disabled</Text>,
    },
    {
      key: "last", header: "Last run",
      cell: (s) => s.lastStatus ? <Box direction="row" gap={1} align="center"><StatusBadge status={s.lastStatus} /><Text size="xs" color="muted">{timeAgo(s.lastRunAt)}</Text></Box> : <Text size="sm" color="muted">never</Text>,
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
              <Tooltip content="Edit"><Button variant="clear" size="sm" aria-label="Edit" iconLeft="edit-outline-rounded" onClick={() => setEditing(s)} /></Tooltip>
              <Tooltip content="Delete"><Button variant="clear" size="sm" color="danger" aria-label="Delete" iconLeft="delete-outline-rounded" onClick={() => setDel(s)} /></Tooltip>
            </>
          )}
        </Box>
      ),
    },
  ];

  return (
    <SectionCard
      title="Schedules"
      description="Start, stop or restart on a cron schedule."
      noBodyPadding
      actions={<Button size="sm" variant="outline" color="neutral" iconLeft="add-rounded" onClick={() => setCreating(true)}>Add schedule</Button>}
    >
      <Table<Schedule>
        data={q.data ?? []}
        columns={cols}
        rowKey={(s) => s.id}
        loading={q.isLoading}
        emptyText={<Text size="sm" color="muted">No schedules. Labels kowan.schedule.start / kowan.schedule.stop are picked up too.</Text>}
      />
      <ScheduleEditor
        open={creating || editing !== null}
        schedule={editing}
        fixedTarget={{ targetType, targetId, hostId, label }}
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
    </SectionCard>
  );
}
