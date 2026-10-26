// "After creating": everything the workload page lets you set on a deployment
// once it exists, chosen up front in the create form and applied right after
// the record is saved. Each item uses the same API as its knob on the workload
// page (autoupdate, volbackups, schedules, exposure, webhook, protection).

import { useQuery } from "@tanstack/react-query";
import { Checkbox, Knob, NumberInput, Select, Text, TextInput } from "@/ui/kit";
import {
  createDeployHook,
  deployDeployment,
  putDeploymentUpdatePolicy,
  setDeploymentProtection,
} from "../../api/client";
import type { Deployment, UpdateMode } from "../../types";
import { cn } from "../../lib/utils";
import { BACKUP_OPTIONS, EXPOSE_OPTIONS, SCHEDULE_OPTIONS, UPDATE_OPTIONS } from "../apps/policies";
import { exposeTarget, getHostExposure } from "../exposure/api";
import { composeServices } from "../exposure/compose";
import { createSchedule } from "../schedules/api";
import { createJob, listRepos } from "../volbackup/api";

export interface CreatePoliciesValue {
  deployNow: boolean;
  updates: UpdateMode;
  updateWindow: string;
  backups: "off" | "on";
  backupRepoId: string;
  backupTime: string;
  backupKeepDaily: number;
  schedule: "off" | "on";
  scheduleStart: string;
  scheduleStop: string;
  scheduleDays: "daily" | "weekdays";
  domain: "off" | "on";
  domainName: string;
  domainPort: number | "";
  domainService: string;
  webhook: boolean;
  protect: boolean;
}

export function emptyCreatePolicies(): CreatePoliciesValue {
  return {
    deployNow: true,
    updates: "off",
    updateWindow: "",
    backups: "off",
    backupRepoId: "",
    backupTime: "03:00",
    backupKeepDaily: 7,
    schedule: "off",
    scheduleStart: "08:00",
    scheduleStop: "20:00",
    scheduleDays: "weekdays",
    domain: "off",
    domainName: "",
    domainPort: "",
    domainService: "",
    webhook: false,
    protect: false,
  };
}

const TIME = /^([01]\d|2[0-3]):([0-5]\d)$/;

function cronAt(time: string, days: "daily" | "weekdays"): string {
  const [, h, m] = TIME.exec(time) ?? ["", "0", "0"];
  return `${Number(m)} ${Number(h)} * * ${days === "weekdays" ? "1-5" : "*"}`;
}

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A message for the first thing that cannot be applied, or null. */
export function validateCreatePolicies(p: CreatePoliciesValue): string | null {
  if (p.backups === "on" && !p.backupRepoId) return "Choose a repository for the backups";
  if (p.backups === "on" && !TIME.test(p.backupTime)) return "Backup time must be HH:MM";
  if (p.schedule === "on" && (!TIME.test(p.scheduleStart) || !TIME.test(p.scheduleStop))) return "Start and stop times must be HH:MM";
  if (p.schedule === "on" && p.scheduleStart === p.scheduleStop) return "Start and stop times must differ";
  if (p.domain === "on" && !/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(p.domainName.trim())) return "Enter a domain like app.example.com";
  return null;
}

export interface PolicyResult {
  label: string;
  ok: boolean;
  error?: string;
}

/** Apply the choices to a freshly created deployment, one by one. A failure
 *  does not stop the others: the deployment exists either way, and the caller
 *  shows what was not set so it can be finished on the workload page. */
export async function applyCreatePolicies(dep: Deployment, p: CreatePoliciesValue): Promise<PolicyResult[]> {
  const host = dep.targets[0] ?? "";
  const zone = browserZone();
  const steps: [string, () => Promise<unknown>][] = [];
  if (p.protect) steps.push(["Deletion protection", () => setDeploymentProtection(dep.id, true)]);
  if (p.webhook) steps.push(["Redeploy webhook", () => createDeployHook(dep.id)]);
  if (p.updates !== "off")
    steps.push(["Auto-update", () => putDeploymentUpdatePolicy(dep.id, { mode: p.updates, window: p.updateWindow.trim() })]);
  if (p.backups === "on")
    steps.push([
      "Volume backups",
      () =>
        createJob({
          name: `${dep.name} volumes`,
          hostId: host,
          repoId: p.backupRepoId,
          selector: { mode: "deployment", deploymentId: dep.id },
          schedule: cronAt(p.backupTime, "daily"),
          timezone: zone,
          retention: { keepDaily: p.backupKeepDaily || 7 },
          hooks: { stopContainers: true, pre: [], post: [] },
          tags: [],
          enabled: true,
        }),
    ]);
  if (p.schedule === "on")
    steps.push([
      "Schedule",
      async () => {
        const base = { targetType: "deployment" as const, targetId: dep.id, hostId: "", timezone: zone, enabled: true, onlyIf: {} };
        await createSchedule({ ...base, name: `${dep.name}: start`, action: "start", cron: cronAt(p.scheduleStart, p.scheduleDays) });
        await createSchedule({ ...base, name: `${dep.name}: stop`, action: "stop", cron: cronAt(p.scheduleStop, p.scheduleDays) });
      },
    ]);
  // A domain is published by editing the deployment and redeploying it, so it
  // also deploys: no separate deploy call then.
  if (p.domain === "on")
    steps.push([
      "Domain",
      () =>
        exposeTarget(host, {
          target: { kind: "deployment", id: dep.id, ...(p.domainService ? { service: p.domainService } : {}) },
          domain: p.domainName.trim(),
          ...(p.domainPort ? { port: Number(p.domainPort) } : {}),
        }),
    ]);
  else if (p.deployNow) steps.push(["Deploy", () => deployDeployment(dep.id)]);

  const out: PolicyResult[] = [];
  for (const [label, run] of steps) {
    try {
      await run();
      out.push({ label, ok: true });
    } catch (err) {
      const message = (err as { response?: { data?: { error?: string } }; message?: string })?.response?.data?.error ?? (err as Error)?.message;
      out.push({ label, ok: false, error: message });
    }
  }
  return out;
}

const LBL = "font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] text-muted-foreground uppercase";

function Cell({ label, children, wide }: { label: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-3 bg-card px-8 py-6", wide && "md:col-span-full")}>
      <div className={LBL}>{label}</div>
      {children}
    </div>
  );
}

export function CreatePolicies({
  value,
  onChange,
  hostId,
  compose,
  oneColumn,
  hideDeploy,
}: {
  value: CreatePoliciesValue;
  onChange: (v: CreatePoliciesValue) => void;
  /** Narrow containers (a dialog): one setting per row. */
  oneColumn?: boolean;
  /** The caller shows its own "deploy now" control. */
  hideDeploy?: boolean;
  /** First target host, for the domain check (a reverse proxy must be there). */
  hostId: string;
  /** Inline compose text, to offer its services for the domain. */
  compose?: string;
}) {
  const set = <K extends keyof CreatePoliciesValue>(k: K, v: CreatePoliciesValue[K]) => onChange({ ...value, [k]: v });
  const repos = useQuery({ queryKey: ["volbackups", "repos"], queryFn: listRepos, staleTime: 60_000 });
  const exposure = useQuery({
    queryKey: ["exposure", hostId],
    queryFn: () => getHostExposure(hostId),
    enabled: !!hostId,
    staleTime: 30_000,
    retry: false,
  });
  const hasProxy = (exposure.data?.proxies ?? []).length > 0;
  const services = compose ? composeServices(compose) : [];
  const repoList = repos.data ?? [];

  return (
    <div className={cn("grid grid-cols-1 gap-px bg-[var(--border)] shadow-[inset_0_0_0_1px_var(--border)]", !oneColumn && "md:grid-cols-2")}>
      {!hideDeploy && (
      <Cell label="Deploy">
        <Checkbox
          checked={value.deployNow || value.domain === "on"}
          disabled={value.domain === "on"}
          onChange={(e) => set("deployNow", e.currentTarget.checked)}
          label="Deploy right after creating"
          description={value.domain === "on" ? "Publishing a domain deploys it." : "Otherwise it is saved stopped, to deploy later."}
        />
      </Cell>
      )}

      <Cell label="Updates">
        <Knob aria-label="Auto-update" value={value.updates} options={UPDATE_OPTIONS} onChange={(v) => set("updates", v)} />
        {value.updates !== "off" && (
          <TextInput
            label="Maintenance window"
            description="Optional, e.g. 02:00-05:00 sat,sun. Empty means any time."
            value={value.updateWindow}
            onChange={(e) => set("updateWindow", e.target.value)}
            maxWidth={320}
          />
        )}
      </Cell>

      <Cell label="Backups">
        <Knob
          aria-label="Volume backups"
          value={value.backups}
          options={BACKUP_OPTIONS}
          disabled={repoList.length === 0}
          onChange={(v) => onChange({ ...value, backups: v, backupRepoId: value.backupRepoId || repoList[0]?.id || "" })}
        />
        {repoList.length === 0 && !repos.isLoading && (
          <Text size="sm" color="muted">Add a backup repository first (Automation › Backups).</Text>
        )}
        {value.backups === "on" && (
          <div className="flex flex-wrap gap-5">
            <div style={{ minWidth: 200 }}><Select
              label="Repository"
              value={value.backupRepoId}
              onChange={(v) => set("backupRepoId", String(v ?? ""))}
              options={repoList.map((r) => ({ value: r.id, label: r.name }))}
             
            /></div>
            <TextInput label="Every day at" value={value.backupTime} onChange={(e) => set("backupTime", e.target.value)} width={120} />
            <div style={{ width: 120 }}><NumberInput label="Keep daily" min={1} max={365} value={value.backupKeepDaily} onChange={(v) => set("backupKeepDaily", Number(v) || 7)} /></div>
          </div>
        )}
      </Cell>

      <Cell label="Schedule">
        <Knob aria-label="Schedule" value={value.schedule} options={SCHEDULE_OPTIONS} onChange={(v) => set("schedule", v)} />
        {value.schedule === "on" && (
          <div className="flex flex-wrap gap-5">
            <TextInput label="Start at" value={value.scheduleStart} onChange={(e) => set("scheduleStart", e.target.value)} width={120} />
            <TextInput label="Stop at" value={value.scheduleStop} onChange={(e) => set("scheduleStop", e.target.value)} width={120} />
            <div style={{ minWidth: 160 }}><Select
              label="On"
              value={value.scheduleDays}
              onChange={(v) => set("scheduleDays", (v as "daily" | "weekdays") ?? "weekdays")}
              options={[
                { value: "weekdays", label: "Weekdays" },
                { value: "daily", label: "Every day" },
              ]}
             
            /></div>
          </div>
        )}
      </Cell>

      <Cell label="Domain" wide>
        <Knob
          aria-label="Domain"
          value={value.domain}
          options={EXPOSE_OPTIONS}
          disabled={!hostId || !hasProxy}
          onChange={(v) => set("domain", v)}
        />
        {hostId && !exposure.isLoading && !hasProxy && (
          <Text size="sm" color="muted">No Traefik on this host yet: deploy it from Exposure first.</Text>
        )}
        {!hostId && <Text size="sm" color="muted">Choose the host first.</Text>}
        {value.domain === "on" && (
          <div className="flex flex-wrap gap-5">
            <TextInput label="Domain" placeholder="app.example.com" value={value.domainName} onChange={(e) => set("domainName", e.target.value)} minWidth={260} />
            <div style={{ width: 140 }}><NumberInput label="App port" value={value.domainPort} onChange={(v) => set("domainPort", v)} /></div>
            {services.length > 1 && (
              <div style={{ minWidth: 180 }}><Select
                label="Service"
                value={value.domainService}
                onChange={(v) => set("domainService", String(v ?? ""))}
                options={services.map((s) => ({ value: s, label: s }))}
               
              /></div>
            )}
          </div>
        )}
      </Cell>

      <Cell label="Webhook">
        <Checkbox
          checked={value.webhook}
          onChange={(e) => set("webhook", e.currentTarget.checked)}
          label="Create a redeploy webhook"
          description="A URL CI can call to redeploy. Shown on the workload page."
        />
      </Cell>

      <Cell label="Protection">
        <Checkbox
          checked={value.protect}
          onChange={(e) => set("protect", e.currentTarget.checked)}
          label="Protect from deletion"
          description="It cannot be deleted until protection is turned off."
        />
      </Cell>
    </div>
  );
}
