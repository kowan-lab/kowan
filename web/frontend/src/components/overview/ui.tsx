import type { ReactNode } from "react";
import { Link } from "react-router";
import { cn } from "@/lib/utils";

/*
 * Overview building blocks, measured from mockup D (d-fleet-console.html):
 * framed solid panels separated by the page gap, 44px section heads, 32/28px
 * flat buttons, 20px mono pills and host chips. Values are D's, in whole
 * pixels; 1px rules are inset shadows so they never change a box's size.
 *
 * Colour mapping from D's tokens to the theme's:
 *   surface → --card · raised → --popover · sunk → --muted · rule → --input
 *   ink → --primary · on-ink → --primary-foreground · green/amber/red → --tone-*-solid|text
 */

export type Tone = "green" | "amber" | "red" | "gray";
export type Sev = "critical" | "warning" | "waiting";

const toneStyle = (t: Tone) =>
  ({
    "--t": `var(--tone-${t}-solid)`,
    "--tt": t === "gray" ? "var(--muted-foreground)" : `var(--tone-${t}-text)`,
  }) as React.CSSProperties;

export const RULE_B = "shadow-[inset_0_-1px_0_var(--border)]";
export const FRAME = "bg-card shadow-[inset_0_0_0_1px_var(--border)]";

/* ── icons (D's 16px line set and 12px severity glyphs) ───────────────── */
export function SevGlyph({ sev, className }: { sev: Sev; className?: string }) {
  const c = cn("size-[12px] shrink-0", className);
  if (sev === "critical")
    return <svg viewBox="0 0 12 12" aria-hidden className={c}><path d="M6 1 11 10.5H1Z" fill="currentColor" /></svg>;
  if (sev === "warning")
    return <svg viewBox="0 0 12 12" aria-hidden className={c}><rect x="2" y="2" width="8" height="8" fill="currentColor" /></svg>;
  return <svg viewBox="0 0 12 12" aria-hidden className={c}><circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" /></svg>;
}

export function OkIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden className={cn("size-[16px] shrink-0 fill-none stroke-current [stroke-linecap:square] [stroke-width:1.5]", className)}>
      <path d="M5 12l5 5 9-10" />
    </svg>
  );
}

export function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden className="size-[16px] shrink-0 fill-none stroke-current [stroke-linecap:square] [stroke-width:1.5]">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

/* ── severity label: CRITICAL / WARNING / WAITING ─────────────────────── */
const SEV_TEXT: Record<Sev, string> = {
  critical: "text-[var(--tone-red-text)]",
  warning: "text-[var(--tone-amber-text)]",
  waiting: "text-muted-foreground",
};

export function SevLabel({ sev }: { sev: Sev }) {
  return (
    <span className={cn("inline-flex items-center gap-[6px] font-mono text-[12px] leading-none font-bold tracking-[0.06em] uppercase", SEV_TEXT[sev])}>
      <SevGlyph sev={sev} />
      {sev}
    </span>
  );
}

/* ── state pill (D .pill): 20px, mono 12, tone hairline ───────────────── */
export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      style={toneStyle(tone)}
      className="inline-flex h-[20px] items-center gap-[6px] px-[7px] align-top font-mono text-[12px] leading-none font-semibold tracking-[0.04em] whitespace-nowrap uppercase text-[var(--tt)] bg-[color-mix(in_srgb,var(--t)_10%,var(--card))] shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--t)_45%,var(--card))]"
    >
      <i aria-hidden className="size-[4px] shrink-0 rounded-full bg-[var(--t)]" />
      {children}
    </span>
  );
}

/* ── host dot and chip ────────────────────────────────────────────────── */
export function Dot({ tone, size = 8 }: { tone: Tone; size?: 6 | 8 }) {
  return (
    <span
      aria-hidden
      style={toneStyle(tone)}
      className={cn("inline-block shrink-0 rounded-full bg-[var(--t)]", size === 6 ? "size-[6px]" : "size-[8px]")}
    />
  );
}

export function HostChip({ name, online }: { name: string; online: boolean }) {
  return (
    <span className="inline-flex h-[20px] items-center gap-[6px] px-[6px] align-top bg-popover font-mono text-[12px] leading-none font-semibold whitespace-nowrap text-foreground shadow-[inset_0_0_0_1px_var(--border)]">
      <Dot tone={online ? "green" : "red"} size={6} />
      <span className="sr-only">{online ? "online host" : "offline host"} </span>
      {name}
    </span>
  );
}

/* ── buttons (D .btn): flat, 32px or 28px, inset hairline ─────────────── */
interface BtnProps {
  children: ReactNode;
  to?: string;
  onClick?: () => void;
  solid?: boolean;
  sm?: boolean;
  disabled?: boolean;
  busy?: boolean;
  title?: string;
}

export function Btn({ children, to, onClick, solid, sm, disabled, busy, title }: BtnProps) {
  const cls = cn(
    "inline-flex shrink-0 items-center gap-[6px] font-sans text-[13px] leading-none font-semibold whitespace-nowrap no-underline transition-transform duration-[60ms] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
    sm ? "h-[28px] px-[10px]" : "h-[32px] px-[12px]",
    solid
      ? "bg-primary text-primary-foreground hover:opacity-90"
      : "bg-popover text-foreground shadow-[inset_0_0_0_1px_var(--border)] hover:bg-muted hover:shadow-[inset_0_0_0_1px_var(--input)]",
    (disabled || busy) && "pointer-events-none opacity-45",
  );
  if (to) {
    return <Link to={to} className={cls} title={title} onClick={(e) => e.stopPropagation()}>{children}</Link>;
  }
  return (
    <button type="button" className={cls} title={title} disabled={disabled || busy} aria-busy={busy || undefined} onClick={(e) => { e.stopPropagation(); onClick?.(); }}>
      {children}
    </button>
  );
}

/* ── section head (D .sect / .band-h) ─────────────────────────────────── */
export function SectHead({ title, label, right, standalone }: { title: ReactNode; label?: ReactNode; right?: ReactNode; standalone?: boolean }) {
  return (
    <div className={cn("flex min-h-[44px] items-center gap-[12px] px-[16px]", standalone ? FRAME : RULE_B)}>
      <h2 className="m-0 font-sans text-[14px] leading-[18px] font-bold tracking-[0.02em] uppercase">{title}</h2>
      {label != null && <Lbl>{label}</Lbl>}
      {right && <div className="ml-auto flex items-center gap-[8px]">{right}</div>}
    </div>
  );
}

function Lbl({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] uppercase text-muted-foreground", className)}>{children}</span>;
}

/* ── meters ───────────────────────────────────────────────────────────── */
/** D .bar: 56×6 track, ink fill; turns amber/red only when the value means it. */
export function Bar({ value }: { value: number }) {
  const v = Math.max(0, Math.min(100, value));
  const fill = v >= 90 ? "bg-[var(--tone-red-solid)]" : v >= 75 ? "bg-[var(--tone-amber-solid)]" : "bg-foreground";
  return (
    <span aria-hidden className="relative inline-block h-[6px] w-[56px] shrink-0 bg-muted align-middle shadow-[inset_0_0_0_1px_var(--border)]">
      <i className={cn("absolute inset-y-0 left-0", fill)} style={{ width: `${v}%` }} />
    </span>
  );
}

/** D .seg: one 4×12 tick per container, coloured by state. */
export function Seg({ states }: { states: Tone[] }) {
  const color: Record<Tone, string> = {
    green: "bg-[var(--tone-green-solid)]",
    amber: "bg-[var(--tone-amber-solid)]",
    red: "bg-[var(--tone-red-solid)]",
    gray: "bg-border",
  };
  return (
    <span aria-hidden className="inline-flex gap-[2px] align-middle">
      {states.map((t, i) => <b key={i} className={cn("h-[12px] w-[4px]", color[t])} />)}
    </span>
  );
}
