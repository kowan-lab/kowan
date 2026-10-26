import { StatePill, Tooltip, type Color } from "@/ui/kit";
import type { ImageScan, ScanCounts, ScanSeverity } from "../../types";
import { formatDate } from "../../utils";

/** One colour per severity; colour carries meaning only (DESIGN.md). */
export const SEVERITY_COLOR: Record<ScanSeverity, Color> = {
  CRITICAL: "red",
  HIGH: "orange",
  MEDIUM: "amber",
  LOW: "gray",
  UNKNOWN: "gray",
};
const ORDER: { key: keyof ScanCounts; sev: ScanSeverity; short: string }[] = [
  { key: "critical", sev: "CRITICAL", short: "C" },
  { key: "high", sev: "HIGH", short: "H" },
  { key: "medium", sev: "MEDIUM", short: "M" },
  { key: "low", sev: "LOW", short: "L" },
];

export function SeverityPill({ severity }: { severity: ScanSeverity }) {
  return <StatePill status={severity.toLowerCase()} label={severity.toLowerCase()} color={SEVERITY_COLOR[severity]} size="sm" />;
}

/**
 * Compact severity counts (critical / high / medium / low) as state pills.
 * `compact` hides zero buckets (list rows); a fully clean image shows one
 * green "clean" pill so "scanned, nothing found" differs from "not scanned".
 */
export function SeverityChips({ counts, compact = false }: { counts: ScanCounts; compact?: boolean }) {
  const total = counts.critical + counts.high + counts.medium + counts.low;
  if (total === 0) return <StatePill status="clean" label="clean" color="green" size="sm" />;
  return (
    <span className="inline-flex flex-nowrap items-center gap-2">
      {ORDER.filter((o) => !compact || counts[o.key] > 0).map((o) => (
        <StatePill
          key={o.key}
          status={`${o.key}-${counts[o.key]}`}
          label={compact ? `${o.short} ${counts[o.key]}` : `${o.key} ${counts[o.key]}`}
          color={counts[o.key] > 0 ? SEVERITY_COLOR[o.sev] : "gray"}
          size="sm"
        />
      ))}
    </span>
  );
}

/** Chips for a stored scan, or the scan's state (scanning / failed / none). */
export function ScanChips({ scan, running, compact = true, emptyLabel }: {
  scan: ImageScan | null | undefined;
  running?: boolean;
  compact?: boolean;
  emptyLabel?: string;
}) {
  if (running) return <StatePill status="scanning" label="scanning" color="blue" size="sm" />;
  if (!scan) return emptyLabel ? <span className="text-xs text-muted-foreground">{emptyLabel}</span> : null;
  if (scan.status !== "ok") {
    return (
      <Tooltip content={scan.error || "Scan failed"}>
        <span tabIndex={0} className="inline-flex">
          <StatePill status="failed" label="scan failed" color="red" size="sm" />
        </span>
      </Tooltip>
    );
  }
  const tip = `Trivy ${scan.scannerVersion || ""} · ${scan.total} finding${scan.total === 1 ? "" : "s"}, ${scan.fixable} fixable · ${formatDate(scan.scannedAt)}`;
  return (
    <Tooltip content={tip}>
      <span tabIndex={0} className="inline-flex">
        <SeverityChips counts={scan.counts} compact={compact} />
      </span>
    </Tooltip>
  );
}
