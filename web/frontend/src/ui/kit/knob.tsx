import { useRef, type KeyboardEvent } from "react";
import { cn } from "@/lib/utils";

/* ── Knob ────────────────────────────────────────────────────────────────
 * A rotary policy control: a 28px dial whose pointer sits at ~8 o'clock when
 * off (muted) and swings towards ~2 o'clock as it is turned up (signal
 * orange). The state is always spelled out in a mono label beside it, so it
 * never relies on the pointer or the colour alone.
 *
 *  - two positions: a `role="switch"`; Space/Enter (or a click) toggles.
 *  - three or more: a `role="slider"` stepping through labelled positions;
 *    arrows step, Home/End jump, a click turns it one notch up (wrapping).
 *    With `showOptions` it becomes a radiogroup: the dial plus every position
 *    as a radio, roving focus, arrows move the selection.
 *
 * The rotation takes 160ms and is dropped under prefers-reduced-motion. */

export interface KnobOption<V extends string = string> {
  value: V;
  label: string;
}

export interface KnobProps<V extends string = string> {
  value: V;
  /** Two or more labelled positions, lowest (off) first. */
  options: KnobOption<V>[];
  onChange?: (value: V) => void;
  disabled?: boolean;
  /** A change is being saved: the knob is inert and says so. */
  loading?: boolean;
  /** Show every position as a radio next to the dial (multi-position only). */
  showOptions?: boolean;
  "aria-label": string;
  /** Hide the state label (only when the same text is shown right next to it). */
  hideLabel?: boolean;
  className?: string;
}

/** Pointer angle: -120deg (≈8 o'clock) for the first position, 60deg (≈2 o'clock) for the last. */
export function knobAngle(index: number, count: number): number {
  if (count <= 1) return -120;
  return -120 + (180 * index) / (count - 1);
}

function Dial({ index, count, on, busy, className }: { index: number; count: number; on: boolean; busy?: boolean; className?: string }) {
  return (
    <span
      aria-hidden
      className={cn(
        "relative inline-block size-[28px] shrink-0 rounded-full bg-[var(--popover)]",
        "shadow-[inset_0_0_0_1px_var(--input),0_1px_0_var(--border)]",
        busy && "animate-pulse",
        className,
      )}
    >
      <span
        className={cn(
          "absolute top-[4px] left-[13px] h-[8px] w-[2px] origin-[1px_10px]",
          "transition-[transform,background-color] duration-[160ms] ease-[cubic-bezier(.2,.8,.2,1)] motion-reduce:transition-none",
          on ? "bg-[var(--signal)]" : "bg-[color-mix(in_srgb,var(--muted-foreground)_72%,var(--card))]",
        )}
        style={{ transform: `rotate(${knobAngle(index, count)}deg)` }}
      />
    </span>
  );
}

const LABEL =
  "font-mono text-[12px] leading-[16px] font-semibold tracking-[0.04em] uppercase whitespace-nowrap";

export function Knob<V extends string = string>({
  value, options, onChange, disabled, loading, showOptions, hideLabel, className, "aria-label": ariaLabel,
}: KnobProps<V>) {
  const count = options.length;
  const found = options.findIndex((o) => o.value === value);
  const index = found < 0 ? 0 : found;
  const on = index > 0;
  const inert = disabled || loading;
  const current = options[index]?.label ?? String(value);
  const radios = useRef<(HTMLButtonElement | null)[]>([]);

  const set = (i: number) => {
    if (inert) return;
    const next = options[Math.max(0, Math.min(count - 1, i))];
    if (next && next.value !== value) onChange?.(next.value);
  };

  const stepKeys = (e: KeyboardEvent, focusRadio: boolean) => {
    let i: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") i = Math.min(count - 1, index + 1);
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") i = Math.max(0, index - 1);
    if (e.key === "Home") i = 0;
    if (e.key === "End") i = count - 1;
    if (i === null) return;
    e.preventDefault();
    set(i);
    if (focusRadio) radios.current[i]?.focus();
  };

  const stateLabel = !hideLabel && (
    <span className={cn(LABEL, on ? "text-foreground" : "text-muted-foreground")}>
      {loading ? "Saving…" : current}
    </span>
  );

  const shell = cn(
    "inline-flex items-center gap-[10px] outline-none select-none",
    "focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-[var(--signal)]",
    inert ? "cursor-not-allowed" : "cursor-pointer",
    disabled && "opacity-45",
    className,
  );

  if (count <= 2) {
    return (
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={ariaLabel}
        aria-busy={loading || undefined}
        disabled={disabled}
        aria-disabled={inert || undefined}
        className={shell}
        onClick={() => set(on ? 0 : 1)}
      >
        <Dial index={index} count={2} on={on} busy={loading} />
        {stateLabel}
      </button>
    );
  }

  if (!showOptions) {
    return (
      <button
        type="button"
        role="slider"
        aria-label={ariaLabel}
        aria-valuemin={0}
        aria-valuemax={count - 1}
        aria-valuenow={index}
        aria-valuetext={current}
        aria-busy={loading || undefined}
        disabled={disabled}
        aria-disabled={inert || undefined}
        className={shell}
        onClick={() => set((index + 1) % count)}
        onKeyDown={(e) => stepKeys(e, false)}
      >
        <Dial index={index} count={count} on={on} busy={loading} />
        {stateLabel}
      </button>
    );
  }

  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      aria-busy={loading || undefined}
      aria-disabled={inert || undefined}
      className={cn("inline-flex items-center gap-[10px]", disabled && "opacity-45", className)}
    >
      <button
        type="button"
        tabIndex={-1}
        aria-hidden
        disabled={inert}
        className={cn("inline-flex outline-none", inert ? "cursor-not-allowed" : "cursor-pointer")}
        onClick={() => {
          const i = (index + 1) % count;
          set(i);
          radios.current[i]?.focus();
        }}
      >
        <Dial index={index} count={count} on={on} busy={loading} />
      </button>
      <span className="inline-flex bg-[var(--popover)] shadow-[inset_0_0_0_1px_var(--border)]">
        {options.map((o, i) => (
          <button
            key={o.value}
            ref={(el) => {
              radios.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={i === index}
            tabIndex={i === index ? 0 : -1}
            disabled={disabled}
            className={cn(
              LABEL,
              "h-[28px] px-[10px] outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:outline-[var(--signal)]",
              i === index ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
              inert && "cursor-not-allowed",
            )}
            onClick={() => set(i)}
            onKeyDown={(e) => stepKeys(e, true)}
          >
            {o.label}
          </button>
        ))}
      </span>
      {loading && <span className={cn(LABEL, "text-muted-foreground")}>Saving…</span>}
    </div>
  );
}
