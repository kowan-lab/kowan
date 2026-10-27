import type { ReactNode } from "react";
import { Crumbs, LBL } from "../apps/ui";

/** The overview's header card, the same frame as the Workloads list's. */
export function OverviewHeader({ scope, title, summary, facts, actions }: {
  /** The host scope for the breadcrumb (a host id, or All hosts). */
  scope: string;
  title: string;
  summary: ReactNode;
  /** A second row of label/value facts under the title. */
  facts?: [string, ReactNode][];
  actions?: ReactNode;
}) {
  return (
    <section className="bg-card px-[16px] pt-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
      <Crumbs hostScope={scope} parts={[{ label: "Overview" }]} />
      <div className={facts ? "mt-[6px] flex flex-wrap items-end gap-[16px]" : "mt-[6px] flex flex-wrap items-end gap-[16px] pb-[16px]"}>
        <h1 className="m-0 min-w-0 truncate text-[24px] leading-[32px] font-extrabold tracking-[-0.02em] uppercase">{title}</h1>
        <div className="flex flex-wrap gap-[16px] pb-[6px]">{summary}</div>
        {actions && <div className="ml-auto flex flex-wrap gap-[8px] pb-[2px]">{actions}</div>}
      </div>
      {facts && (
        <div className="flex flex-wrap gap-x-[24px] gap-y-[4px] pt-[10px] pb-[16px]">
          {facts.map(([k, v]) => (
            <span key={k} className={LBL}>
              {k} <b className="font-semibold text-foreground">{v}</b>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

export function SumItem({ n, label }: { n?: ReactNode; label: ReactNode }) {
  return (
    <span className={LBL}>
      {n != null && <><b className="font-semibold text-foreground">{n}</b> </>}
      {label}
    </span>
  );
}
