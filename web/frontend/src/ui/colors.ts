import { toneBase, toneVars, type Color } from "./kit/tone";

/** Map any colour name onto a valid kit colour (unknown names -> `fallback`). */
export function toKitColor(color?: string, fallback: Color = "neutral"): Color {
  if (!color) return fallback;
  const b = toneBase(color, "gray");
  return (b === "gray" && color !== "gray" && color !== "neutral" ? fallback : color) as Color;
}

/** Inline style for a soft coloured chip (icon squares in headers and stat tiles). */
export function softChip(color?: string): { background: string; color: string } {
  const v = toneVars(color ?? "neutral") as Record<string, string>;
  return { background: v["--t-soft"], color: v["--t-text"] };
}
