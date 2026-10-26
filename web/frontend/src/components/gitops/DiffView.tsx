import { useMemo, useState } from "react";
import type { GitOpsDiffLine } from "../../types";

const CONTEXT = 3;

/**
 * Line-level diff with +/− gutters. Added lines use the green tone ramp,
 * removed lines the red one (soft background, tone text); the gutter sign
 * carries the meaning too, so it never relies on colour alone. Long runs of
 * unchanged lines collapse to a "N unchanged lines" row that expands.
 */
export function DiffView({ lines, full = false }: { lines: GitOpsDiffLine[]; full?: boolean }) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const blocks = useMemo(() => collapse(lines, full ? Infinity : CONTEXT), [lines, full]);
  if (lines.length === 0) return null;
  let oldNo = 0;
  let newNo = 0;
  return (
    <div className="overflow-x-auto border bg-background font-mono text-[13px] leading-[18px]" role="table" aria-label="Diff">
      {blocks.map((b, bi) => {
        if (b.kind === "fold" && !expanded.has(bi)) {
          oldNo += b.lines.length;
          newNo += b.lines.length;
          return (
            <button
              key={bi}
              type="button"
              onClick={() => setExpanded((s) => new Set(s).add(bi))}
              className="block w-full border-y border-dashed bg-muted/40 px-5 py-[2px] text-left text-[12px] text-muted-foreground outline-none hover:text-foreground focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
            >
              ⋯ {b.lines.length} unchanged line{b.lines.length === 1 ? "" : "s"}
            </button>
          );
        }
        return b.lines.map((l, i) => {
          if (l.op !== "+") oldNo += 1;
          if (l.op !== "-") newNo += 1;
          const tone = l.op === "+" ? "green" : l.op === "-" ? "red" : "";
          return (
            <div
              key={`${bi}-${i}`}
              role="row"
              className="flex min-w-max"
              style={tone ? { background: `var(--tone-${tone}-soft)`, color: `var(--tone-${tone}-text)` } : undefined}
            >
              <span className="w-15 shrink-0 select-none pr-3 text-right text-muted-foreground/70 tabular-nums">
                {l.op === "+" ? "" : oldNo}
              </span>
              <span className="w-15 shrink-0 select-none pr-3 text-right text-muted-foreground/70 tabular-nums">
                {l.op === "-" ? "" : newNo}
              </span>
              <span
                aria-label={l.op === "+" ? "added" : l.op === "-" ? "removed" : undefined}
                className="w-8 shrink-0 select-none text-center"
                style={tone ? { borderLeft: `2px solid var(--tone-${tone}-line)` } : { borderLeft: "2px solid transparent" }}
              >
                {l.op === "+" ? "+" : l.op === "-" ? "−" : " "}
              </span>
              <span className="whitespace-pre pr-6">{l.text || " "}</span>
            </div>
          );
        });
      })}
    </div>
  );
}

type Block = { kind: "lines" | "fold"; lines: GitOpsDiffLine[] };

function collapse(lines: GitOpsDiffLine[], context: number): Block[] {
  if (!Number.isFinite(context) || !lines.some((l) => l.op !== " ")) {
    return [{ kind: "lines", lines }];
  }
  const keep = lines.map(() => false);
  lines.forEach((l, i) => {
    if (l.op === " ") return;
    for (let j = Math.max(0, i - context); j <= Math.min(lines.length - 1, i + context); j += 1) keep[j] = true;
  });
  // Always keep the document header (apiVersion/kind/metadata) for orientation.
  for (let j = 0; j < Math.min(4, lines.length); j += 1) keep[j] = true;
  const out: Block[] = [];
  lines.forEach((l, i) => {
    const kind = keep[i] ? "lines" : "fold";
    const last = out[out.length - 1];
    if (last && last.kind === kind) last.lines.push(l);
    else out.push({ kind, lines: [l] });
  });
  return out;
}
