import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Popup } from "@/ui/kit";
import { deleteCrashSnapshot, getCrashSnapshot, listCrashSnapshots } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage, formatBytes, formatDate, timeAgo } from "../../utils";
import type { CrashSnapshot } from "../../types";
import { Band, Btn, Fact, LBL, Pill, Tag } from "../apps/ui";

/**
 * Crash snapshots (kowan/crashes): when a container died with an error and
 * nobody stopped it, Kowan saved its logs, configuration and environment at
 * that moment, so a crash can be understood after the container was
 * restarted or replaced.
 */

const MONO = "font-mono text-[13px] leading-[20px]";

/** Why it ended, in words: OOM, a signal, or the exit code. */
export function exitReason(code: number, oom: boolean): string {
  if (oom) return "killed for using too much memory (OOM)";
  if (code === 137) return "killed (SIGKILL, exit 137)";
  if (code === 139) return "crashed (segmentation fault, exit 139)";
  if (code === 143) return "terminated (SIGTERM, exit 143)";
  if (code === 134) return "aborted (exit 134)";
  return `exited with code ${code}`;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-[6px]">
      <h3 className="m-0 text-[14px] leading-[20px] font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function CrashDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["crash-snapshot", id], queryFn: () => getCrashSnapshot(id) });
  const s = q.data;
  const d = s?.data;
  const peak = d?.metrics?.length ? Math.max(...d.metrics.map((m) => m.mem || 0)) : 0;
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{s ? `${s.containerName} · ${formatDate(s.createdAt)}` : "Crash"}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {q.isLoading && <span className={LBL}>Loading…</span>}
          {q.isError && <span className="text-[var(--tone-red-text)]">{errorMessage(q.error, "Could not load the snapshot")}</span>}
          {s && d && (
            <div className="flex min-w-0 flex-col gap-[18px]">
              <div className="flex flex-wrap items-center gap-[8px]">
                <Pill tone="r">{exitReason(s.exitCode, s.oomKilled)}</Pill>
                {s.repeats > 0 && <Tag>crashed {s.repeats} more time{s.repeats === 1 ? "" : "s"} until {timeAgo(s.lastAt)}</Tag>}
                {d.error && <span className="text-[13px] text-[var(--tone-red-text)]">{d.error}</span>}
              </div>
              <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-[12px]">
                <Fact label="Image" mono>{d.image}</Fact>
                <Fact label="Started">{d.startedAt ? formatDate(d.startedAt) : "—"}</Fact>
                <Fact label="Ended">{d.finishedAt ? formatDate(d.finishedAt) : "—"}</Fact>
                <Fact label="Restarts">{`${d.restartCount} · policy ${d.restartPolicy}`}</Fact>
                <Fact label="Memory limit">{d.memoryLimit ? formatBytes(d.memoryLimit) : "none"}</Fact>
                <Fact label="Memory, last 30 min">{peak ? `peak ${formatBytes(peak)}` : "no data"}</Fact>
              </div>
              <Section title="Last log lines">
                <pre className={`m-0 max-h-[360px] overflow-auto bg-[var(--muted)] p-[10px_12px] whitespace-pre-wrap break-words ${MONO}`}>
                  {d.logs || "(no output)"}
                </pre>
              </Section>
              {d.health.lastOutput && (
                <Section title={`Last health check (${d.health.status})`}>
                  <pre className={`m-0 max-h-[160px] overflow-auto bg-[var(--muted)] p-[10px_12px] whitespace-pre-wrap ${MONO}`}>{d.health.lastOutput}</pre>
                </Section>
              )}
              <Section title="Command">
                <code className={MONO}>{[...d.entrypoint, ...d.command].join(" ") || "(image default)"}</code>
                {(d.workingDir || d.user) && <span className={LBL}>{[d.workingDir && `in ${d.workingDir}`, d.user && `as ${d.user}`].filter(Boolean).join(" · ")}</span>}
              </Section>
              <Section title="Environment">
                <span className={LBL}>Values of names that look secret are hidden.</span>
                <div className={`grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-[16px] gap-y-[2px] ${MONO}`}>
                  {d.env.map((e) => (
                    <div key={e.name} className="contents">
                      <span className="text-muted-foreground">{e.name}</span>
                      <span className="break-all">{e.value}</span>
                    </div>
                  ))}
                </div>
              </Section>
              {(d.mounts.length > 0 || d.networks.length > 0) && (
                <Section title="Mounts and networks">
                  {d.mounts.map((m, i) => (
                    <code key={i} className={MONO}>{`${m.source || m.type} → ${m.destination}${m.rw ? "" : " (read-only)"}`}</code>
                  ))}
                  {d.networks.length > 0 && <span className={LBL}>Networks: {d.networks.join(", ")}</span>}
                </Section>
              )}
            </div>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose}>Close</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

/** A workload's crashes, newest first. */
export function CrashList({ workload, canOperate }: { workload: string; canOperate: boolean }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["crash-snapshots", workload], queryFn: () => listCrashSnapshots(workload), refetchInterval: 30_000 });
  const [open, setOpen] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: (s: CrashSnapshot) => deleteCrashSnapshot(s.id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["crash-snapshots"] }),
    onError: (err) => notify({ color: "red", title: "Could not delete", message: errorMessage(err, "") }),
  });
  const items = list.data ?? [];
  return (
    <Band card title="Crashes" count={list.data ? items.length : undefined} right={<span className={LBL}>Saved when a container dies with an error</span>}>
      {list.isLoading && <p className={`m-0 p-[16px] ${LBL}`}>Loading…</p>}
      {list.data && !items.length && (
        <p className="m-0 p-[16px] text-[14px] leading-[20px] text-muted-foreground">
          No crashes. When a container of this workload dies with an error and nobody stopped it, Kowan saves its last logs, configuration and
          environment here, before a restart replaces it.
        </p>
      )}
      {items.map((s) => (
        <div key={s.id} className="flex flex-wrap items-center gap-[10px] p-[12px_16px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
          <span className="text-[14px] leading-[20px] font-semibold">{s.containerName}</span>
          <Pill tone="r">{exitReason(s.exitCode, s.oomKilled)}</Pill>
          {s.repeats > 0 && <Tag>+{s.repeats} more</Tag>}
          <span className={LBL} title={formatDate(s.createdAt)}>{timeAgo(s.createdAt)}</span>
          <div className="ml-auto flex gap-[6px]">
            {canOperate && <Btn size="sm" danger onClick={() => remove.mutate(s)} aria-label={`Delete the crash of ${timeAgo(s.createdAt)}`}>Delete</Btn>}
            <Btn size="sm" solid onClick={() => setOpen(s.id)} aria-label={`Open the crash of ${s.containerName}, ${timeAgo(s.createdAt)}`}>Open</Btn>
          </div>
        </div>
      ))}
      {open && <CrashDetail id={open} onClose={() => setOpen(null)} />}
    </Band>
  );
}

/** How many crashes a workload has (for its tab), 0 while unknown. */
export function useCrashCount(workload: string): number {
  const q = useQuery({ queryKey: ["crash-snapshots", workload], queryFn: () => listCrashSnapshots(workload), refetchInterval: 30_000 });
  return q.data?.length ?? 0;
}
