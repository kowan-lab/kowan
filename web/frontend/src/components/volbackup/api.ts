/**
 * Volume backups (restic): types and API calls (backend: kowan/api/volbackups.py).
 */
import { api } from "../../api/client";

export type RepoType = "s3" | "local" | "sftp" | "rest";

export interface BackupRepo {
  id: string;
  name: string;
  type: RepoType;
  /** Local repositories live on one host's disk. */
  hostId: string;
  config: Record<string, string>;
  /** Masked: "********" when set, "" otherwise. */
  secrets: Record<string, string>;
  initialized: boolean;
  createdAt: string;
  updatedAt: string;
}
export interface RepoInput {
  name: string;
  type: RepoType;
  hostId?: string;
  config: Record<string, string>;
  secrets: Record<string, string>;
}

export type SelectorMode = "names" | "deployment" | "stack" | "label";
export interface Selector {
  mode: SelectorMode;
  volumes?: string[];
  deploymentId?: string;
  stack?: string;
  exclude?: string[];
}
export interface Retention {
  keepLast?: number;
  keepHourly?: number;
  keepDaily?: number;
  keepWeekly?: number;
  keepMonthly?: number;
  keepYearly?: number;
}
export interface HookCmd {
  container: string;
  command: string;
}
interface Hooks {
  stopContainers?: boolean;
  pre?: HookCmd[];
  post?: HookCmd[];
}
export interface BackupJob {
  id: string;
  name: string;
  hostId: string;
  repoId: string;
  selector: Selector;
  schedule: string;
  timezone: string;
  retention: Retention;
  hooks: Hooks;
  tags: string[];
  enabled: boolean;
  lastRunAt: string;
  createdAt: string;
  updatedAt: string;
  nextRun: string;
  lastRun: BackupRun | null;
  running: boolean;
}
export type JobInput = Pick<BackupJob, "name" | "hostId" | "repoId" | "selector" | "schedule" | "timezone" | "retention" | "hooks" | "tags" | "enabled">;

export interface BackupRun {
  id: string;
  jobId: string;
  jobName: string;
  repoId: string;
  hostId: string;
  kind: "backup" | "restore";
  trigger: string;
  status: "running" | "success" | "warning" | "failed" | "skipped";
  volumes: string[];
  snapshots: { volume: string; id: string }[];
  stats: {
    filesNew?: number; filesChanged?: number; filesTotal?: number; bytesAdded?: number; bytesTotal?: number;
    filesRestored?: number; bytesRestored?: number;
  };
  bytesAdded: number;
  durationSec: number;
  error: string;
  log?: string;
  startedAt: string;
  finishedAt: string;
}

export interface Snapshot {
  id: string;
  shortId: string;
  time: string;
  hostname: string;
  paths: string[];
  tags: string[];
  volume: string;
  jobId: string;
  sizeBytes: number;
  fileCount: number;
  repoId: string;
}
export interface SnapshotEntry {
  name: string;
  path: string;
  type: string;
  isDir: boolean;
  size: number;
  mtime: string;
  mode: string;
}

const B = "/volbackups";

export const listRepos = async () => (await api.get<BackupRepo[]>(`${B}/repos`)).data;
export const createRepo = async (b: RepoInput) => (await api.post<BackupRepo>(`${B}/repos`, b)).data;
export const updateRepo = async (id: string, b: Partial<RepoInput>) => (await api.put<BackupRepo>(`${B}/repos/${id}`, b)).data;
export const deleteRepo = async (id: string) => { await api.delete(`${B}/repos/${id}`); };
export const testRepo = async (id: string, hostId = "") =>
  (await api.post<{ ok: boolean; initialized: boolean; log: string }>(`${B}/repos/${id}/test`, { hostId })).data;
export const browseSnapshot = async (repoId: string, snap: string, path: string, hostId = "") =>
  (await api.get<{ path: string; entries: SnapshotEntry[]; truncated: boolean }>(`${B}/repos/${repoId}/snapshots/${snap}/ls`, { params: { path, hostId } })).data;

export const listJobs = async (params: { hostId?: string; deploymentId?: string } = {}) =>
  (await api.get<BackupJob[]>(`${B}/jobs`, { params })).data;
export const createJob = async (b: JobInput) => (await api.post<BackupJob>(`${B}/jobs`, b)).data;
export const updateJob = async (id: string, b: Partial<JobInput>) => (await api.put<BackupJob>(`${B}/jobs/${id}`, b)).data;
export const deleteJob = async (id: string) => { await api.delete(`${B}/jobs/${id}`); };
export const runJob = async (id: string) => (await api.post<BackupRun>(`${B}/jobs/${id}/run`, {})).data;
export const previewSelector = async (hostId: string, selector: Selector) =>
  (await api.post<{ volumes: string[] }>(`${B}/jobs/preview`, { hostId, selector })).data.volumes;

export const listRuns = async (params: { jobId?: string; volume?: string; hostId?: string; limit?: number } = {}) =>
  (await api.get<BackupRun[]>(`${B}/runs`, { params })).data;
export const getRun = async (id: string) => (await api.get<BackupRun>(`${B}/runs/${id}`)).data;

export const volumeSnapshots = async (hostId: string, name: string) =>
  (await api.get<{ snapshots: Snapshot[]; errors: { repoId: string; error: string }[]; repoIds: string[] }>(
    `${B}/volumes/${encodeURIComponent(hostId)}/${encodeURIComponent(name)}/snapshots`)).data;
export const backupVolume = async (hostId: string, name: string, repoId: string) =>
  (await api.post<BackupRun>(`${B}/volumes/${encodeURIComponent(hostId)}/${encodeURIComponent(name)}/backup`, { repoId })).data;
export const restoreSnapshot = async (b: {
  hostId: string; repoId: string; snapshotId: string; target: string; newVolume: boolean; stopContainers: boolean; exact: boolean;
}) => (await api.post<BackupRun>(`${B}/restore`, b)).data;

export function durationText(sec: number): string {
  if (!sec) return "-";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  return m < 60 ? `${m}m ${Math.round(sec % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function retentionText(r: Retention): string {
  const parts: string[] = [];
  if (r.keepLast) parts.push(`${r.keepLast} last`);
  if (r.keepHourly) parts.push(`${r.keepHourly}h`);
  if (r.keepDaily) parts.push(`${r.keepDaily}d`);
  if (r.keepWeekly) parts.push(`${r.keepWeekly}w`);
  if (r.keepMonthly) parts.push(`${r.keepMonthly}m`);
  if (r.keepYearly) parts.push(`${r.keepYearly}y`);
  return parts.length ? parts.join(" · ") : "keep all";
}

export function selectorText(s: Selector, deploymentName?: (id: string) => string): string {
  switch (s.mode) {
    case "names": return (s.volumes ?? []).join(", ");
    case "deployment": return `deployment ${deploymentName?.(s.deploymentId ?? "") ?? s.deploymentId}`;
    case "stack": return `stack ${s.stack}`;
    case "label": return "label kowan.backup=true";
  }
}
