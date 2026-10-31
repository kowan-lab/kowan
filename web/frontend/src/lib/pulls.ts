import { useEffect, useRef, useSyncExternalStore } from "react";

/**
 * Image pulls in flight on every host, whoever started them (the Pull button,
 * an update, a deploy, the auto-updater): the server reads each pull's Docker
 * progress and sends a summary over the host's events WebSocket (`type=pull`,
 * kowan/docker/pulls.py), which components/shell/useDockerEvents.ts feeds in
 * here. An ended pull is kept a few seconds so its outcome can be read.
 */
export interface PullProgress {
  type: "pull";
  id: string;
  hostId: string;
  ref: string;
  status: "pulling" | "done" | "error";
  /** Last Docker status line, or the error. */
  message: string;
  /** Bytes downloaded of `total`, over the layers whose size is known. */
  current: number;
  total: number;
  layers: number;
  layersDone: number;
  /** Unix ms. */
  startedAt: number;
}

const KEEP_MS = 6000;
let pulls: PullProgress[] = [];
const listeners = new Set<() => void>();
const timers = new Map<string, number>();

function emit() {
  for (const l of listeners) l();
}

export function recordPull(p: PullProgress): void {
  pulls = [...pulls.filter((x) => x.id !== p.id), p].sort((a, b) => a.startedAt - b.startedAt);
  if (p.status !== "pulling" && !timers.has(p.id)) {
    timers.set(p.id, window.setTimeout(() => {
      timers.delete(p.id);
      pulls = pulls.filter((x) => x.id !== p.id);
      emit();
    }, KEEP_MS));
  }
  emit();
}

/** The pulls of every host, oldest first. */
export function usePulls(): PullProgress[] {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => pulls,
  );
}

/** 0–100, or null while no layer size is known yet. */
export function pullPercent(p: PullProgress): number | null {
  if (p.status === "done") return 100;
  if (p.total > 0) return (p.current / p.total) * 100;
  if (p.layers > 0) return (p.layersDone / p.layers) * 100;
  return null;
}

/**
 * The pulls an action started: while `active`, the pulls on `hostIds` that
 * were not there when it began (an update, a redeploy). Empty when inactive.
 */
export function usePullsDuring(hostIds: string[], active: boolean): PullProgress[] {
  const all = usePulls();
  const before = useRef<Set<string> | null>(null);
  // Taken during render, so the first render of the action already has it.
  if (active && before.current === null) before.current = new Set(all.map((p) => p.id));
  useEffect(() => {
    if (!active) before.current = null;
  }, [active]);
  if (!active || before.current === null) return [];
  const seen = before.current;
  return all.filter((p) => hostIds.includes(p.hostId) && !seen.has(p.id));
}
