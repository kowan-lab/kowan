import { StatePill } from "@/ui/kit";
import type { GitOpsAction, GitOpsStatus } from "../../types";

const SYNC: Record<string, { label: string; color: "green" | "amber" | "red" | "gray" }> = {
  in_sync: { label: "In sync", color: "green" },
  drift: { label: "Drift", color: "amber" },
  error: { label: "Error", color: "red" },
  "": { label: "Not checked", color: "gray" },
};

export function SyncStatusPill({ status, detail }: { status: GitOpsStatus; detail?: string }) {
  const s = SYNC[status] ?? SYNC[""];
  return <StatePill status={status || "unknown"} label={s.label} color={s.color} detail={detail} />;
}

const ACTION: Record<GitOpsAction, { label: string; color: "green" | "amber" | "red" | "gray" | "blue" }> = {
  create: { label: "Create", color: "green" },
  update: { label: "Update", color: "amber" },
  delete: { label: "Delete", color: "red" },
  noop: { label: "No change", color: "gray" },
  orphan: { label: "Release", color: "gray" },
  forget: { label: "Forget", color: "gray" },
};

export function ActionPill({ action, adopt }: { action: GitOpsAction; adopt?: boolean }) {
  const a = ACTION[action] ?? ACTION.noop;
  return <StatePill status={action} label={adopt && action === "noop" ? "Adopt" : a.label} color={a.color} size="sm" detail={adopt && action !== "noop" ? "adopt" : undefined} />;
}

export function shortSha(sha: string): string {
  return sha ? sha.slice(0, 8) : "";
}
