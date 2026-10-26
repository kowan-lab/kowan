import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Box, Button, Callout, InputToggle, Popup, Select, TextInput } from "@/ui/kit";
import { listContainers, listDeployments, listHosts } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import CronField from "./CronField";
import {
  ACTIONS, createSchedule, updateSchedule,
  type Schedule, type ScheduleAction, type ScheduleInput, type ScheduleTargetType,
} from "./api";

const LABEL_PROJECT = "com.docker.compose.project";

/**
 * Create / edit a schedule. When opened from a container or deployment page
 * the target is fixed (`fixedTarget`); on the Schedules page it is picked.
 */
export default function ScheduleEditor({
  open, onClose, onSaved, schedule, fixedTarget,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
  schedule?: Schedule | null;
  fixedTarget?: { targetType: ScheduleTargetType; targetId: string; hostId: string; label?: string };
}) {
  const blank: ScheduleInput = {
    name: "", targetType: fixedTarget?.targetType ?? "container", targetId: fixedTarget?.targetId ?? "",
    hostId: fixedTarget?.hostId ?? "local", action: "stop", cron: "0 22 * * 1-5", timezone: "", enabled: true, onlyIf: {},
  };
  const [f, setF] = useState<ScheduleInput>(blank);
  useEffect(() => {
    if (!open) return;
    setF(schedule ? {
      name: schedule.name, targetType: schedule.targetType, targetId: schedule.targetId, hostId: schedule.hostId,
      action: schedule.action, cron: schedule.cron, timezone: schedule.timezone, enabled: schedule.enabled, onlyIf: schedule.onlyIf ?? {},
    } : blank);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, schedule]);
  const set = (p: Partial<ScheduleInput>) => setF((x) => ({ ...x, ...p }));

  const pick = open && !fixedTarget && !schedule;
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: pick });
  const containers = useQuery({
    queryKey: ["containers", f.hostId, "all"], queryFn: () => listContainers(f.hostId, true),
    enabled: pick && f.targetType !== "deployment" && !!f.hostId,
  });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, enabled: pick && f.targetType === "deployment" });

  const targetOptions = useMemo(() => {
    if (f.targetType === "deployment") return (deployments.data ?? []).map((d) => ({ value: d.id, label: d.name }));
    const cs = containers.data ?? [];
    if (f.targetType === "stack") {
      const names = Array.from(new Set(cs.map((c) => c.labels?.[LABEL_PROJECT]).filter(Boolean) as string[])).sort();
      return names.map((n) => ({ value: n, label: n }));
    }
    return cs.map((c) => (c.names?.[0] ?? c.id).replace(/^\//, "")).sort().map((n) => ({ value: n, label: n }));
  }, [f.targetType, containers.data, deployments.data]);

  const save = useMutation({
    mutationFn: () => {
      const body = { ...f, onlyIf: f.targetType === "container" && f.action !== "start" ? f.onlyIf : {} };
      return schedule ? updateSchedule(schedule.id, body) : createSchedule(body);
    },
    onSuccess: () => {
      notify({ color: "green", title: "Schedule saved", message: f.name || `${f.action} ${f.targetId}` });
      onSaved();
      onClose();
    },
    onError: (e) => notify({ color: "red", title: "Save failed", message: errorMessage(e, "Could not save the schedule") }),
  });

  const actions = ACTIONS[f.targetType];
  return (
    <Popup open={open} onOpenChange={(o) => { if (!o) onClose(); }} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{schedule ? "Edit schedule" : "New schedule"}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {fixedTarget || schedule ? (
            <Callout color="info">
              Target: {f.targetType} <b>{fixedTarget?.label ?? f.targetId}</b>
            </Callout>
          ) : (
            <Box direction="row" wrap gap={3}>
              <Select
                label="Target type" width={160} value={f.targetType}
                options={[{ value: "container", label: "Container" }, { value: "deployment", label: "Deployment" }, { value: "stack", label: "Stack" }]}
                onChange={(v) => set({ targetType: v as ScheduleTargetType, targetId: "", action: "stop" })}
              />
              {f.targetType !== "deployment" && (
                <Select label="Host" width={180} value={f.hostId} onChange={(v) => set({ hostId: v, targetId: "" })}
                  options={(hosts.data ?? []).map((h) => ({ value: h.id, label: h.name }))} />
              )}
              <Select label="Target" width={240} value={f.targetId} onChange={(v) => set({ targetId: v })} options={targetOptions}
                searchable loading={containers.isLoading || deployments.isLoading} emptyText="Nothing on this host" />
            </Box>
          )}
          <Box direction="row" wrap gap={3}>
            <Select label="Action" width={160} value={f.action} onChange={(v) => set({ action: v as ScheduleAction })}
              options={actions.map((a) => ({ value: a, label: a }))} />
            <TextInput label="Name" placeholder={`${f.action} ${f.targetId}`} value={f.name} onChange={(e) => set({ name: e.target.value })} maxWidth={320} />
          </Box>
          <CronField cron={f.cron} timezone={f.timezone} onCron={(cron) => set({ cron })} onTimezone={(timezone) => set({ timezone })} />
          {f.targetType === "container" && f.action !== "start" && (
            <InputToggle
              label="Only if no exec session is open"
              description="Skip the run while a console/exec is attached to the container (best effort)."
              checked={!!f.onlyIf.noExecSession}
              onChange={(e) => set({ onlyIf: { noExecSession: e.target.checked } })}
            />
          )}
          <InputToggle label="Enabled" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        </Popup.Body>
        <Popup.Footer>
          <Button variant="outline" color="neutral" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!f.targetId || !f.cron.trim()} onClick={() => save.mutate()}>
            {schedule ? "Save" : "Create schedule"}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
