import type { ReactNode } from "react";
import { InPanel } from "@/ui/kit";
import { cn } from "@/lib/utils";

export interface SectionCardProps {
  title?: ReactNode;
  description?: ReactNode;
  /** Accepted for compatibility; panels no longer draw a title icon. */
  icon?: ReactNode;
  /** Right-aligned header content (counts, filters, actions). */
  actions?: ReactNode;
  /** Full-bleed body, for tables whose rows meet the panel edges. */
  noBodyPadding?: boolean;
  hideHeaderDivider?: boolean;
  /** Drop the header: the page title already names this section and the table
   *  toolbar shows the row count. */
  plain?: boolean;
  children: ReactNode;
}

/**
 * A titled section: a full-bleed band between hairlines, not a boxed card.
 * Tables inside it run edge to edge and drop their own frame.
 */
export default function SectionCard({ title, description, actions, noBodyPadding = false, hideHeaderDivider = false, plain = false, children }: SectionCardProps) {
  const hasHeader = !plain && Boolean(title || description || actions);
  return (
    <section className="shadow-[inset_0_0_0_1px_var(--border)] bg-card">
      {hasHeader && (
        <div className={cn("flex min-h-22 flex-wrap items-center justify-between gap-x-6 gap-y-3 px-8 py-3", !hideHeaderDivider && "shadow-[inset_0_-1px_0_var(--border)]")}>
          <div className="flex min-w-0 flex-col">
            {title && (typeof title === "string" ? <h2 className="m-0 text-[14px] leading-[18px] font-bold tracking-[0.02em] text-foreground uppercase">{title}</h2> : title)}
            {description && <p className="m-0 text-[13px] leading-[18px] text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-3">{actions}</div>}
        </div>
      )}
      <InPanel.Provider value={true}>
        <div className={cn("flex min-w-0 flex-col", !noBodyPadding && "gap-6 px-8 py-8")}>{children}</div>
      </InPanel.Provider>
    </section>
  );
}
