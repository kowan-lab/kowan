// Small presentational pieces of the Apps surfaces, drawn to mockup D's
// measurements (d-fleet-console.html): pills, tags, buttons, section heads,
// crumbs and facts. Every 1px rule is an inset shadow; every surface is solid.

import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { listHosts } from "../../api/client";
import { isAllHosts } from "../../HostContext";
import { cn } from "@/lib/utils";
import type { AppTone } from "../../lib/apps";

const TONE_VAR: Record<AppTone, string> = { g: "green", a: "amber", r: "red", n: "gray" };

/** D's `.lbl`: mono uppercase panel label. */
export const LBL = "font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] uppercase text-muted-foreground";

/** D's state pill: mono uppercase, coloured hairline, a 4px dot. */
export function Pill({ tone, children, signal, className }: { tone: AppTone; children: ReactNode; signal?: boolean; className?: string }) {
  const t = signal ? "var(--signal)" : `var(--tone-${TONE_VAR[tone]}-solid)`;
  const tt = signal ? "var(--link)" : tone === "n" ? "var(--muted-foreground)" : `var(--tone-${TONE_VAR[tone]}-text)`;
  return (
    <span
      className={cn(
        "inline-flex h-[20px] shrink-0 items-center gap-[6px] px-[7px] align-top font-mono text-[12px] leading-none font-semibold tracking-[0.04em] whitespace-nowrap uppercase",
        className,
      )}
      style={{
        color: tt,
        background: `color-mix(in srgb, ${t} 10%, var(--card))`,
        boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${t} 45%, var(--card))`,
      }}
    >
      <i aria-hidden className="size-[4px] shrink-0 rounded-full" style={{ background: t }} />
      {children}
    </span>
  );
}

/** D's `.tag`: a quiet mono token (ports, ids). */
export function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex h-[20px] items-center bg-card px-[6px] align-top font-mono text-[12px] leading-none font-medium whitespace-nowrap text-muted-foreground shadow-[inset_0_0_0_1px_var(--border)]", className)}>
      {children}
    </span>
  );
}

/** Round status dot for a host. */
export function Dot({ tone, size = 8 }: { tone: AppTone; size?: 6 | 8 }) {
  return (
    <span
      aria-hidden
      className={cn("inline-block shrink-0 rounded-full", size === 6 ? "size-[6px]" : "size-[8px]")}
      style={{ background: `var(--tone-${TONE_VAR[tone]}-solid)` }}
    />
  );
}

/** D's `.chip`: a host chip with its status dot. */
export function HostChip({ name, online }: { name: string; online: boolean }) {
  return (
    <span className="inline-flex h-[20px] items-center gap-[6px] bg-[var(--popover)] px-[6px] align-top font-mono text-[12px] leading-none font-semibold whitespace-nowrap text-foreground shadow-[inset_0_0_0_1px_var(--border)]">
      <Dot tone={online ? "g" : "r"} size={6} />
      {name}
    </span>
  );
}

const BTN_BASE =
  "inline-flex shrink-0 items-center gap-[6px] font-sans font-semibold whitespace-nowrap no-underline outline-none select-none transition-transform duration-[60ms] focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-[var(--signal)] disabled:cursor-not-allowed disabled:opacity-45 aria-disabled:cursor-not-allowed aria-disabled:opacity-45";

export function btnClass({ solid, size = "md", icon, ghost, danger }: { solid?: boolean; size?: "md" | "sm"; icon?: boolean; ghost?: boolean; danger?: boolean } = {}) {
  return cn(
    BTN_BASE,
    "text-[13px] leading-none",
    size === "sm" ? "h-[28px]" : "h-[32px]",
    icon ? (size === "sm" ? "w-[28px] justify-center px-0" : "w-[32px] justify-center px-0") : size === "sm" ? "px-[10px]" : "px-[12px]",
    solid
      ? "bg-primary text-primary-foreground hover:opacity-90"
      : ghost
        ? "bg-transparent text-foreground hover:bg-muted"
        : "bg-[var(--popover)] text-foreground shadow-[inset_0_0_0_1px_var(--border)] hover:bg-muted hover:shadow-[inset_0_0_0_1px_var(--input)]",
    danger && "text-[var(--tone-red-text)]",
  );
}

type BtnProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  solid?: boolean;
  size?: "md" | "sm";
  icon?: boolean;
  ghost?: boolean;
  danger?: boolean;
};

/** D's `.btn` (outline), `.btn.solid` (graphite ink), `.btn.sm`, `.btn.icon`, `.btn.ghost`. */
export const Btn = forwardRef<HTMLButtonElement, BtnProps>(function Btn(
  { solid, size, icon, ghost, danger, className, type = "button", ...rest },
  ref,
) {
  return <button ref={ref} type={type} className={cn(btnClass({ solid, size, icon, ghost, danger }), className)} {...rest} />;
});

export function BtnLink({
  to, children, solid, size, icon, ghost, className, ...rest
}: { to: string; children: ReactNode; solid?: boolean; size?: "md" | "sm"; icon?: boolean; ghost?: boolean; className?: string; "aria-label"?: string; title?: string }) {
  return (
    <Link to={to} className={cn(btnClass({ solid, size, icon, ghost }), className)} {...rest}>
      {children}
    </Link>
  );
}

/** D's `.sect`: a standalone 44px section header between groups of cards. */
export function SectHead({ title, count, right }: { title: string; count?: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex min-h-[44px] items-center gap-[12px] bg-card px-[16px] shadow-[inset_0_0_0_1px_var(--border)]">
      <h2 className="m-0 text-[14px] leading-[18px] font-bold tracking-[0.02em] uppercase">{title}</h2>
      {count != null && <span className={LBL}>{count}</span>}
      {right && <div className="ml-auto flex items-center gap-[8px]">{right}</div>}
    </div>
  );
}

/** D's `.band` with its `.band-h` head. */
export function Band({ title, count, right, children, className }: { title?: ReactNode; count?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("min-w-0 bg-card shadow-[inset_0_0_0_1px_var(--border)]", className)}>
      {title != null && (
        <div className="flex min-h-[44px] items-center gap-[12px] px-[16px] shadow-[inset_0_-1px_0_var(--border)]">
          {typeof title === "string" ? <h2 className="m-0 text-[14px] leading-[18px] font-bold tracking-[0.02em] uppercase">{title}</h2> : title}
          {count != null && <span className={LBL}>{count}</span>}
          {right && <div className="ml-auto flex items-center gap-[8px]">{right}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/**
 * D's breadcrumb trail: the scope first (the host selection, with its dot),
 * then the path. Mono uppercase 12/20.
 */
export function Crumbs({ hostScope, parts }: { hostScope: string; parts: { label: string; to?: string }[] }) {
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts }).data ?? [];
  const all = isAllHosts(hostScope);
  const host = hosts.find((h) => h.id === hostScope);
  return (
    <nav aria-label="Breadcrumb" className="flex flex-wrap items-center gap-[8px] font-mono text-[12px] leading-[20px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">
      <span className="inline-flex items-center gap-[6px] text-foreground" title="Scope: change it in the host menu">
        {all ? (
          <span className="inline-flex items-center gap-[3px]">
            {hosts.map((h) => (
              <Dot key={h.id} tone={h.online ? "g" : "r"} size={6} />
            ))}
          </span>
        ) : (
          <Dot tone={host ? (host.online ? "g" : "r") : "n"} size={6} />
        )}
        {all ? "All hosts" : host?.name || hostScope}
      </span>
      {parts.map((p, i) => (
        <span key={`${p.label}-${i}`} className="inline-flex items-center gap-[8px]">
          <span aria-hidden className="text-[var(--input)]">/</span>
          {p.to ? (
            <Link to={p.to} className="text-muted-foreground no-underline hover:text-foreground hover:underline">{p.label}</Link>
          ) : (
            <span aria-current="page">{p.label}</span>
          )}
        </span>
      ))}
    </nav>
  );
}

/** One fact of the app header's facts row. */
export function Fact({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div className="mr-[20px] pr-[20px] shadow-[inset_-1px_0_0_var(--border)] last:shadow-none">
      <div className={LBL}>{label}</div>
      <div className={cn("mt-[2px] flex items-center gap-[6px]", mono ? "font-mono text-[13px] leading-[20px] font-medium" : "text-[14px] leading-[20px] font-semibold")}>
        {children}
      </div>
    </div>
  );
}

/** D's callout (problem banner). */
export function CalloutBar({ tone, icon, children, actions }: { tone: "r" | "a" | "info"; icon?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  const t = tone === "r" ? "var(--tone-red-solid)" : tone === "a" ? "var(--tone-amber-solid)" : "";
  return (
    <div
      role={tone === "info" ? undefined : "status"}
      className="flex flex-wrap items-center gap-[12px] px-[16px] py-[12px] text-[14px] leading-[20px]"
      style={
        tone === "info"
          ? { background: "var(--card)", boxShadow: "inset 0 0 0 1px var(--border)", color: "var(--muted-foreground)" }
          : {
              background: `color-mix(in srgb, ${t} ${tone === "r" ? 7 : 10}%, var(--card))`,
              boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${t} ${tone === "r" ? 40 : 50}%, var(--card))`,
              color: tone === "r" ? "var(--tone-red-text)" : "var(--tone-amber-text)",
            }
      }
    >
      {icon && <span className="inline-flex shrink-0">{icon}</span>}
      <div className="min-w-0 flex-1 text-foreground">{children}</div>
      {actions && <div className="flex gap-[8px]">{actions}</div>}
    </div>
  );
}

/** Container state squares (D's `.sqs`): one 12px square per container. */
export function StateSquares({ tones, titles, size = 12 }: { tones: AppTone[]; titles?: string[]; size?: 12 | 8 }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-[4px]">
      {tones.map((t, i) => (
        <span
          key={i}
          title={titles?.[i]}
          className={cn("inline-block shrink-0", size === 8 ? "size-[8px]" : "size-[12px]")}
          style={t === "n" ? { boxShadow: "inset 0 0 0 1px var(--input)" } : { background: `var(--tone-${TONE_VAR[t]}-solid)` }}
        />
      ))}
    </span>
  );
}
