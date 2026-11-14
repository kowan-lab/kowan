import { prune, type PruneKind } from "../api/client";
import { ALL_HOSTS } from "../HostContext";
import { formatBytes, errorMessage } from "../utils";
import type { Host } from "../types";

export interface PruneOutcome {
  reclaimed: number;
  failed: string[];
  hosts: number;
}

/**
 * Prune one host, or every online host one after the other when the target
 * is ALL_HOSTS (the "All hosts" view). A host that fails is reported and the
 * others still go; it throws only when every host failed.
 */
export async function pruneOn(target: string, hosts: Host[], kind: PruneKind, all?: boolean): Promise<PruneOutcome> {
  const targets = target === ALL_HOSTS ? hosts.filter((h) => h.online) : [hosts.find((h) => h.id === target) ?? { id: target, name: target }];
  let reclaimed = 0;
  const failed: string[] = [];
  for (const h of targets) {
    try {
      reclaimed += (await prune(h.id, kind, all)).spaceReclaimed ?? 0;
    } catch (err) {
      failed.push(`${h.name || h.id}: ${errorMessage(err, "could not prune")}`);
    }
  }
  if (!targets.length) throw new Error("No online host to prune");
  if (failed.length === targets.length) throw new Error(failed.join("\n"));
  return { reclaimed, failed, hosts: targets.length };
}

/** The toast for a prune: what it freed, on how many hosts, and which failed. */
export function pruneNotice(what: string, o: PruneOutcome, showBytes = true): { color: "green" | "yellow"; title: string; message: string } {
  const freed = showBytes ? `Reclaimed ${formatBytes(o.reclaimed)}` : `Removed unused ${what}`;
  const where = o.hosts > 1 ? ` on ${o.hosts} hosts` : "";
  if (o.failed.length) {
    return { color: "yellow", title: `${what[0].toUpperCase()}${what.slice(1)} pruned, ${o.failed.length} host${o.failed.length === 1 ? "" : "s"} failed`,
      message: [freed + where, ...o.failed].join("\n") };
  }
  return { color: "green", title: `${what[0].toUpperCase()}${what.slice(1)} pruned`, message: freed + where };
}
