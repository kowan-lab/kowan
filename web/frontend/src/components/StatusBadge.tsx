import type { ReactNode } from "react";
import { StatePill } from "@/ui/kit";

export interface StatusBadgeProps {
  /** The raw status/state string. */
  status: string;
  /** Optional label override; defaults to the humanized status. */
  value?: string;
  /** Optional detail after the divider: exit code, uptime, duration. */
  detail?: ReactNode;
  size?: "sm" | "md" | "lg";
}

/** State pill used everywhere a status is shown, so colours stay consistent. */
export default function StatusBadge({ status, value, detail, size = "sm" }: StatusBadgeProps) {
  return <StatePill status={status} label={value} detail={detail} size={size === "lg" ? "md" : size} />;
}
