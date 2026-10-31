/**
 * Scheduled start/stop: types and API calls (backend: kowan/api/schedules.py).
 * Kept beside the components that use them so the shared client.ts/types.ts
 * stay untouched.
 */
import { api } from "../../api/client";

/** A runbook runs with its parameters' defaults; a group is every container
 *  on the hosts with that tag (Kowan's own excluded). */
export type ScheduleTargetType = "container" | "deployment" | "stack" | "runbook" | "group";
export type ScheduleAction = "start" | "stop" | "restart" | "deploy" | "run";

export interface Schedule {
  id: string;
  name: string;
  targetType: ScheduleTargetType;
  /** Container name, deployment id or compose project name. */
  targetId: string;
  hostId: string;
  action: ScheduleAction;
  cron: string;
  /** IANA zone; empty = server local time. */
  timezone: string;
  enabled: boolean;
  onlyIf: { noExecSession?: boolean };
  /** "label" = derived from kowan.schedule.* container labels (read-only). */
  source: "manual" | "label";
  error?: string;
  nextRun: string;
  lastRunAt: string;
  lastStatus: string;
  createdAt: string;
  updatedAt: string;
}

export type ScheduleInput = Pick<Schedule, "name" | "targetType" | "targetId" | "hostId" | "action" | "cron" | "timezone" | "enabled" | "onlyIf">;

export interface ScheduleRun {
  id: string;
  scheduleId: string;
  scheduleName: string;
  targetType: ScheduleTargetType;
  targetId: string;
  hostId: string;
  action: ScheduleAction;
  trigger: "schedule" | "manual";
  status: "success" | "failed" | "skipped";
  message: string;
  scheduledFor: string;
  startedAt: string;
  finishedAt: string;
}

export const ACTIONS: Record<ScheduleTargetType, ScheduleAction[]> = {
  container: ["start", "stop", "restart"],
  deployment: ["start", "stop", "restart", "deploy"],
  stack: ["start", "stop", "restart"],
  runbook: ["run"],
  group: ["start", "stop", "restart"],
};

export async function listSchedules(filter: Partial<Pick<Schedule, "targetType" | "targetId" | "hostId">> = {}): Promise<Schedule[]> {
  return (await api.get<Schedule[]>("/schedules", { params: filter })).data;
}
export async function createSchedule(body: ScheduleInput): Promise<Schedule> {
  return (await api.post<Schedule>("/schedules", body)).data;
}
export async function updateSchedule(id: string, body: Partial<ScheduleInput>): Promise<Schedule> {
  return (await api.put<Schedule>(`/schedules/${encodeURIComponent(id)}`, body)).data;
}
export async function deleteSchedule(id: string): Promise<void> {
  await api.delete(`/schedules/${encodeURIComponent(id)}`);
}
export async function runSchedule(id: string): Promise<ScheduleRun> {
  return (await api.post<ScheduleRun>(`/schedules/${encodeURIComponent(id)}/run`, {})).data;
}
export async function listScheduleRuns(scheduleId = "", limit = 100): Promise<ScheduleRun[]> {
  return (await api.get<ScheduleRun[]>("/schedules/runs", { params: { scheduleId, limit } })).data;
}
export async function previewSchedule(cron: string, timezone: string, count = 5): Promise<string[]> {
  return (await api.post<{ nextRuns: string[] }>("/schedules/preview", { cron, timezone, count })).data.nextRuns ?? [];
}
export async function rescanScheduleLabels(): Promise<void> {
  await api.post("/schedules/rescan", {});
}

/** "in 3h 12m" / "in 2d" for an upcoming instant. */
export function untilText(iso: string): string {
  if (!iso) return "";
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms <= 0) return "due";
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `in ${h}h ${m % 60}m`;
  return `in ${Math.round(h / 24)}d`;
}

/** Wall-clock rendering of an instant in a given zone (falls back to local). */
export function inZone(iso: string, tz: string): string {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat(undefined, {
      weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      timeZone: tz || undefined, timeZoneName: "short",
    }).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleString();
  }
}

export const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Weekdays 08:00", cron: "0 8 * * 1-5" },
  { label: "Weekdays 22:00", cron: "0 22 * * 1-5" },
  { label: "Every day 03:00", cron: "0 3 * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Sundays 04:00", cron: "0 4 * * 0" },
];

export function timeZones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  const all = intl.supportedValuesOf?.("timeZone") ?? ["UTC", "Europe/Rome", "Europe/London", "America/New_York"];
  return all.includes("UTC") ? all : ["UTC", ...all];
}
