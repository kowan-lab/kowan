// The app's policies, drawn as mockup D draws them: a compact strip of knobs on
// the Overview ("Policies and wiring") and four framed sections on the
// Policies tab, each with the current state, a knob and a door to the editor.

import type { ReactNode } from "react";
import { Link } from "react-router";
import { ArrowUpCircle, Clock, Globe, HardDrive } from "lucide-react";
import { Knob } from "@/ui/kit";
import type { App } from "../../lib/apps";
import { timeAgo } from "../../utils";
import { MODE_OPTIONS, outcomeText, scanLabel } from "../autoupdate/labels";
import { inZone, untilText } from "../schedules/api";
import { retentionText } from "../volbackup/api";
import { BACKUP_OPTIONS, EXPOSE_OPTIONS, SCHEDULE_OPTIONS, UPDATE_OPTIONS, type PolicyControls } from "./policies";
import { Btn, LBL, Pill, Tag } from "./ui";
import { cn } from "@/lib/utils";

const ICON = "size-[16px] shrink-0";

/** Anonymous volumes are 64-hex names: show the first 12 like Docker does. */
function volLabel(v: string): string {
  return /^[0-9a-f]{64}$/.test(v) ? `${v.slice(0, 12)} (anonymous)` : v;
}

// ── knobs (shared by the strip and the sections) ────────────────────────

function UpdatesKnob({ app, pc, showOptions }: { app: App; pc: PolicyControls; showOptions?: boolean }) {
  const u = pc.updates;
  if (u.mode === "mixed") {
    return (
      <span className="inline-flex items-center gap-[10px]">
        <Knob aria-label={`Updates for ${app.name}`} value="off" options={UPDATE_OPTIONS} disabled hideLabel />
        <span className={LBL}>Mixed</span>
        {pc.canOperate && <Btn size="sm" onClick={() => pc.open("update-details")}>Set per container</Btn>}
      </span>
    );
  }
  return (
    <Knob
      aria-label={`Update policy for ${app.name}`}
      value={u.mode ?? "off"}
      options={UPDATE_OPTIONS}
      showOptions={showOptions}
      loading={u.saving || (u.loading && !u.mode)}
      disabled={!pc.canOperate || !pc.live || u.labelLocked || !!u.error}
      onChange={(m) => u.set(m)}
    />
  );
}

function BackupsKnob({ app, pc }: { app: App; pc: PolicyControls }) {
  const b = pc.backups;
  const noVolumes = b.volumes !== undefined && b.volumes.length === 0 && b.jobs.length === 0;
  return (
    <Knob
      aria-label={`Backups for ${app.name}`}
      value={b.on ? "on" : "off"}
      options={BACKUP_OPTIONS}
      loading={b.saving}
      disabled={!pc.canOperate || noVolumes}
      onChange={(v) => b.toggle(v === "on")}
    />
  );
}

function ScheduleKnob({ app, pc }: { app: App; pc: PolicyControls }) {
  const s = pc.schedule;
  const labelOnly = s.schedules.length > 0 && s.manual.length === 0;
  return (
    <Knob
      aria-label={`Schedule for ${app.name}`}
      value={s.on ? "on" : "off"}
      options={SCHEDULE_OPTIONS}
      loading={s.saving}
      disabled={!pc.canOperate || labelOnly}
      onChange={(v) => s.toggle(v === "on")}
    />
  );
}

function ExposureKnob({ app, pc }: { app: App; pc: PolicyControls }) {
  const e = pc.exposure;
  return (
    <Knob
      aria-label={`Domain for ${app.name}`}
      value={e.on ? "on" : "off"}
      options={EXPOSE_OPTIONS}
      disabled={!pc.canOperate || !pc.live || !e.canExpose}
      // Both directions go through the expose dialog: it asks for the domain,
      // and it holds the "Unexpose" action for a domain Kowan set up.
      onChange={() => pc.open("expose")}
    />
  );
}

// ── Overview strip ──────────────────────────────────────────────────────

function Cell({ label, children, control }: { label: string; children: ReactNode; control?: boolean }) {
  return (
    <div className="flex min-h-[72px] min-w-0 flex-col justify-start gap-[6px] px-[16px] py-[12px] shadow-[inset_-1px_0_0_var(--border),inset_0_-1px_0_var(--border)] [&:nth-child(3n)]:shadow-[inset_0_-1px_0_var(--border)] [&:nth-last-child(-n+3)]:shadow-[inset_-1px_0_0_var(--border)] last:!shadow-none max-sm:[&:not(:last-child)]:!shadow-[inset_0_-1px_0_var(--border)]">
      <div className={LBL}>{label}</div>
      <div className={cn("min-w-0 text-[14px] leading-[20px]", !control && "truncate")}>{children}</div>
    </div>
  );
}

function sourceText(app: App): string {
  const d = app.deployment;
  if (d) {
    if (d.git?.repoUrl) return `Git · ${d.git.repoUrl.replace(/^[a-z+]+:\/\//i, "").replace(/\.git$/, "")}`;
    return d.type === "compose" ? "Kowan · inline compose" : "Kowan · container spec";
  }
  if (app.kind === "stack") {
    const f = app.containers[0]?.container.labels?.["com.docker.compose.project.config_files"];
    return f ? `Discovered · ${f}` : "Discovered from labels";
  }
  return "Standalone container";
}

export function PolicyStrip({ app, pc }: { app: App; pc: PolicyControls }) {
  const vols = pc.backups.volumes;
  const pub = app.ports.filter((p) => p.public);
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3">
      <Cell label="Volumes">
        <span className="font-mono text-[13px] leading-[20px] font-medium">{vols === undefined ? "…" : vols.length ? vols.map(volLabel).join(", ") : "none"}</span>
      </Cell>
      <Cell label="Source">
        <span className="font-mono text-[13px] leading-[20px] font-medium">{sourceText(app)}</span>
      </Cell>
      <Cell label="Exposure">
        <span className="font-mono text-[13px] leading-[20px] font-medium">
          {pc.exposure.domains.length
            ? pc.exposure.domains.map((d) => d.domain).join(", ")
            : pub.length
              ? `${pub.map((p) => `:${p.publicPort}`).join(" ")} · no domain`
              : "internal only"}
        </span>
      </Cell>
      <Cell label="Updates" control><UpdatesKnob app={app} pc={pc} /></Cell>
      <Cell label="Backups" control><BackupsKnob app={app} pc={pc} /></Cell>
      <Cell label="Schedule" control><ScheduleKnob app={app} pc={pc} /></Cell>
    </div>
  );
}

// ── Policies tab ────────────────────────────────────────────────────────

function Section({ icon, title, desc, status, children }: { icon: ReactNode; title: string; desc: string; status: ReactNode; children: ReactNode }) {
  return (
    <section className="grid grid-cols-[264px_minmax(0,1fr)] bg-card shadow-[inset_0_0_0_1px_var(--border)] max-[900px]:grid-cols-1">
      <div className="flex flex-col items-start gap-[8px] p-[16px] shadow-[inset_-1px_0_0_var(--border)] max-[900px]:shadow-[inset_0_-1px_0_var(--border)]">
        <h3 className="m-0 flex items-center gap-[8px] text-[14px] leading-[18px] font-bold tracking-[0.02em] uppercase">
          {icon}
          {title}
        </h3>
        <p className="m-0 text-[13px] leading-[18px] text-muted-foreground">{desc}</p>
        {status}
      </div>
      <div className="py-[4px]">{children}</div>
    </section>
  );
}

function Row({ k, small, mono, children }: { k: ReactNode; small?: ReactNode; mono?: boolean; children: ReactNode }) {
  return (
    <div className="grid min-h-[56px] grid-cols-[220px_minmax(0,1fr)] items-center gap-[16px] px-[16px] py-[10px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none max-[700px]:grid-cols-1">
      <div className={cn(mono ? "font-mono text-[13px] leading-[20px] font-medium" : "text-[14px] leading-[20px] font-semibold")}>
        {k}
        {small && <small className="block font-sans text-[13px] leading-[18px] font-normal text-muted-foreground">{small}</small>}
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-[10px] text-[14px]">{children}</div>
    </div>
  );
}

function StatusPill({ on, children }: { on: boolean; children: ReactNode }) {
  return on ? <Pill tone="a" signal>{children}</Pill> : <Pill tone="n">{children}</Pill>;
}

const Muted = ({ children }: { children: ReactNode }) => <span className="text-[13px] leading-[18px] text-muted-foreground">{children}</span>;

export function PoliciesTab({ app, pc }: { app: App; pc: PolicyControls }) {
  const u = pc.updates;
  const b = pc.backups;
  const s = pc.schedule;
  const e = pc.exposure;
  const eff = u.policies[0]?.effective;
  const modeLabel = u.mode === "mixed" ? "Mixed" : UPDATE_OPTIONS.find((o) => o.value === (u.mode ?? "off"))?.label ?? "Off";
  const waiting = app.containers.filter((c) => c.update?.updateAvailable);
  const lastResult = u.policies.map((p) => p.state).find((st) => st?.lastResult);
  // One row per published port: Docker reports the IPv4 and IPv6 binds separately.
  const ports: { p: (typeof e.ports)[number]; ips: string[] }[] = [];
  for (const p of e.ports) {
    const same = ports.find((x) => x.p.containerId === p.containerId && x.p.publicPort === p.publicPort && x.p.protocol === p.protocol);
    if (same) {
      if (p.ip && !same.ips.includes(p.ip)) same.ips.push(p.ip);
      if (p.scope === "public") same.p = { ...same.p, scope: "public", risk: same.p.risk ?? p.risk };
    } else ports.push({ p, ips: p.ip ? [p.ip] : [] });
  }

  return (
    <div className="flex flex-col gap-[12px]">
      <Section
        icon={<ArrowUpCircle className={ICON} strokeWidth={1.5} aria-hidden />}
        title="Updates"
        desc="Watch images for new digests, gate them on a scan, and decide how they land."
        status={<StatusPill on={!!u.mode && u.mode !== "off"}>{modeLabel}</StatusPill>}
      >
        {waiting.length > 0 && (
          <Row k="Waiting" small={`${waiting.length} of ${app.total} container${app.total === 1 ? "" : "s"}`}>
            {waiting.map((c) => (
              <Tag key={c.container.id}>{c.service} · {c.container.image}</Tag>
            ))}
            {app.deployment ? <Muted>Redeploy pulls the new images.</Muted> : <Muted>Update each from its container page.</Muted>}
          </Row>
        )}
        <Row k="Policy" small={u.labelLocked ? "Set by a kowan.update label in the compose file" : app.deployment ? "Applies to every service of the deployment" : app.total > 1 ? `Applied to all ${app.total} containers` : "Applies to this container"}>
          <UpdatesKnob app={app} pc={pc} showOptions />
        </Row>
        <Row k="Window and gates" small="Maintenance window, minimum image age, scan gate">
          {eff ? (
            <Muted>
              {eff.window ? `Window ${eff.window}` : "Any time"} · {eff.minAge ? `min age ${eff.minAge}` : "no minimum age"} · {scanLabel(eff.scanBlock)}
            </Muted>
          ) : (
            <Muted>{u.error ? "Could not load the policy." : "…"}</Muted>
          )}
          <Btn size="sm" onClick={() => pc.open("update-details")}>Edit details</Btn>
        </Row>
        <Row k="Last check">
          {lastResult ? (
            <>
              <Muted>{timeAgo(lastResult.lastCheckAt)}</Muted>
              <Tag>{outcomeText(lastResult.lastResult ?? "")}</Tag>
              {lastResult.lastMessage && <Muted>{lastResult.lastMessage}</Muted>}
            </>
          ) : (
            <Muted>{u.mode === "off" || !u.mode ? "Off: no automatic checks." : `Not checked yet. ${MODE_OPTIONS.find((o) => o.value === u.mode)?.description ?? ""}.`}</Muted>
          )}
        </Row>
      </Section>

      <Section
        icon={<HardDrive className={ICON} strokeWidth={1.5} aria-hidden />}
        title="Backups"
        desc="Restic snapshots of the workload's volumes, to a repository set up once on the Backups page."
        status={<StatusPill on={b.on}>{b.on ? "On" : "Off"}</StatusPill>}
      >
        <Row k="Back up" small={b.volumes === undefined ? "Finding volumes…" : b.volumes.length ? `${b.volumes.length} volume${b.volumes.length === 1 ? "" : "s"}` : "No named volumes"}>
          <BackupsKnob app={app} pc={pc} />
          {!b.jobs.length && <Muted>Turning it on opens a new backup job.</Muted>}
        </Row>
        <Row k="Volumes">
          {b.volumes?.length ? b.volumes.map((v) => <span key={v} title={v}><Tag>{volLabel(v)}</Tag></span>) : <Muted>{b.volumes === undefined ? "…" : "This workload has no named volumes."}</Muted>}
        </Row>
        <Row k="Jobs">
          {b.jobs.length ? (
            b.jobs.map((j) => (
              <span key={j.id} className="inline-flex items-center gap-[8px]">
                <Tag>{j.name} · {j.schedule || "manual"} · {retentionText(j.retention)}</Tag>
                {pc.canOperate && !app.deployment && (
                  <Btn size="sm" onClick={() => pc.open({ backupEdit: j })}>Edit</Btn>
                )}
              </span>
            ))
          ) : (
            <Muted>No backup job covers this workload.</Muted>
          )}
          {pc.canOperate && app.deployment && <Btn size="sm" onClick={() => pc.open("backup-manage")}>Manage jobs</Btn>}
          {pc.canOperate && !app.deployment && (b.volumes?.length ?? 0) > 0 && <Btn size="sm" onClick={() => pc.open("backup-new")}>New backup job</Btn>}
          <Link to="/backups" className="text-[13px] text-[var(--link)] underline decoration-1 underline-offset-[3px]">Repositories and runs</Link>
        </Row>
      </Section>

      <Section
        icon={<Clock className={ICON} strokeWidth={1.5} aria-hidden />}
        title="Schedule"
        desc="Start, stop or restart the workload on a timer, for things that only need to run part of the day."
        status={<StatusPill on={s.on}>{s.on ? "Timer" : "Always on"}</StatusPill>}
      >
        <Row k="Run" small={s.schedules.length && !s.manual.length ? "Set by kowan.schedule labels" : "A timer starts and stops it on cron"}>
          <ScheduleKnob app={app} pc={pc} />
          {!s.schedules.length && <Muted>Turning it on opens a new schedule.</Muted>}
        </Row>
        <Row k="Schedules">
          {s.schedules.length ? (
            s.schedules.map((x) => (
              <Tag key={x.id}>
                {x.action} · {x.cron}
                {x.enabled && x.nextRun ? ` · ${untilText(x.nextRun)}` : x.enabled ? "" : " · off"}
              </Tag>
            ))
          ) : (
            <Muted>No schedules.</Muted>
          )}
          {s.schedules[0]?.nextRun && <Muted>Next {inZone(s.schedules[0].nextRun, s.schedules[0].timezone)}</Muted>}
          <Btn size="sm" onClick={() => pc.open("schedule-manage")}>Manage schedules</Btn>
        </Row>
      </Section>

      <Section
        icon={<Globe className={ICON} strokeWidth={1.5} aria-hidden />}
        title="Exposure"
        desc="What the workload publishes on the host, and the domain it is reached on through the reverse proxy."
        status={<StatusPill on={e.on}>{e.on ? "Domain" : app.ports.some((p) => p.public) ? "Ports" : "Internal"}</StatusPill>}
      >
        <Row k="Domain" small={e.proxies.length ? `Proxy: ${e.proxies.map((p) => p.name).join(", ")}` : e.loading ? "Looking for a proxy…" : "No reverse proxy found on this host"}>
          <ExposureKnob app={app} pc={pc} />
          {e.domains.map((d) => (
            <Tag key={`${d.router}-${d.domain}`}>{d.tls ? "https" : "http"}://{d.domain}</Tag>
          ))}
        </Row>
        {ports.length ? (
          ports.map(({ p, ips }) => (
            <Row key={`${p.containerId}-${p.publicPort}-${p.protocol}`} mono k={`:${p.publicPort} → ${p.containerName}:${p.privatePort}`} small={ips.length ? `Bound on ${ips.join(" and ")}` : "Bound on every interface"}>
              <Pill tone={p.scope === "public" ? (p.risk ? "r" : "a") : "n"}>
                {p.scope === "public" ? "Public" : p.scope === "local" ? "Loopback" : "Interface"} · {p.protocol}
              </Pill>
              {p.risk && <Muted>{p.risk.label} reachable from outside</Muted>}
            </Row>
          ))
        ) : (
          <Row k="Ports">
            <Muted>{e.loading ? "…" : e.error ? "Could not read the host's exposure." : "Nothing published. Containers are reachable only on their networks."}</Muted>
          </Row>
        )}
        <Row k="Fleet view">
          <Link to="/exposure" className="text-[13px] text-[var(--link)] underline decoration-1 underline-offset-[3px]">Every published port and domain</Link>
        </Row>
      </Section>
    </div>
  );
}
