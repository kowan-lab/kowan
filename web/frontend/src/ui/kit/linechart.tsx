import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { toneVars, type Color } from "./tone";

/**
 * Dependency-free SVG line chart (replaces ECharts, ~600 KB). Draws one or
 * more series over shared x labels with a light grid, mono axis labels, an
 * optional legend, area fill, step interpolation and a hover crosshair with a
 * tooltip. Colours come from the theme tokens, so it follows every palette.
 */
export interface LineSeries {
  name: string;
  data: number[];
  color?: Color;
}

const AUTO: Color[] = ["primary", "green", "amber", "purple", "rose", "cyan", "orange", "teal"];
const PAD = { top: 10, right: 12, bottom: 22, left: 40 };

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  const n = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return n * exp;
}

function fmt(v: number, unit?: string): string {
  const s = Math.abs(v) >= 100 || Number.isInteger(v) ? String(Math.round(v)) : v.toFixed(1);
  return unit ? `${s}${unit}` : s;
}

export function LineChart({
  labels, series, height = 220, yMin = 0, yMax, unit, step = false, area = false, legend, className, style,
}: {
  labels: string[];
  series: LineSeries[];
  height?: number;
  yMin?: number;
  yMax?: number;
  unit?: string;
  step?: boolean;
  area?: boolean;
  legend?: boolean;
  className?: string;
  style?: CSSProperties;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.floor(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const showLegend = legend ?? series.length > 1;
  const legendH = showLegend ? 18 : 0;
  const n = Math.max(...series.map((s) => s.data.length), labels.length, 0);
  const top = yMax ?? niceMax(Math.max(1, ...series.flatMap((s) => s.data)));
  const innerW = Math.max(0, width - PAD.left - PAD.right);
  const innerH = Math.max(0, height - PAD.top - PAD.bottom - legendH);
  const x = (i: number) => PAD.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v: number) => PAD.top + legendH + innerH - ((Math.min(Math.max(v, yMin), top) - yMin) / (top - yMin || 1)) * innerH;

  const paths = useMemo(
    () =>
      series.map((s) => {
        let d = "";
        s.data.forEach((v, i) => {
          const px = x(i).toFixed(1);
          const py = y(v).toFixed(1);
          if (i === 0) d = `M${px},${py}`;
          else if (step) d += `H${px}V${py}`;
          else d += `L${px},${py}`;
        });
        const fill = d && area ? `${d}L${x(s.data.length - 1).toFixed(1)},${y(yMin).toFixed(1)}L${x(0).toFixed(1)},${y(yMin).toFixed(1)}Z` : "";
        return { d, fill };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [series, width, height, top, step, area, n, legendH],
  );

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => yMin + t * (top - yMin));
  const xEvery = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(innerW / 70))));
  const colors = series.map((s, i) => toneVars(s.color ?? AUTO[i % AUTO.length]) as Record<string, string>);

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    if (n === 0) return;
    const r = e.currentTarget.getBoundingClientRect();
    const rel = (e.clientX - r.left - PAD.left) / (innerW || 1);
    setHover(Math.min(n - 1, Math.max(0, Math.round(rel * (n - 1)))));
  };

  return (
    <div ref={wrap} className={className} style={{ position: "relative", height, ...style }}>
      {width > 0 && (
        <svg width={width} height={height} onPointerMove={onMove} onPointerLeave={() => setHover(null)} role="img" aria-label={series.map((s) => s.name).join(", ")} className="block select-none">
          {showLegend && (
            <g transform={`translate(${PAD.left},2)`}>
              {series.map((s, i) => {
                const off = series.slice(0, i).reduce((a, p) => a + p.name.length * 7 + 28, 0);
                return (
                  <g key={s.name} transform={`translate(${off},0)`}>
                    <rect y={6} width={12} height={2} fill={colors[i]["--t-solid"]} />
                    <text x={18} y={11} fontSize={11} fill="var(--muted-foreground)">{s.name}</text>
                  </g>
                );
              })}
            </g>
          )}
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={PAD.left + innerW} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeDasharray={t === yMin ? undefined : "2 3"} />
              <text x={PAD.left - 6} y={y(t) + 3.5} fontSize={10.5} textAnchor="end" fill="var(--muted-foreground)">{fmt(t, unit)}</text>
            </g>
          ))}
          {labels.map((l, i) =>
            i % xEvery === 0 ? (
              <text key={i} x={x(i)} y={height - 6} fontSize={10.5} textAnchor="middle" fill="var(--muted-foreground)">{l}</text>
            ) : null,
          )}
          {paths.map((p, i) => (
            <g key={series[i].name}>
              {p.fill && <path d={p.fill} fill={colors[i]["--t-soft"]} />}
              <path d={p.d} fill="none" stroke={colors[i]["--t-solid"]} strokeWidth={1.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
            </g>
          ))}
          {hover !== null && (
            <g pointerEvents="none">
              <line x1={x(hover)} x2={x(hover)} y1={PAD.top + legendH} y2={PAD.top + legendH + innerH} stroke="var(--muted-foreground)" strokeDasharray="2 2" />
              {series.map((s, i) => (s.data[hover] !== undefined ? <circle key={s.name} cx={x(hover)} cy={y(s.data[hover])} r={3} fill="var(--card)" stroke={colors[i]["--t-solid"]} strokeWidth={1.5} /> : null))}
            </g>
          )}
        </svg>
      )}
      {hover !== null && width > 0 && (
        <div
          className="pointer-events-none absolute z-10 border bg-popover px-3 py-[4px] text-[12px] shadow-[0_8px_24px_-8px_rgb(0_0_0/0.35)]"
          style={{ top: PAD.top + legendH, left: Math.min(x(hover) + 10, width - 150) }}
        >
          <div className="pb-2 text-muted-foreground">{labels[hover]}</div>
          {series.map((s, i) => (
            <div key={s.name} className="flex items-center gap-3 whitespace-nowrap">
              <span className="size-3" style={{ background: colors[i]["--t-solid"] }} />
              <span className="text-muted-foreground">{s.name}</span>
              <span className="ml-auto pl-5 font-medium">{s.data[hover] !== undefined ? fmt(s.data[hover], unit) : "—"}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
