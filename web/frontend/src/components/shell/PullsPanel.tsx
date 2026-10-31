import { Progress } from "@/ui/kit";
import { useHosts } from "@/lib/hostsQuery";
import { pullPercent, usePulls, usePullsDuring, type PullProgress } from "@/lib/pulls";
import { formatBytes } from "@/utils";

/** One pull: reference, bar, bytes and layers, or how it ended. */
export function PullLine({ pull, host }: { pull: PullProgress; host?: string }) {
  const pct = pullPercent(pull);
  const detail =
    pull.status === "error" ? pull.message
      : pull.status === "done" ? pull.message || "Pulled"
        : [
            pull.total > 0 ? `${formatBytes(pull.current)} / ${formatBytes(pull.total)}` : "",
            pull.layers ? `${pull.layersDone}/${pull.layers} layers` : "",
          ].filter(Boolean).join(" · ") || pull.message || "Starting";
  return (
    <div className="flex min-w-0 flex-col gap-[6px]" aria-label={`Pull of ${pull.ref}`}>
      <div className="flex min-w-0 items-baseline gap-[8px]">
        <span className="min-w-0 flex-1 truncate font-mono text-[13px] leading-[18px] font-medium text-foreground" title={pull.ref}>{pull.ref}</span>
        {host && <span className="shrink-0 font-mono text-[12px] leading-[16px] text-muted-foreground">{host}</span>}
      </div>
      <Progress value={pct ?? 0} color={pull.status === "error" ? "danger" : pull.status === "done" ? "success" : "info"} />
      <span
        className="truncate font-mono text-[12px] leading-[16px]"
        style={{ color: pull.status === "error" ? "var(--tone-red-text)" : "var(--muted-foreground)" }}
        title={detail}
      >
        {pct !== null && pull.status === "pulling" ? `${Math.floor(pct)}% · ` : ""}{detail}
      </span>
    </div>
  );
}

/** The pulls an action started on `hostIds` while it runs, one line each. */
export function PullsDuring({ hostIds, active }: { hostIds: string[]; active: boolean }) {
  const pulls = usePullsDuring(hostIds, active);
  if (!pulls.length) return null;
  return (
    <div className="flex flex-col gap-[10px]" aria-label="Pulls in progress">
      {pulls.map((p) => <PullLine key={p.id} pull={p} />)}
    </div>
  );
}

/** Every pull in flight, in a corner of every page. */
export default function PullsPanel() {
  const pulls = usePulls();
  const hosts = useHosts();
  if (!pulls.length) return null;
  const name = (id: string) => (hosts.data ?? []).find((h) => h.id === id)?.name ?? id;
  return (
    <section
      aria-label="Image pulls"
      aria-live="polite"
      className="fixed right-[16px] bottom-[16px] z-40 flex w-[min(360px,calc(100vw-32px))] flex-col gap-[12px] bg-card p-[14px_16px] shadow-[inset_0_0_0_1px_var(--border),0_8px_24px_rgb(0_0_0/0.12)]"
    >
      <span className="font-mono text-[12px] leading-[16px] font-medium tracking-[0.04em] text-muted-foreground uppercase">
        Pulling {pulls.filter((p) => p.status === "pulling").length || ""} image{pulls.length === 1 ? "" : "s"}
      </span>
      {pulls.slice(-4).map((p) => <PullLine key={p.id} pull={p} host={name(p.hostId)} />)}
    </section>
  );
}
