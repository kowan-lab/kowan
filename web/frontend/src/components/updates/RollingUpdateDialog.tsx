import { useEffect, useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button, Callout, Checkbox, Popup } from "@/ui/kit";
import { getAgentUpdateStatus, getPlatformUpdate, rollingAgentUpdate } from "@/api/client";
import type { Host, PlatformUpdate } from "@/types";
import { errorMessage } from "@/utils";
import { notify } from "@/ui/toast";
import { UpdateResultPill } from "./UpdatePanel";

/** The steps of an agent update in order, for a progress estimate. */
const STEPS = ["queued", "lock", "pull", "preflight", "launch", "stop", "create", "start", "verify", "commit"];
const STEP_LABEL: Record<string, string> = {
  queued: "Queued", lock: "Starting", resolve: "Starting", pull: "Pulling the image", compare: "Pulling the image",
  preflight: "Testing the new image", snapshot: "Testing the new image", platform: "Testing the new image",
  launch: "Handing over", stop: "Restarting", park: "Restarting", create: "Restarting", start: "Restarting",
  inspect: "Checking health", verify: "Checking health", wait: "Checking health",
  commit: "Finishing", cleanup: "Finishing", rollback: "Rolling back", restore: "Rolling back",
};

/** How far a run is (0..1) and what it is doing now. */
export function runProgress(run: PlatformUpdate): { value: number; label: string } {
  if (run.status !== "running") return { value: 1, label: "" };
  const last = run.phases[run.phases.length - 1]?.phase ?? "queued";
  const reached = run.phases.reduce((m, p) => Math.max(m, STEPS.indexOf(p.phase)), 0);
  return { value: Math.max(0.05, (reached + 1) / (STEPS.length + 1)), label: STEP_LABEL[last] ?? last };
}

function Bar({ value, tone }: { value: number; tone: "run" | "ok" | "bad" }) {
  return (
    <span aria-hidden className="relative block h-[4px] w-full overflow-hidden bg-muted">
      <i
        className={
          "absolute inset-y-0 left-0 transition-[width] duration-500 motion-reduce:transition-none " +
          (tone === "ok" ? "bg-[var(--tone-green-solid)]" : tone === "bad" ? "bg-[var(--tone-red-solid)]" : "bg-[var(--signal)]")
        }
        style={{ width: `${Math.round(value * 100)}%` }}
      />
    </span>
  );
}

/**
 * Agent update: pick agents (those with a newer image are preselected), then
 * the server updates them all at the same time; each one is health-checked and
 * rolled back on its own. Progress is shown per host while the dialog stays
 * open, and the list stays as it was when the update started (an agent that
 * restarts drops offline for a moment).
 */
export default function RollingUpdateDialog({ open, onOpenChange, hosts }: { open: boolean; onOpenChange: (o: boolean) => void; hosts: Host[] }) {
  const qc = useQueryClient();
  const online = hosts.filter((h) => h.id !== "local" && h.online);
  // Once started, the list is frozen: a restarting agent is briefly offline.
  const [frozen, setFrozen] = useState<Host[] | null>(null);
  const agents = frozen ?? online;
  const statuses = useQueries({
    queries: agents.map((h) => ({ queryKey: ["agent-update", h.id], queryFn: () => getAgentUpdateStatus(h.id), staleTime: 30_000, enabled: open })),
  });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [ids, setIds] = useState<Record<string, string>>({});

  // Preselect agents that have a newer image, once their status is known.
  useEffect(() => {
    if (!open) return;
    setPicked((cur) => {
      if (cur.size) return cur;
      const next = new Set<string>();
      agents.forEach((h, i) => statuses[i]?.data?.updateAvailable && next.add(h.id));
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, statuses.map((s) => s.dataUpdatedAt).join()]);

  const runs = useQueries({
    queries: Object.entries(ids).map(([, id]) => ({
      queryKey: ["platform-update", id],
      queryFn: () => getPlatformUpdate(id),
      retry: false,
      refetchInterval: (q: { state: { data?: { status: string } } }) => (q.state.data && q.state.data.status !== "running" ? false : 2000),
    })),
  });
  const started = Object.keys(ids).length > 0;
  const done = started && runs.every((r) => r.data && r.data.status !== "running");

  useEffect(() => {
    if (done) {
      void qc.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("agent-update") });
      void qc.invalidateQueries({ queryKey: ["hosts"] });
    }
  }, [done, qc]);

  const start = useMutation({
    mutationFn: () => rollingAgentUpdate([...picked]),
    onSuccess: (r) => {
      const order = [...picked];
      setFrozen(online);
      setIds(Object.fromEntries(order.map((h, i) => [h, r.ids[i]]).filter(([, id]) => id)));
    },
    onError: (e) => notify({ color: "red", title: "Could not start the rolling update", message: errorMessage(e, "Nothing was changed.") }),
  });

  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setIds({});
      setPicked(new Set());
      setFrozen(null);
    }
  };

  return (
    <Popup open={open} onOpenChange={close} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Update agents</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <p className="m-0 text-[13px]">
            Agents are updated at the same time. Each one is health-checked and rolled back automatically if it fails; a failure does not stop the others.
          </p>
          {started && (() => {
            const total = Object.keys(ids).length;
            const finished = runs.filter((r) => r.data && r.data.status !== "running").length;
            const failed = runs.filter((r) => r.data && r.data.status !== "running" && r.data.status !== "updated").length;
            const value = runs.reduce((sum, r) => sum + (r.data ? runProgress(r.data).value : 0), 0) / Math.max(1, total);
            return (
              <div className="flex flex-col gap-[6px]" role="progressbar" aria-label="Update progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={finished}>
                <div className="flex items-baseline justify-between text-[13px]">
                  <span className="font-semibold">{done ? (failed ? `${total - failed} of ${total} updated` : `All ${total} updated`) : `Updating ${total} agent${total === 1 ? "" : "s"}`}</span>
                  <span className="text-muted-foreground">{finished} of {total} done</span>
                </div>
                <Bar value={done ? 1 : value} tone={done ? (failed ? "bad" : "ok") : "run"} />
              </div>
            );
          })()}
          {agents.length === 0 ? (
            <Callout color="gray">No online agents to update.</Callout>
          ) : (
            <ul className="m-0 flex list-none flex-col border p-0">
              {agents.map((h, i) => {
                const st = statuses[i]?.data;
                const runIdx = Object.keys(ids).indexOf(h.id);
                const run = runIdx >= 0 ? runs[runIdx]?.data : undefined;
                return (
                  <li key={h.id} className="flex flex-col gap-[6px] border-b px-5 py-3 text-[13px] last:border-0">
                    <div className="flex items-center gap-5">
                    <Checkbox
                      aria-label={`Update ${h.name || h.id}`}
                      checked={picked.has(h.id)}
                      disabled={started || !st?.supported}
                      onChange={(e) => {
                        const next = new Set(picked);
                        if (e.target.checked) next.add(h.id);
                        else next.delete(h.id);
                        setPicked(next);
                      }}
                    />
                    <span className="min-w-0 flex-1 truncate font-medium">{h.name || h.id}</span>
                    {run ? (
                      <UpdateResultPill status={run.status} />
                    ) : runIdx >= 0 ? (
                      <span className="text-muted-foreground">queued</span>
                    ) : statuses[i]?.isLoading ? (
                      <Loader2 className="size-[12px] animate-spin text-muted-foreground" />
                    ) : !st?.supported ? (
                      <span className="text-muted-foreground">not updatable</span>
                    ) : st.updateAvailable ? (
                      <span className="text-[var(--tone-amber-text)]">update available</span>
                    ) : (
                      <span className="text-muted-foreground">up to date</span>
                    )}
                    </div>
                    {run && (() => {
                      const p = runProgress(run);
                      return (
                        <div className="flex items-center gap-[10px] pl-[26px]">
                          <span className="min-w-0 flex-1"><Bar value={p.value} tone={run.status === "running" ? "run" : run.status === "updated" ? "ok" : "bad"} /></span>
                          <span className="w-[150px] shrink-0 truncate text-right text-[12px] text-muted-foreground">
                            {run.status === "running" ? p.label : run.error || (run.toVersion ? `now ${run.toVersion}` : "")}
                          </span>
                        </div>
                      );
                    })()}
                  </li>
                );
              })}
            </ul>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={() => close(false)}>{done ? "Close" : "Cancel"}</Button>
          {!started && (
            <Button disabled={picked.size === 0} loading={start.isPending} onClick={() => start.mutate()}>
              Update {picked.size || ""} agent{picked.size === 1 ? "" : "s"}
            </Button>
          )}
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
