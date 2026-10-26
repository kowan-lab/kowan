import type { ReactNode } from "react";
import { Skeleton } from "@/ui/kit";

export interface StatCardProps {
  label: string;
  value: ReactNode;
  /** Accepted for compatibility; stats no longer draw an icon chip. */
  icon?: ReactNode;
  color?: string;
  hint?: ReactNode;
  loading?: boolean;
}

/**
 * One cell of a stat strip: label, a mono figure and an optional hint. Place
 * several inside a <StatStrip> so they share hairlines instead of floating as
 * separate cards.
 */
export default function StatCard({ label, value, hint, loading = false }: StatCardProps) {
  return (
    <div className="flex min-w-0 flex-col gap-2 px-6 py-5">
      <span className="text-[12px] font-medium tracking-[0.06em] text-muted-foreground font-mono uppercase">{label}</span>
      {loading ? (
        <Skeleton width={72} height={26} />
      ) : (
        <span className="font-mono text-[24px] leading-11 font-semibold tracking-[-0.02em] tabular-nums">{value}</span>
      )}
      {hint && <span className="truncate text-xs text-muted-foreground">{hint}</span>}
    </div>
  );
}

/** A framed row of stats separated by hairlines; wraps on narrow screens. */
export function StatStrip({ children }: { children: ReactNode }) {
  return (
    <div className="grid grid-cols-2 shadow-[inset_0_0_0_1px_var(--border)] bg-card [&>*:first-child]:pl-[var(--page-px,16px)] md:grid-cols-[repeat(auto-fit,minmax(170px,1fr))] [&>*]:border-border [&>*:not(:last-child)]:border-r max-md:[&>*:nth-child(2n)]:border-r-0 max-md:[&>*:nth-child(n+3)]:border-t">
      {children}
    </div>
  );
}
