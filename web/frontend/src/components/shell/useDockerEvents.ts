import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { eventsWebSocketURL, getMyPermissions } from "@/api/client";
import { useHosts } from "@/lib/hostsQuery";
import { setEventsLive } from "@/lib/live";
import { recordPull, type PullProgress } from "@/lib/pulls";

/**
 * Push instead of poll: one Docker events WebSocket per online host (a team
 * member, whom the server refuses Docker events, gets one for their own image
 * pulls only). Each event
 * invalidates only the query families it affects (batched every 250 ms), so
 * lists update the moment something changes and idle tabs cost nothing.
 * Polling stays on as a slow safety net (lib/live.ts).
 */
const FAMILIES: Record<string, string[]> = {
  container: ["containers", "container-inspect", "container-spec", "system", "deployment", "deployments", "metrics"],
  image: ["images", "image-inspect", "system", "disk-usage"],
  volume: ["volumes", "vol-inspect", "volexp-browse", "disk-usage"],
  network: ["networks", "network-inspect"],
};

export function useDockerEvents(): void {
  const qc = useQueryClient();
  const hosts = useHosts();
  // Docker events are host-wide: team-scoped users are refused by the server,
  // so they stay on polling instead of retrying a socket that can never open.
  const perms = useQuery({ queryKey: ["me-permissions"], queryFn: getMyPermissions, staleTime: 300_000, retry: false });
  const allowed = perms.isSuccess ? perms.data.mode !== "scoped" : perms.isError;
  const decided = perms.isSuccess || perms.isError;
  const online = (hosts.data ?? []).filter((h) => h.online).map((h) => h.id).sort();
  // Team members still follow their own image pulls: a pull-only socket.
  const types = allowed ? "container,image,volume,network,pull" : "pull";
  const key = decided && online.length ? `${types}|${online.join(",")}` : "";

  const pending = useRef(new Set<string>());
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (!key) {
      setEventsLive(false);
      return;
    }
    const [want, list] = key.split("|");
    const ids = list.split(",");
    const docker = want !== "pull";
    const open = new Set<string>();
    const sockets = new Map<string, WebSocket>();
    const retries = new Map<string, number>();
    let disposed = false;

    const flush = () => {
      timer.current = null;
      const fams = [...pending.current];
      pending.current.clear();
      void qc.invalidateQueries({
        predicate: (q) => {
          const head = String(q.queryKey[0]);
          return fams.some((f) => head === f || head.startsWith(`${f}:`));
        },
      });
    };
    const schedule = (fams: string[]) => {
      for (const f of fams) pending.current.add(f);
      if (timer.current === null) timer.current = window.setTimeout(flush, 250);
    };
    // Lists poll fast unless Docker events push their changes.
    const updateLive = () => setEventsLive(docker && open.size === ids.length);

    const connect = (id: string) => {
      if (disposed) return;
      const ws = new WebSocket(eventsWebSocketURL(id, { type: want }));
      sockets.set(id, ws);
      ws.onopen = () => {
        open.add(id);
        retries.set(id, 0);
        updateLive();
      };
      ws.onmessage = (m) => {
        try {
          const ev = JSON.parse(m.data as string) as { type?: string };
          // Progress of an image pull (kowan/docker/pulls.py), not a Docker event.
          if (ev.type === "pull") {
            recordPull(ev as PullProgress);
            return;
          }
          const fams = FAMILIES[ev.type ?? ""];
          if (fams) schedule(fams);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        open.delete(id);
        updateLive();
        if (disposed) return;
        // Reconnect with capped backoff; a host going offline shows up in /hosts.
        const n = (retries.get(id) ?? 0) + 1;
        retries.set(id, n);
        window.setTimeout(() => connect(id), Math.min(30_000, 1000 * 2 ** Math.min(n, 5)));
        void qc.invalidateQueries({ queryKey: ["hosts"] });
      };
    };
    ids.forEach(connect);

    return () => {
      disposed = true;
      setEventsLive(false);
      for (const ws of sockets.values()) ws.close();
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [key, qc]);
}
