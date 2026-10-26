// Policy state of one app, read from the existing APIs and turned by knobs:
//
//  - Updates   auto-update policy mode  (GET/PUT /deployments/{id}/autoupdate,
//              or /hosts/{h}/containers/{c}/autoupdate for each container)
//  - Backups   restic backup jobs covering the app's volumes  (/volbackups/jobs)
//  - Schedule  start/stop schedules on the app  (/schedules)
//  - Exposure  a domain through the host's reverse proxy  (/hosts/{h}/exposure)
//
// A knob changes the simple thing in place (mode, enable/disable); the
// details open the existing editors (PolicyEditor, JobEditor, ScheduleEditor,
// ExposeDialog), mounted once by `dialogs`.

import { useState, type ReactNode } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Popup, type KnobOption } from "@/ui/kit";
import {
  getContainerSpec,
  getContainerUpdatePolicy,
  getDeploymentUpdatePolicy,
  putContainerUpdatePolicy,
  putDeploymentUpdatePolicy,
} from "../../api/client";
import type { App } from "../../lib/apps";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import type { ObjectUpdatePolicy, UpdateMode } from "../../types";
import PolicyEditor, { POLICY_KEY } from "../autoupdate/PolicyEditor";
import { useCanOperate } from "../deployment/shared";
import { ExposeDialog } from "../exposure/ExposeDialog";
import { getHostExposure, type ExposeTarget } from "../exposure/api";
import { composeServices } from "../exposure/compose";
import ScheduleEditor from "../schedules/ScheduleEditor";
import ScheduleSection from "../schedules/ScheduleSection";
import { listSchedules, updateSchedule, type ScheduleTargetType } from "../schedules/api";
import DeploymentBackupSection from "../volbackup/DeploymentBackupSection";
import JobEditor from "../volbackup/JobEditor";
import { listJobs, previewSelector, updateJob, type BackupJob, type Selector } from "../volbackup/api";

export const UPDATE_OPTIONS: KnobOption<UpdateMode>[] = [
  { value: "off", label: "Off" },
  { value: "notify", label: "Notify" },
  { value: "pull", label: "Pull" },
  { value: "apply", label: "Auto" },
];
export const BACKUP_OPTIONS: KnobOption<"off" | "on">[] = [
  { value: "off", label: "Off" },
  { value: "on", label: "On" },
];
export const SCHEDULE_OPTIONS: KnobOption<"off" | "on">[] = [
  { value: "off", label: "Always on" },
  { value: "on", label: "Timer" },
];
export const EXPOSE_OPTIONS: KnobOption<"off" | "on">[] = [
  { value: "off", label: "No domain" },
  { value: "on", label: "Domain" },
];

type Dialog =
  | null
  | "update-details"
  | "backup-new"
  | { backupEdit: BackupJob }
  | "backup-manage"
  | "schedule-new"
  | "schedule-manage"
  | "expose";

function hostOf(app: App): string {
  return app.hostIds[0] ?? "";
}

/** Named volumes of a container spec (bind mounts are paths and are skipped). */
function namedVolumes(sources: { source: string }[]): string[] {
  return sources.map((v) => v.source).filter((s) => !!s && !s.startsWith("/") && !s.startsWith("."));
}

export function usePolicyControls(app: App | undefined) {
  const qc = useQueryClient();
  const canOperate = useCanOperate();
  const [dialog, setDialog] = useState<Dialog>(null);
  const host = app ? hostOf(app) : "";
  const dep = app?.deployment;
  const ctrs = app?.containers ?? [];
  const live = !!app && !app.stale;

  // ── Updates ──────────────────────────────────────────────────────────
  const updateTargets: { key: unknown[]; get: () => Promise<ObjectUpdatePolicy>; put: (b: Parameters<typeof putDeploymentUpdatePolicy>[1]) => Promise<ObjectUpdatePolicy> }[] =
    !app
      ? []
      : dep
        ? [{ key: [POLICY_KEY, "dep", dep.id], get: () => getDeploymentUpdatePolicy(dep.id), put: (b) => putDeploymentUpdatePolicy(dep.id, b) }]
        : ctrs.map((c) => ({
            key: [POLICY_KEY, c.hostId, c.container.id],
            get: () => getContainerUpdatePolicy(c.hostId, c.container.id),
            put: (b) => putContainerUpdatePolicy(c.hostId, c.container.id, b),
          }));
  const updateQs = useQueries({
    queries: updateTargets.map((t) => ({ queryKey: t.key, queryFn: t.get, staleTime: 15_000, retry: false, enabled: live })),
  });
  const policies = updateQs.map((q) => q.data).filter((d): d is ObjectUpdatePolicy => !!d);
  const modes = [...new Set(policies.map((p) => p.effective.mode))];
  const updateMode: UpdateMode | "mixed" | null = policies.length === 0 ? null : modes.length === 1 ? modes[0] : "mixed";
  const updateLabelLocked = policies.some((p) => p.effective.sources.mode === "label");
  const setUpdateMode = useMutation({
    mutationFn: async (mode: UpdateMode) => {
      const out = await Promise.all(
        updateTargets.map((t, i) => {
          const cur = updateQs[i]?.data;
          return t.put({
            mode,
            window: cur?.stored?.window ?? "",
            minAge: cur?.stored?.minAge ?? "",
            scanBlock: cur?.stored?.scanBlock ?? "",
          });
        }),
      );
      return out;
    },
    onSuccess: (res, mode) => {
      res.forEach((d, i) => qc.setQueryData(updateTargets[i].key, d));
      qc.invalidateQueries({ queryKey: ["autoupdate-overview"] });
      notify({ color: "green", title: "Auto-update", message: `${app?.name}: ${UPDATE_OPTIONS.find((o) => o.value === mode)?.label}` });
    },
    onError: (err) => notify({ color: "red", title: "Save failed", message: errorMessage(err, "Could not save the policy") }),
  });

  // ── Backups ──────────────────────────────────────────────────────────
  const firstCtr = ctrs[0];
  const specQ = useQuery({
    queryKey: ["container-spec", host, firstCtr?.container.id],
    queryFn: () => getContainerSpec(host, firstCtr!.container.id),
    enabled: live && app?.kind === "container" && !!firstCtr,
    staleTime: 60_000,
  });
  const selector: Selector | null = !app
    ? null
    : dep
      ? { mode: "deployment", deploymentId: dep.id }
      : app.kind === "stack"
        ? { mode: "stack", stack: app.project }
        : specQ.data
          ? { mode: "names", volumes: namedVolumes(specQ.data.volumes ?? []) }
          : null;
  const volumesQ = useQuery({
    queryKey: ["volbackups", "preview", host, selector],
    queryFn: () => previewSelector(host, selector!),
    enabled: live && !!selector && !!host && selector.mode !== "names",
    staleTime: 60_000,
    retry: false,
  });
  const volumes: string[] | undefined = selector?.mode === "names" ? selector.volumes ?? [] : volumesQ.data;
  const jobsQ = useQuery({
    queryKey: ["volbackups", "jobs", dep ? "deployment" : "host", dep ? dep.id : host],
    queryFn: () => (dep ? listJobs({ deploymentId: dep.id }) : listJobs({ hostId: host })),
    enabled: !!app && (!!dep || !!host),
  });
  const jobs = (jobsQ.data ?? []).filter((j) => {
    if (dep) return true;
    if (j.hostId && j.hostId !== host) return false;
    if (app?.kind === "stack" && j.selector.mode === "stack" && j.selector.stack === app.project) return true;
    if (j.selector.mode === "names" && volumes?.length) return (j.selector.volumes ?? []).some((v) => volumes.includes(v));
    return false;
  });
  const backupsOn = jobs.some((j) => j.enabled);
  const toggleBackups = useMutation({
    mutationFn: (on: boolean) => Promise.all(jobs.map((j) => updateJob(j.id, { enabled: on }))),
    onSuccess: (_r, on) => {
      qc.invalidateQueries({ queryKey: ["volbackups"] });
      notify({ color: on ? "green" : "gray", title: on ? "Backups on" : "Backups paused", message: `${jobs.length} backup job${jobs.length === 1 ? "" : "s"} ${on ? "enabled" : "disabled"}.` });
    },
    onError: (err) => notify({ color: "red", title: "Backup jobs", message: errorMessage(err, "Could not change the backup jobs") }),
  });
  const backupInitial = app && selector
    ? {
        name: `${app.name} volumes`,
        hostId: host,
        selector,
        hooks: { stopContainers: true, pre: [], post: [] },
      }
    : undefined;

  // ── Schedule ─────────────────────────────────────────────────────────
  const schedTarget: { targetType: ScheduleTargetType; targetId: string; hostId: string } | null = !app
    ? null
    : dep
      ? { targetType: "deployment", targetId: dep.id, hostId: "" }
      : app.kind === "stack"
        ? { targetType: "stack", targetId: app.project ?? app.name, hostId: host }
        : { targetType: "container", targetId: app.name, hostId: host };
  const schedQ = useQuery({
    queryKey: ["schedules", schedTarget?.targetType, schedTarget?.targetId, schedTarget?.hostId],
    queryFn: () =>
      listSchedules({
        targetType: schedTarget!.targetType,
        targetId: schedTarget!.targetId,
        ...(schedTarget!.hostId ? { hostId: schedTarget!.hostId } : {}),
      }),
    enabled: !!schedTarget,
  });
  const schedules = schedQ.data ?? [];
  const manualSchedules = schedules.filter((s) => s.source !== "label");
  const scheduleOn = schedules.some((s) => s.enabled);
  const toggleSchedule = useMutation({
    mutationFn: (on: boolean) => Promise.all(manualSchedules.map((s) => updateSchedule(s.id, { enabled: on }))),
    onSuccess: (_r, on) => {
      qc.invalidateQueries({ queryKey: ["schedules"] });
      notify({ color: on ? "green" : "gray", title: on ? "Timer on" : "Timer off", message: `${manualSchedules.length} schedule${manualSchedules.length === 1 ? "" : "s"} ${on ? "enabled" : "disabled"}.` });
    },
    onError: (err) => notify({ color: "red", title: "Schedules", message: errorMessage(err, "Could not change the schedules") }),
  });

  // ── Exposure ─────────────────────────────────────────────────────────
  const exposureQ = useQuery({
    queryKey: ["exposure", host],
    queryFn: () => getHostExposure(host),
    enabled: live && !!host,
    staleTime: 15_000,
  });
  const ids = new Set(ctrs.map((c) => c.container.id));
  const owns = (o: { containerId: string; deploymentId: string; stack: string }) =>
    dep ? o.deploymentId === dep.id || ids.has(o.containerId) : app?.kind === "stack" ? o.stack === app.project || ids.has(o.containerId) : ids.has(o.containerId);
  const exposedPorts = (exposureQ.data?.ports ?? []).filter(owns);
  const domains = (exposureQ.data?.domains ?? []).filter(owns);
  const exposeTargetCtr = ctrs.find((c) => (c.container.ports ?? []).length > 0) ?? ctrs[0];
  const exposeTarget: ExposeTarget | null = !app
    ? null
    : dep
      ? { kind: "deployment", id: dep.id, service: dep.type === "compose" ? exposeTargetCtr?.service : undefined }
      : exposeTargetCtr
        ? { kind: "container", id: exposeTargetCtr.container.id }
        : null;
  const exposeDefaultPort = (exposeTargetCtr?.container.ports ?? []).find((p) => p.privatePort)?.privatePort;

  const close = () => setDialog(null);
  const refreshBackups = () => qc.invalidateQueries({ queryKey: ["volbackups"] });

  const dialogs: ReactNode = app ? (
    <>
      <Popup open={dialog === "update-details"} onOpenChange={(o) => !o && close()} size="xl">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Auto-update · {app.name}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            {dialog === "update-details" &&
              (dep ? (
                <PolicyEditor kind="deployment" deploymentId={dep.id} />
              ) : (
                ctrs.map((c) => <PolicyEditor key={c.container.id} kind="container" hostId={c.hostId} containerId={c.container.id} />)
              ))}
          </Popup.Body>
        </Popup.Content>
      </Popup>
      {backupInitial && (
        <JobEditor
          open={dialog === "backup-new" || (typeof dialog === "object" && dialog !== null && "backupEdit" in dialog)}
          job={typeof dialog === "object" && dialog !== null && "backupEdit" in dialog ? dialog.backupEdit : null}
          initial={backupInitial}
          onClose={close}
          onSaved={refreshBackups}
        />
      )}
      {dep && (
        <Popup open={dialog === "backup-manage"} onOpenChange={(o) => !o && close()} size="xl">
          <Popup.Content>
            <Popup.Header>
              <Popup.Title>Backups · {app.name}</Popup.Title>
              <Popup.Close />
            </Popup.Header>
            <Popup.Body>
              {dialog === "backup-manage" && <DeploymentBackupSection deploymentId={dep.id} deploymentName={dep.name} hostId={host} />}
            </Popup.Body>
          </Popup.Content>
        </Popup>
      )}
      {schedTarget && (
        <>
          <ScheduleEditor
            open={dialog === "schedule-new"}
            schedule={null}
            fixedTarget={{ ...schedTarget, label: app.name }}
            onClose={close}
            onSaved={() => qc.invalidateQueries({ queryKey: ["schedules"] })}
          />
          <Popup open={dialog === "schedule-manage"} onOpenChange={(o) => !o && close()} size="xl">
            <Popup.Content>
              <Popup.Header>
                <Popup.Title>Schedules · {app.name}</Popup.Title>
                <Popup.Close />
              </Popup.Header>
              <Popup.Body>
                {dialog === "schedule-manage" && (
                  <ScheduleSection targetType={schedTarget.targetType} targetId={schedTarget.targetId} hostId={schedTarget.hostId} label={app.name} />
                )}
              </Popup.Body>
            </Popup.Content>
          </Popup>
        </>
      )}
      {dialog === "expose" && exposeTarget && (
        <ExposeDialog
          open
          onClose={() => {
            close();
            qc.invalidateQueries({ queryKey: ["exposure", host] });
          }}
          hostId={host}
          target={exposeTarget}
          name={app.name}
          defaultPort={exposeDefaultPort}
          services={dep?.type === "compose" ? composeServices(dep.compose) : undefined}
        />
      )}
    </>
  ) : null;

  return {
    canOperate,
    live,
    open: setDialog,
    dialogs,
    updates: {
      mode: updateMode,
      policies,
      loading: updateQs.some((q) => q.isLoading),
      error: updateQs.find((q) => q.error)?.error ?? null,
      labelLocked: updateLabelLocked,
      saving: setUpdateMode.isPending,
      set: (m: UpdateMode) => setUpdateMode.mutate(m),
    },
    backups: {
      on: backupsOn,
      jobs,
      volumes,
      loading: jobsQ.isLoading,
      saving: toggleBackups.isPending,
      /** A knob turn: enable/disable the jobs, or open the editor when there is none. */
      toggle: (on: boolean) => {
        if (jobs.length) toggleBackups.mutate(on);
        else if (on) setDialog("backup-new");
      },
    },
    schedule: {
      on: scheduleOn,
      schedules,
      manual: manualSchedules,
      loading: schedQ.isLoading,
      saving: toggleSchedule.isPending,
      toggle: (on: boolean) => {
        if (manualSchedules.length) toggleSchedule.mutate(on);
        else if (on) setDialog("schedule-new");
      },
    },
    exposure: {
      on: domains.length > 0,
      domains,
      ports: exposedPorts,
      proxies: exposureQ.data?.proxies ?? [],
      loading: exposureQ.isLoading,
      error: exposureQ.error ?? null,
      canExpose: !!exposeTarget,
    },
  };
}

export type PolicyControls = ReturnType<typeof usePolicyControls>;
