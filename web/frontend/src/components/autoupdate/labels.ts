import type { Color } from "@/ui/kit";

export const MODE_OPTIONS = [
  { value: "off", label: "Off", description: "No automatic checks" },
  { value: "notify", label: "Notify", description: "Alert when a newer image exists" },
  { value: "pull", label: "Pull", description: "Pull the new image in advance, do not switch" },
  { value: "apply", label: "Apply", description: "Switch automatically, verify, roll back on failure" },
];

export const SCAN_OPTIONS = [
  { value: "never", label: "Never block" },
  { value: "critical", label: "Block on critical" },
  { value: "high", label: "Block on critical or high" },
  { value: "worse", label: "Block if worse than current" },
];

export function modeLabel(mode: string): string {
  return MODE_OPTIONS.find((o) => o.value === mode)?.label ?? mode;
}
export function scanLabel(v: string): string {
  return SCAN_OPTIONS.find((o) => o.value === v)?.label ?? v;
}

/** Tone of an auto-update result/outcome pill. */
export function outcomeColor(outcome: string): Color {
  switch (outcome) {
    case "updated": case "up_to_date": case "current":
      return "green";
    case "rolled_back": case "blocked": case "waiting": case "skipped":
      return "amber";
    case "failed": case "error":
      return "red";
    case "notified": case "available": case "pulled":
      return "blue";
    default:
      return "gray";
  }
}

export function outcomeText(outcome: string): string {
  return outcome.replace(/_/g, " ");
}
