import { useEffect, useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Button, Callout, Checkbox, Popup } from "@/ui/kit";
import { getAgentUpdateStatus, getPlatformUpdate, rollingAgentUpdate } from "@/api/client";
import type { Host } from "@/types";
import { errorMessage } from "@/utils";
import { notify } from "@/ui/toast";
import { UpdateResultPill } from "./UpdatePanel";

/**
 * Rolling agent update: pick agents (those with a newer image are preselected),
 * then the server updates them one at a time and stops at the first failure.
 * Progress is shown per host while the dialog stays open.
 */
export default function RollingUpdateDialog({ open, onOpenChange, hosts }: { open: boolean; onOpenChange: (o: boolean) => void; hosts: Host[] }) {
  const qc = useQueryClient();
  const agents = hosts.filter((h) => h.id !== "local" && h.online);
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
      setIds(Object.fromEntries(order.map((h, i) => [h, r.ids[i]]).filter(([, id]) => id)));
    },
    onError: (e) => notify({ color: "red", title: "Could not start the rolling update", message: errorMessage(e, "Nothing was changed.") }),
  });

  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setIds({});
      setPicked(new Set());
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
            Agents are updated one at a time. Each one is health-checked and rolled back automatically if it fails, and the rollout stops at the first failure.
          </p>
          {agents.length === 0 ? (
            <Callout color="gray">No online agents to update.</Callout>
          ) : (
            <ul className="m-0 flex list-none flex-col border p-0">
              {agents.map((h, i) => {
                const st = statuses[i]?.data;
                const runIdx = Object.keys(ids).indexOf(h.id);
                const run = runIdx >= 0 ? runs[runIdx]?.data : undefined;
                return (
                  <li key={h.id} className="flex items-center gap-5 border-b px-5 py-3 text-[13px] last:border-0">
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
