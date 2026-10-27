import {
  createContext,
  forwardRef,
  useContext,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Loader2, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { renderIcon } from "./icon";
import { toneVars, type Color } from "./tone";
import { copyText } from "../../lib/clipboard";

/* ── Button ──────────────────────────────────────────────────────────────
 * Variants: solid (default), light (soft tint), outline, clear (ghost).
 * A `color` tints the button; without one, solid uses the primary action
 * colour and the others stay neutral. */
export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "color"> {
  variant?: "solid" | "light" | "outline" | "clear" | "link";
  color?: Color;
  size?: "xs" | "sm" | "md" | "lg";
  iconLeft?: string | ReactNode;
  iconRight?: string | ReactNode;
  loading?: boolean;
  fullWidth?: boolean;
  href?: string;
  target?: string;
  rel?: string;
}

const BTN_SIZE = {
  xs: "h-16 px-5 text-[13px] gap-3",
  sm: "h-18 px-5 text-[13px] gap-3",
  md: "h-20 px-6 text-[13px] gap-3",
  lg: "h-22 px-7 text-[14px] gap-4",
};
const ICON_ONLY = { xs: "w-16 px-0", sm: "w-18 px-0", md: "w-20 px-0", lg: "w-22 px-0" };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "solid", color, size = "md", iconLeft, iconRight, loading, fullWidth, href, target,
    rel, disabled, className, style, children, type = "button", onClick, ...rest
  },
  ref,
) {
  // Solid buttons stay monochrome unless the colour carries meaning (danger,
  // success, warning). Brand-ish colours from older call sites collapse to primary.
  const MEANING = ["danger", "critical", "red", "success", "green", "warning", "yellow", "orange"];
  if (variant === "solid" && color && !MEANING.includes(color)) color = undefined;
  const neutral = !color || color === "neutral" || color === "gray";
  const tone = color && !neutral ? toneVars(color) : toneVars("primary");
  const iconOnly = children == null || children === false;
  const cls = cn(
    "inline-flex shrink-0 items-center justify-center font-semibold whitespace-nowrap select-none",
    "transition-[background-color,border-color,color] duration-100 outline-none",
    "focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal disabled:pointer-events-none disabled:opacity-50",
    BTN_SIZE[size],
    iconOnly && ICON_ONLY[size],
    variant === "solid" && !neutral && color && "bg-[var(--t-solid)] text-[var(--t-on)] hover:brightness-110",
    variant === "solid" && neutral && color && "bg-foreground text-background hover:bg-foreground/90",
    variant === "solid" && !color && "bg-primary text-primary-foreground hover:bg-primary/85",
    variant === "light" && !neutral && "bg-[var(--t-soft)] text-[var(--t-text)] hover:bg-[var(--t-line)]",
    variant === "light" && neutral && "bg-muted text-foreground hover:bg-accent",
    variant === "outline" && "border border-input bg-card hover:bg-accent",
    variant === "outline" && !neutral && "border-[var(--t-line)] text-[var(--t-text)]",
    variant === "clear" && !neutral && "text-[var(--t-text)] hover:bg-[var(--t-soft)]",
    variant === "clear" && neutral && "text-muted-foreground hover:bg-accent hover:text-foreground",
    variant === "link" && "h-auto px-0 text-[var(--t-text)] underline-offset-4 hover:underline",
    fullWidth && "w-full",
    className,
  );
  const content = (
    <>
      {loading ? <Loader2 className="size-6 animate-spin" aria-hidden /> : renderIcon(iconLeft, size === "lg" ? "md" : "sm")}
      {children}
      {renderIcon(iconRight, "sm")}
    </>
  );
  if (href) {
    return (
      <a href={href} target={target} rel={rel ?? (target === "_blank" ? "noreferrer" : undefined)} className={cls} style={{ ...tone, ...style }}>
        {content}
      </a>
    );
  }
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cls}
      style={{ ...tone, ...style }}
      onClick={onClick as (e: MouseEvent<HTMLButtonElement>) => void}
      {...rest}
    >
      {content}
    </button>
  );
});

/* ── Badge ───────────────────────────────────────────────────────────────*/
export interface BadgeProps {
  color?: Color | string;
  variant?: "light" | "solid" | "outline";
  size?: "sm" | "md" | "lg";
  pill?: boolean;
  dot?: boolean;
  iconLeft?: string | ReactNode;
  onDismiss?: () => void;
  className?: string;
  style?: CSSProperties;
  title?: string;
  children?: ReactNode;
}

export function Badge({
  color = "gray", variant = "light", size = "md", dot, iconLeft, onDismiss, className, style, title, children,
}: BadgeProps) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex max-w-full items-center gap-2 rounded-md align-top font-medium whitespace-nowrap",
        size === "sm" ? "h-8 px-[4px] text-[12px]" : size === "lg" ? "h-11 px-[8px] text-[13px]" : "h-9 px-3 text-xs",
        variant === "light" && "bg-[var(--t-soft)] text-[var(--t-text)]",
        variant === "solid" && "bg-[var(--t-solid)] text-[var(--t-on)]",
        variant === "outline" && "border border-[var(--t-line)] bg-card text-[var(--t-text)]",
        className,
      )}
      style={{ ...toneVars(color), ...style }}
    >
      {dot && <span aria-hidden className="size-[4px] shrink-0 rounded-full bg-[var(--t-solid)]" />}
      {renderIcon(iconLeft, "xs")}
      <span className="truncate">{children}</span>
      {onDismiss && (
        <button type="button" aria-label="Remove" onClick={onDismiss} className="-mr-2 rounded p-[2px] hover:bg-black/10">
          <X className="size-5" />
        </button>
      )}
    </span>
  );
}

/* ── Callout ─────────────────────────────────────────────────────────────*/
export interface CalloutProps {
  color?: Color;
  variant?: "light" | "outline";
  title?: ReactNode;
  iconLeft?: string | ReactNode;
  onDismiss?: () => void;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}

const DEFAULT_ICON: Record<string, string> = {
  danger: "error-outline-rounded", red: "error-outline-rounded", critical: "error-outline-rounded",
  warning: "warning-outline-rounded", yellow: "warning-outline-rounded", orange: "warning-outline-rounded",
  success: "check-circle-outline-rounded", green: "check-circle-outline-rounded",
};

export function Callout({ color = "info", title, iconLeft, onDismiss, className, style, children }: CalloutProps) {
  const danger = ["danger", "red", "critical"].includes(color);
  return (
    <div
      role={danger ? "alert" : "status"}
      className={cn(
        "flex gap-5 rounded-lg bg-[var(--t-soft)] px-8 py-8 text-sm",
        className,
      )}
      style={{ ...toneVars(color), ...style }}
    >
      <span className="pt-[2px] text-[var(--t-text)]">{renderIcon(iconLeft ?? DEFAULT_ICON[color] ?? "info-outline-rounded", "md")}</span>
      <div className="min-w-0 flex-1 space-y-2">
        {title && <p className="m-0 font-semibold text-[var(--t-text)]">{title}</p>}
        {children && <div className="text-foreground/85 [&_p]:m-0">{children}</div>}
      </div>
      {onDismiss && (
        <button type="button" aria-label="Dismiss" onClick={onDismiss} className="self-start rounded p-2 text-muted-foreground hover:bg-black/5">
          <X className="size-6" />
        </button>
      )}
    </div>
  );
}

/* ── Card ────────────────────────────────────────────────────────────────
 * A framed hairline panel (square corners, corner ticks). Anything framed
 * inside it (tables) drops its own frame via InPanel, so panels never nest. */
export const InPanel = createContext(false);

interface CardProps {
  variant?: "default" | "raised" | "flat" | "outline";
  size?: "sm" | "md" | "lg";
  color?: Color;
  interactive?: boolean;
  selected?: boolean;
  className?: string;
  style?: CSSProperties;
  onClick?: (e: MouseEvent<HTMLDivElement>) => void;
  children?: ReactNode;
}

function CardRoot({ variant = "default", size = "md", color, interactive, selected, className, style, onClick, children }: CardProps) {
  // A default card placed directly on the page is a framed card like every
  // other section. Nested cards and explicit outline/raised variants (grid
  // tiles, sortable items) stay square framed boxes.
  const nested = useContext(InPanel);
  const band = (variant === "default" || variant === "raised") && !nested && !color && !interactive && !onClick;
  return (
    <div
      data-size={size}
      onClick={onClick}
      className={cn(
        "kit-card group/card flex min-w-0 flex-col text-card-foreground",
        band ? "bg-card shadow-[inset_0_0_0_1px_var(--border)]" : nested ? "border-t border-border bg-transparent" : "border border-border bg-card",
        variant === "flat" && "bg-muted/40",
        color && "border-[var(--t-line)] bg-[var(--t-soft)]",
        (interactive || onClick) && "cursor-pointer transition-colors hover:border-ring/40",
        selected && "ring-2 ring-ring/40",
        className,
      )}
      style={{ ...(color ? toneVars(color) : {}), ...style }}
    >
      <InPanel.Provider value={true}>{children}</InPanel.Provider>
    </div>
  );
}

function CardHead({ separator, className, style, children }: { separator?: boolean; className?: string; style?: CSSProperties; children?: ReactNode }) {
  return (
    <div
      className={cn(
        "flex items-center gap-5 px-6 pt-[10px] group-data-[size=sm]/card:px-5 group-data-[size=sm]/card:pt-5",
        separator && "border-b pb-5",
        className,
      )}
      style={style}
    >
      {children}
    </div>
  );
}

function CardBody({ className, style, children }: { className?: string; style?: CSSProperties; children?: ReactNode }) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-5 p-6 group-data-[size=sm]/card:p-5", className)} style={style}>
      {children}
    </div>
  );
}

function CardFooter({ className, style, children }: { className?: string; style?: CSSProperties; children?: ReactNode }) {
  return (
    <div className={cn("flex items-center gap-3 border-t px-6 py-5", className)} style={style}>
      {children}
    </div>
  );
}

export const Card = Object.assign(CardRoot, { Head: CardHead, Body: CardBody, Footer: CardFooter });

/* ── CopyButton (new; used by detail pages for IDs and digests) ─────────*/
export function CopyButton({ value, label = "Copy" }: { value: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      variant="clear"
      size="xs"
      aria-label={done ? "Copied" : label}
      iconLeft={done ? "check-rounded" : "content-copy-outline-rounded"}
      onClick={() => {
        void copyText(value).then((ok) => {
          if (!ok) return;
          setDone(true);
          window.setTimeout(() => setDone(false), 1400);
        });
      }}
    />
  );
}
