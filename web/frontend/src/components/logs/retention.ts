// Shared bits of the retained-logs UI: retention choices and workload names.

import type { Deployment, Host } from "../../types";
import { parseAppId } from "../../lib/apps";

export const RETENTION_DAYS = [
  { value: "1", label: "1 day" },
  { value: "7", label: "7 days" },
  { value: "14", label: "14 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
];

export function daysLabel(days: number): string {
  return days === 1 ? "1 day" : `${days} days`;
}

/** A readable name and place for a workload id (dep~… / stack~… / ctr~…). */
export function workloadName(
  id: string,
  deployments: Deployment[] | undefined,
  hosts: Host[] | undefined,
): { name: string; where: string } {
  const p = parseAppId(id);
  const hostName = (h: string) => hosts?.find((x) => x.id === h)?.name ?? h;
  if (!p) return { name: id, where: "" };
  if (p.kind === "deployment") {
    const d = deployments?.find((x) => x.id === p.deploymentId);
    return { name: d?.name ?? p.deploymentId.slice(0, 8), where: d?.targets?.[0] ? hostName(d.targets[0]) : "deployment" };
  }
  if (p.kind === "stack") return { name: p.project, where: hostName(p.hostId) };
  return { name: p.name, where: hostName(p.hostId) };
}
