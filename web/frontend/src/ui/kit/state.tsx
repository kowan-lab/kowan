import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { toneVars, type Color } from "./tone";

/**
 * Map a state string (container, host, deployment, run, alert level…) onto a
 * tone. One table for the whole app, so "running" is the same green everywhere.
 */
export function stateTone(status: string): Color {
  switch (status.trim().toLowerCase()) {
    case "running": case "up": case "active": case "online": case "deployed": case "success":
    case "succeeded": case "healthy": case "available": case "enabled": case "connected": case "ok":
    case "resolved":
      return "green";
    case "paused": case "warning": case "degraded": case "partial": case "restarting":
    case "pending": case "queued": case "starting": case "deploying": case "in_progress":
    case "in-progress": case "updating": case "firing":
      return "amber";
    case "created": case "new": case "draft": case "info":
      return "blue";
    case "dead": case "error": case "failed": case "failure": case "critical": case "unhealthy":
    case "removing": case "destroyed":
      return "red";
    default:
      return "gray";
  }
}

function humanize(s: string): string {
  const c = s.trim().replace(/[_-]+/g, " ");
  return c ? c.charAt(0).toUpperCase() + c.slice(1) : s;
}

/**
 * The console's signature state pill: mono label with a coloured hairline, an
 * optional detail (exit code, duration) after a divider, and one pulse of the
 * border when the state changes while on screen.
 */
export function StatePill({
  status, label, detail, color, className, style,
}: {
  status: string;
  label?: ReactNode;
  detail?: ReactNode;
  color?: Color;
  size?: "sm" | "md";
  className?: string;
  style?: CSSProperties;
}) {
  const tone = color ?? stateTone(status);
  const prev = useRef(status);
  const [beat, setBeat] = useState(0);
  useEffect(() => {
    if (prev.current !== status) {
      prev.current = status;
      setBeat((b) => b + 1);
    }
  }, [status]);
  return (
    <span
      key={beat}
      className={cn(
        "inline-flex max-w-full items-center gap-[6px] align-top leading-[18px] font-medium tracking-normal bg-[var(--t-soft)] font-sans whitespace-nowrap text-[var(--t-text)] ",
        "min-h-[24px] px-[8px] text-[12px]",
        beat > 0 && "animate-[pill-beat_900ms_ease-out_1]",
        className,
      )}
      style={{ ...toneVars(tone), ...style }}
    >
      <span className="truncate">{label ?? humanize(status)}</span>
      {detail != null && detail !== "" && (
        <>
          <span aria-hidden className="h-5 w-px bg-[var(--t-line)]" />
          <span className="normal-case opacity-80">{detail}</span>
        </>
      )}
    </span>
  );
}
