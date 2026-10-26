import type { CSSProperties } from "react";

/**
 * Named colours accepted by every `color` prop. Semantic names (success,
 * danger…) and hue names (blue, teal…) both resolve onto a small set of tone
 * ramps defined in styles/globals.css (`--tone-<base>-solid|soft|text|line`),
 * each tuned for light and dark themes.
 */
export type Color =
  | "success" | "warning" | "danger" | "critical" | "info" | "pending" | "discovery"
  | "neutral" | "black" | "gray" | "blue" | "green" | "yellow" | "red" | "orange"
  | "teal" | "purple" | "magenta" | "lime" | "cyan" | "lavender" | "rose"
  | "seagreen" | "chartreuse" | "tangerine" | "primary" | "amber";

export type ToneBase =
  | "gray" | "blue" | "green" | "amber" | "red" | "orange" | "teal"
  | "purple" | "magenta" | "cyan" | "lime" | "rose" | "primary";

const BASE: Record<string, ToneBase> = {
  success: "green", warning: "amber", danger: "red", critical: "red", info: "blue",
  amber: "amber", pending: "amber", discovery: "purple", neutral: "gray", black: "gray", gray: "gray",
  blue: "blue", green: "green", yellow: "amber", red: "red",
  orange: "orange", teal: "teal", purple: "purple", magenta: "magenta", lime: "lime",
  cyan: "cyan", lavender: "purple", rose: "rose", seagreen: "teal",
  chartreuse: "lime", tangerine: "orange", primary: "primary",
};

export function toneBase(color?: string, fallback: ToneBase = "gray"): ToneBase {
  if (!color) return fallback;
  return BASE[color] ?? fallback;
}

/** CSS variables `--t-solid/soft/text/line` for a colour, consumed by kit classes. */
export function toneVars(color?: string, fallback: ToneBase = "gray"): CSSProperties {
  const b = toneBase(color, fallback);
  return {
    "--t-solid": `var(--tone-${b}-solid)`,
    "--t-soft": `var(--tone-${b}-soft)`,
    "--t-text": `var(--tone-${b}-text)`,
    "--t-line": `var(--tone-${b}-line)`,
    "--t-on": `var(--tone-${b}-on)`,
  } as CSSProperties;
}
