import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent, type WheelEvent as RWheelEvent } from "react";
import { Box, Button, Tooltip, stateTone, toneVars, type Color } from "@/ui/kit";
import type { NetEdge, NetGroup, NetMap, NetNode } from "./api";

/**
 * Lightweight SVG renderer for the network map. Coordinates come from the
 * server's deterministic layered layout; this only draws, pans (drag the
 * background), zooms (wheel / buttons) and highlights a node's neighbours on
 * hover or keyboard focus. Nodes are focusable buttons: Enter opens them.
 */

interface View { k: number; x: number; y: number }

const PAD = 24;

function nodeTone(n: NetNode): Color {
  switch (n.kind) {
    case "container":
      return stateTone(n.state || "unknown");
    case "port":
      return n.warn ? "warning" : n.state === "local" ? "gray" : "blue";
    case "domain":
      return n.state === "tls" ? "green" : "warning";
    default:
      return "gray";
  }
}

const KIND_GLYPH: Record<NetNode["kind"], string> = { container: "▣", network: "≋", port: "⇥", domain: "◎" };

function edgePath(e: NetEdge, a: NetNode, b: NetNode): string {
  if (e.kind === "depends") {
    // container -> container in the same column: arc on the left side
    const x = a.x;
    const y1 = a.y + a.h / 2;
    const y2 = b.y + b.h / 2;
    const bend = Math.min(60, 18 + Math.abs(y2 - y1) / 4);
    return `M${x},${y1} C${x - bend},${y1} ${x - bend},${y2} ${x},${y2}`;
  }
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

function clip(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

export interface NetMapGraphProps {
  map: NetMap;
  onOpenNode: (n: NetNode) => void;
  onOpenGroup?: (g: NetGroup) => void;
  height?: number | string;
}

export default function NetMapGraph({ map, onOpenNode, onOpenGroup, height = "calc(100vh - 260px)" }: NetMapGraphProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 900, h: 600 });
  const [view, setView] = useState<View>({ k: 1, x: PAD, y: PAD });
  const [hover, setHover] = useState<string | null>(null);
  const drag = useRef<{ px: number; py: number; vx: number; vy: number } | null>(null);

  const byId = useMemo(() => new Map(map.nodes.map((n) => [n.id, n])), [map.nodes]);
  const neighbours = useMemo(() => {
    const m = new Map<string, Set<string>>();
    for (const e of map.edges) {
      if (!m.has(e.from)) m.set(e.from, new Set());
      if (!m.has(e.to)) m.set(e.to, new Set());
      m.get(e.from)!.add(e.to);
      m.get(e.to)!.add(e.from);
    }
    return m;
  }, [map.edges]);
  const lit = hover ? new Set([hover, ...(neighbours.get(hover) ?? [])]) : null;

  // Track the drawing area's size so "fit" can compute a scale.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth || 900, h: el.clientHeight || 600 }));
    ro.observe(el);
    setSize({ w: el.clientWidth || 900, h: el.clientHeight || 600 });
    return () => ro.disconnect();
  }, []);

  const fit = useCallback(() => {
    const w = map.width + PAD * 2 + 60;
    const h = map.height + PAD * 2;
    const k = Math.max(0.2, Math.min(1.25, size.w / w, size.h / Math.max(h, 1)));
    setView({ k, x: PAD + 60 * k, y: PAD });
  }, [map.width, map.height, size.w, size.h]);
  // Re-fit when the data's extent changes (new host / filter), not on every refetch.
  useEffect(() => {
    fit();
  }, [map.width, map.height, size.w]); // eslint-disable-line react-hooks/exhaustive-deps

  const zoomAt = (factor: number, cx: number, cy: number) =>
    setView((v) => {
      const k = Math.max(0.15, Math.min(3, v.k * factor));
      const r = k / v.k;
      return { k, x: cx - (cx - v.x) * r, y: cy - (cy - v.y) * r };
    });

  const onWheel = (e: RWheelEvent<SVGSVGElement>) => {
    const rect = svgRef.current!.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - rect.left, e.clientY - rect.top);
  };
  // Block page scroll while zooming the map (React wheel listeners are passive).
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const stop = (e: WheelEvent) => e.preventDefault();
    el.addEventListener("wheel", stop, { passive: false });
    return () => el.removeEventListener("wheel", stop);
  }, []);

  const onPointerDown = (e: RPointerEvent<SVGSVGElement>) => {
    if ((e.target as Element).closest("[data-node]")) return;
    drag.current = { px: e.clientX, py: e.clientY, vx: view.x, vy: view.y };
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: RPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    setView((v) => ({ ...v, x: d.vx + e.clientX - d.px, y: d.vy + e.clientY - d.py }));
  };
  const onPointerUp = () => {
    drag.current = null;
  };

  return (
    <Box direction="column" style={{ position: "relative", height, minHeight: 360 }}>
      <Box direction="row" gap={1} style={{ position: "absolute", right: 8, top: 8, zIndex: 1 }}>
        <Tooltip content="Zoom in"><Button size="xs" variant="outline" color="neutral" aria-label="Zoom in" onClick={() => zoomAt(1.2, size.w / 2, size.h / 2)}>+</Button></Tooltip>
        <Tooltip content="Zoom out"><Button size="xs" variant="outline" color="neutral" aria-label="Zoom out" onClick={() => zoomAt(1 / 1.2, size.w / 2, size.h / 2)}>−</Button></Tooltip>
        <Tooltip content="Fit to view"><Button size="xs" variant="outline" color="neutral" onClick={fit}>Fit</Button></Tooltip>
      </Box>
      <svg
        ref={svgRef}
        role="img"
        aria-label="Network and dependency map"
        width="100%"
        height="100%"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        style={{ cursor: drag.current ? "grabbing" : "grab", touchAction: "none", userSelect: "none", display: "block", fontFamily: "'Geist Mono Variable', ui-monospace, monospace" }}
      >
        <defs>
          <marker id="nm-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="var(--muted-foreground)" />
          </marker>
        </defs>
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {/* column captions */}
          {map.groups.length > 0 && (
            <g fontSize={10} fill="var(--muted-foreground)" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontFamily: "var(--font-mono)" }}>
              <text x={0} y={-8}>INGRESS</text>
              <text x={300} y={-8}>CONTAINERS BY STACK</text>
              <text x={640} y={-8}>NETWORKS</text>
            </g>
          )}
          {map.groups.map((g) =>
            g.kind === "host" ? (
              <g key={g.id}>
                <rect x={g.x - 10} y={g.y} width={g.w + 20} height={g.h} fill="none" stroke="var(--border)" />
                <text x={g.x} y={g.y + 20} fontSize={13} fontWeight={600} fill="var(--foreground)">{g.label}</text>
              </g>
            ) : (
              <g key={g.id}>
                <rect x={g.x} y={g.y} width={g.w} height={g.h} fill="var(--muted)" fillOpacity={0.35} stroke="var(--border)" />
                <text
                  x={g.x + 6}
                  y={g.y + 15}
                  fontSize={11}
                  fill={g.deploymentId ? "var(--primary)" : "var(--muted-foreground)"}
                  style={{ cursor: g.deploymentId && onOpenGroup ? "pointer" : undefined }}
                  onClick={() => g.deploymentId && onOpenGroup?.(g)}
                >
                  {clip(g.label, 34)}
                </text>
              </g>
            ),
          )}
          <g fill="none">
            {map.edges.map((e) => {
              const a = byId.get(e.from);
              const b = byId.get(e.to);
              if (!a || !b) return null;
              const on = !lit || (lit.has(e.from) && lit.has(e.to));
              return (
                <path
                  key={e.id}
                  d={edgePath(e, a, b)}
                  stroke={on && lit ? "var(--foreground)" : "var(--muted-foreground)"}
                  strokeOpacity={on ? (lit ? 0.9 : 0.45) : 0.08}
                  strokeWidth={lit && on ? 1.6 : 1}
                  strokeDasharray={e.kind === "depends" ? "4 3" : e.kind === "domain" ? "2 3" : undefined}
                  markerEnd={e.kind === "depends" ? "url(#nm-arrow)" : undefined}
                />
              );
            })}
          </g>
          {map.nodes.map((n) => {
            const tone = toneVars(nodeTone(n));
            const dim = lit && !lit.has(n.id);
            const maxChars = Math.floor((n.w - 30) / 7.2);
            return (
              <g
                key={n.id}
                data-node
                role="button"
                tabIndex={0}
                aria-label={`${n.kind} ${n.label}${n.state ? `, ${n.state}` : ""}`}
                transform={`translate(${n.x},${n.y})`}
                opacity={dim ? 0.25 : 1}
                style={{ ...tone, cursor: "pointer", outline: "none" }}
                onMouseEnter={() => setHover(n.id)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(n.id)}
                onBlur={() => setHover(null)}
                onClick={() => onOpenNode(n)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpenNode(n);
                  }
                }}
              >
                <title>{`${n.label}${n.sub ? `\n${n.sub}` : ""}${n.state ? `\n${n.state}` : ""}`}</title>
                <rect width={n.w} height={n.h} fill="var(--card)" stroke={hover === n.id ? "var(--ring)" : "var(--t-line)"} strokeWidth={hover === n.id ? 1.5 : 1} />
                <rect width={3} height={n.h} fill="var(--t-solid)" />
                <text x={10} y={n.h / 2 + 4} fontSize={11} fill="var(--t-text)">{KIND_GLYPH[n.kind]}</text>
                <text x={24} y={n.h / 2 + 4} fontSize={11.5} fill="var(--foreground)">
                  {clip(n.label, maxChars)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </Box>
  );
}
