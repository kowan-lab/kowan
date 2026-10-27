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
 * A titled card: the heading band (title, description, actions) sits inside
 * the frame, then the body. Tables inside it run edge to edge and drop their
 * own frame.
 */
export default function SectionCard({ title, description, actions, noBodyPadding = false, hideHeaderDivider = false, plain = false, children }: SectionCardProps) {
  const hasHeader = !plain && Boolean(title || description || actions);
  return (
    <section className="min-w-0 bg-card shadow-[inset_0_0_0_1px_var(--border)]">
      {hasHeader && (
        <div className={cn("flex min-h-[52px] flex-wrap items-center justify-between gap-[12px] px-[16px] py-[10px]", !hideHeaderDivider && "shadow-[inset_0_-1px_0_var(--border)]")}>
          <div className="flex min-w-0 flex-col">
            {title && (typeof title === "string" ? <h2 className="m-0 text-[16px] leading-[24px] font-semibold tracking-normal text-foreground">{title}</h2> : title)}
            {description && <p className="m-0 text-[13px] leading-[18px] text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-[8px]">{actions}</div>}
        </div>
      )}
      <InPanel.Provider value={true}>
        <div className={cn("flex min-w-0 flex-col", !noBodyPadding && "gap-[16px] p-[16px]")}>{children}</div>
      </InPanel.Provider>
    </section>
  );
}
