import {
  createElement,
  type CSSProperties,
  type ElementType,
  type HTMLAttributes,
  type ReactNode,
  type Ref,
} from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { toneVars, type Color } from "./tone";

/* ── spacing ──────────────────────────────────────────────────────────────
 * Numeric spacing follows a compact 3px step (1 = 3px … 6 = 18px), matching the scale
 * the pages were written against. Strings pass through as CSS lengths. */
const NAMED: Record<string, string> = { xs: "4px", sm: "6px", md: "10px", lg: "12px", xl: "18px" };
function space(v: number | string | undefined): string | undefined {
  if (v === undefined) return undefined;
  if (typeof v === "number") return `${2 * Math.floor((v * 3 + 1) / 2)}px`; // even whole pixels only
  return NAMED[v] ?? v;
}
function len(v: number | string | undefined): string | undefined {
  if (v === undefined) return undefined;
  return typeof v === "number" ? `${v}px` : v;
}

/* ── Box ─────────────────────────────────────────────────────────────────*/
export interface BoxProps extends HTMLAttributes<HTMLElement> {
  as?: ElementType;
  display?: "flex" | "grid" | "block" | "inline-flex" | "inline-block" | "none" | "contents";
  direction?: "row" | "column" | "row-reverse" | "column-reverse";
  align?: "start" | "center" | "end" | "stretch" | "baseline";
  justify?: "start" | "center" | "end" | "between" | "around" | "evenly";
  wrap?: boolean | "wrap" | "nowrap";
  gap?: number | string;
  padding?: number | string;
  px?: number | string;
  py?: number | string;
  width?: number | string;
  height?: number | string;
  fullWidth?: boolean;
  fullHeight?: boolean;
  minWidth?: number | string;
  maxWidth?: number | string;
  grow?: boolean | number;
  shrink?: boolean | number;
  flex?: number | string;
  ref?: Ref<HTMLElement>;
}

const ALIGN = { start: "flex-start", end: "flex-end", center: "center", stretch: "stretch", baseline: "baseline" };
const JUSTIFY = { start: "flex-start", end: "flex-end", center: "center", between: "space-between", around: "space-around", evenly: "space-evenly" };

export function Box({
  as = "div", display = "flex", direction, align, justify, wrap, gap, padding, px, py,
  width, height, fullWidth, fullHeight, minWidth, maxWidth, grow, shrink, flex,
  style, children, ...rest
}: BoxProps) {
  const s: CSSProperties = {
    display,
    flexDirection: display.includes("flex") ? direction ?? "row" : undefined,
    alignItems: align ? ALIGN[align] : undefined,
    justifyContent: justify ? JUSTIFY[justify] : undefined,
    flexWrap: wrap === undefined ? undefined : wrap === true ? "wrap" : wrap === false ? "nowrap" : wrap,
    gap: space(gap),
    padding: space(padding),
    paddingInline: space(px),
    paddingBlock: space(py),
    width: len(width) ?? (fullWidth ? "100%" : undefined),
    height: len(height) ?? (fullHeight ? "100%" : undefined),
    minWidth: len(minWidth),
    maxWidth: len(maxWidth),
    marginInline: maxWidth !== undefined && maxWidth !== "100%" ? "auto" : undefined,
    flexGrow: grow === true ? 1 : grow === false ? undefined : grow,
    flexShrink: shrink === false ? 0 : shrink === true ? 1 : shrink,
    flex,
    ...style,
  };
  return createElement(as, { ...rest, style: s }, children);
}

/* ── Text ────────────────────────────────────────────────────────────────*/
type TextVariant = "body" | "heading" | "title" | "subheading" | "caption" | "label" | "display";
type TextSize = "xs" | "sm" | "base" | "md" | "lg" | "xl" | "2xl" | "3xl";
export interface TextProps extends Omit<HTMLAttributes<HTMLElement>, "color"> {
  as?: ElementType;
  variant?: TextVariant;
  size?: TextSize;
  weight?: "regular" | "medium" | "semibold" | "bold";
  color?: Color | "default" | "muted" | "subtle" | "inherit";
  align?: "left" | "center" | "right";
  transform?: "uppercase" | "lowercase" | "capitalize" | "none";
  italic?: boolean;
  mono?: boolean;
  tabularNums?: boolean;
  truncate?: boolean;
  lines?: number;
  ref?: Ref<HTMLElement>;
}

const VARIANT: Record<TextVariant, string> = {
  display: "text-3xl font-semibold tracking-tight",
  heading: "text-xl font-semibold tracking-tight",
  title: "text-base font-semibold",
  subheading: "text-sm font-semibold",
  body: "text-sm",
  caption: "text-xs",
  label: "text-xs font-medium",
};
const SIZE: Record<TextSize, string> = {
  xs: "text-xs", sm: "text-[13px]", base: "text-sm", md: "text-sm", lg: "text-base",
  xl: "text-lg", "2xl": "text-xl", "3xl": "text-2xl",
};
const WEIGHT = { regular: "font-normal", medium: "font-medium", semibold: "font-semibold", bold: "font-bold" };

export function Text({
  as, variant = "body", size, weight, color = "default", align, transform, italic, mono,
  tabularNums, truncate, lines, className, style, children, ...rest
}: TextProps) {
  const tag = as ?? (variant === "heading" || variant === "title" || variant === "display" ? "p" : "span");
  const toned = !["default", "muted", "subtle", "inherit"].includes(color);
  return createElement(
    tag,
    {
      ...rest,
      className: cn(
        "m-0",
        VARIANT[variant],
        size && SIZE[size],
        weight && WEIGHT[weight],
        color === "muted" && "text-muted-foreground",
        color === "subtle" && "text-muted-foreground/80",
        color === "inherit" && "text-inherit",
        toned && "text-[var(--t-text)]",
        align === "left" && "text-left",
        align === "center" && "text-center",
        align === "right" && "text-right",
        transform === "uppercase" && "font-sans",
        transform === "lowercase" && "lowercase",
        transform === "capitalize" && "capitalize",
        italic && "italic",
        (mono || tabularNums) && "tabular-nums",
        mono && "font-mono",
        truncate && !lines && "block truncate",
        lines && "overflow-hidden",
        className,
      ),
      style: {
        ...(toned ? toneVars(color) : {}),
        ...(lines
          ? { display: "-webkit-box", WebkitLineClamp: lines, WebkitBoxOrient: "vertical" as const }
          : {}),
        ...style,
      },
    },
    children,
  );
}

/* ── Divider ─────────────────────────────────────────────────────────────*/
export function Divider({ label, className, style }: { label?: ReactNode; className?: string; style?: CSSProperties }) {
  if (!label) return <hr className={cn("my-2 border-0 border-t border-border", className)} style={style} />;
  return (
    <div className={cn("flex items-center gap-5 py-2", className)} style={style} role="separator">
      <span className="text-xs font-semibold font-sans  tracking-normal text-muted-foreground whitespace-nowrap">
        {label}
      </span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

/* ── Code ────────────────────────────────────────────────────────────────*/
export function Code({
  block, color, className, style, children,
}: { block?: boolean; color?: Color; className?: string; style?: CSSProperties; children?: ReactNode }) {
  const toned = color ? toneVars(color) : undefined;
  if (block) {
    return (
      <pre
        className={cn(
          "m-0 overflow-auto rounded-md border bg-muted/60 p-5 font-mono text-xs leading-[20px] whitespace-pre-wrap break-all",
          color && "text-[var(--t-text)]",
          className,
        )}
        style={{ ...toned, ...style }}
      >
        <code>{children}</code>
      </pre>
    );
  }
  return (
    <code
      className={cn(
        "rounded bg-muted px-[4px] py-[2px] font-mono text-[0.85em] break-all",
        color && "text-[var(--t-text)]",
        className,
      )}
      style={{ ...toned, ...style }}
    >
      {children}
    </code>
  );
}

/* ── Skeleton / Spinner / Progress ───────────────────────────────────────*/
export function Skeleton({ width, height = 16, className, style }: { width?: number | string; height?: number | string; className?: string; style?: CSSProperties }) {
  return (
    <div
      aria-hidden
      className={cn("skeleton", className)}
      style={{ width: len(width) ?? "100%", height: len(height), ...style }}
    />
  );
}

export function Spinner({ size = "md", label = "Loading" }: { size?: "xs" | "sm" | "md" | "lg"; label?: string }) {
  const px = { xs: 12, sm: 16, md: 20, lg: 28 }[size];
  return (
    <span role="status" aria-label={label} className="inline-flex text-muted-foreground">
      <Loader2 width={px} height={px} className="animate-spin" />
    </span>
  );
}

export function Progress({ value = 0, color }: { value?: number; color?: Color | string }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(v)}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-[4px] w-full overflow-hidden rounded-full bg-muted"
      style={toneVars(color, "primary")}
    >
      <div className="h-full rounded-full bg-[var(--t-solid)] " style={{ width: `${v}%` }} />
    </div>
  );
}
