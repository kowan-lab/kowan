import type { ReactNode } from "react";
import { useHost } from "../HostContext";
import { Crumbs } from "./apps/ui";

export interface Breadcrumb { label: string; to?: string }
export interface PageHeaderProps {
  title: ReactNode; description?: ReactNode; icon?: ReactNode;
  actions?: ReactNode; breadcrumbs?: Breadcrumb[];
}

/**
 * Every page's header card, the same frame as the Workloads list's: the trail
 * (scope, parents, this page, with the up-one-level button), the title with
 * the actions beside it, then the description.
 */
export default function PageHeader({ title, description, actions, breadcrumbs }: PageHeaderProps) {
  const { hostId } = useHost();
  const parents = (breadcrumbs ?? []).filter((c) => c.to);
  const here = typeof title === "string" ? title : breadcrumbs?.filter((c) => !c.to).slice(-1)[0]?.label;
  const parts = [...parents, ...(here ? [{ label: here }] : [])];
  return (
    <header className="min-w-0 bg-card px-[16px] pt-[14px] pb-[16px] shadow-[inset_0_0_0_1px_var(--border)]">
      <Crumbs hostScope={hostId} parts={parts} />
      <div className="mt-[6px] flex flex-wrap items-center gap-[16px]">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-[12px]">
          {typeof title === "string" ? (
            <h1 className="m-0 min-w-0 text-[24px] leading-[32px] font-extrabold tracking-[-0.02em] uppercase [overflow-wrap:anywhere]">{title}</h1>
          ) : (
            <div className="flex min-w-0 flex-wrap items-center gap-[12px] [&_h1]:m-0 [&_h1]:text-[24px] [&_h1]:leading-[32px] [&_h1]:font-extrabold [&_h1]:tracking-[-0.02em] [&_h1]:uppercase [&>*>span:first-child]:text-[24px] [&>*>span:first-child]:leading-[32px] [&>*>span:first-child]:font-extrabold [&>*>span:first-child]:tracking-[-0.02em] [&>*>span:first-child]:uppercase">{title}</div>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-[8px]">{actions}</div>}
      </div>
      {description && <div className="max-w-[80ch] pt-[6px] text-[13px] leading-[20px] text-muted-foreground">{description}</div>}
    </header>
  );
}
