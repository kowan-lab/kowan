import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Dot, FRAME, type Tone } from "./ui";

/** D's page head: scope crumb, the title, a mono summary line and actions, in one framed panel. */
export function OverviewHeader({
  scope, scopeTone, title, objectTitle, summary, actions,
}: {
  scope: string;
  scopeTone?: Tone | "mixed";
  title: string;
  /** Object pages (one host) show the name as typed, not uppercased. */
  objectTitle?: boolean;
  summary: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className={cn(FRAME, "px-[16px] pt-[14px]")}>
      <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-[8px] font-mono text-[12px] leading-[20px] font-semibold tracking-[0.06em] uppercase text-muted-foreground">
        <span className="inline-flex items-center gap-[6px] text-foreground">
          {scopeTone === "mixed" ? (
            <span aria-hidden className="inline-flex items-center gap-[3px]"><Dot tone="green" size={6} /><Dot tone="amber" size={6} /><Dot tone="red" size={6} /></span>
          ) : scopeTone ? <Dot tone={scopeTone} /> : null}
          {scope}
        </span>
        <span aria-hidden className="text-input">/</span>
        <span aria-current="page">Overview</span>
      </nav>
      <div className="mt-[6px] flex flex-wrap items-end gap-[16px] pb-[16px]">
        <h1
          className={cn(
            "m-0 font-sans text-[24px] leading-[32px] tracking-[-0.02em] text-foreground",
            objectTitle ? "font-[650]" : "font-extrabold uppercase",
          )}
        >
          {title}
        </h1>
        <div className="flex flex-wrap gap-[16px] pb-[6px]">{summary}</div>
        {actions && <div className="ml-auto flex flex-wrap gap-[8px] pb-[2px]">{actions}</div>}
      </div>
    </section>
  );
}

export function SumItem({ n, label }: { n?: ReactNode; label: ReactNode }) {
  return (
    <span className="font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] uppercase text-muted-foreground">
      {n != null && <b className="font-semibold text-foreground">{n}</b>}
      {n != null && " "}
      {label}
    </span>
  );
}
