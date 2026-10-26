import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Code,
  Icon,
  InputToggle,
  Popover,
  Radio,
  RadioGroup,
  Spinner,
  Text,
  TextInput,
  Tooltip,
} from "@/ui/kit";

import { toKitColor } from "../../ui/colors";
import EmptyState from "../EmptyState";
import {
  highlightSegments,
  matchLine,
  useCompiledFilter,
  useLogStream,
  type LogLevel,
  type LogSource,
} from "./logStream";

const TAG_COLORS = [
  "blue",
  "magenta",
  "teal",
  "orange",
  "cyan",
  "rose",
  "lime",
  "purple",
  "blue",
  "red",
];

// Theme tokens, so the log pane follows every palette in light and dark.
const LEVEL_COLORS: Record<LogLevel, string | undefined> = {
  error: "var(--tone-red-text)",
  warn: "var(--tone-amber-text)",
  info: "var(--tone-blue-text)",
  debug: "var(--muted-foreground)",
  none: undefined,
};

// Rendering budget. Without wrap every line is exactly one ROW_H-tall row, so
// the list is virtualized: only the rows inside the viewport (plus OVERSCAN on
// each side) are in the DOM, and spacers stand in for the rest. With wrap, row
// heights vary with content and width, so instead of measuring we cap the DOM
// to the newest WRAP_CAP lines (the buffer itself keeps up to 5000).
const ROW_H = 18;
const OVERSCAN = 30;
const WRAP_CAP = 1500;

const REFRESH_OPTIONS = [
  { value: "0", label: "Off" },
  { value: "2", label: "2s" },
  { value: "5", label: "5s" },
  { value: "10", label: "10s" },
  { value: "30", label: "30s" },
];

interface LogViewerProps {
  sources: LogSource[];
  /** Show the host name alongside the container in tags/lines (multi-host). */
  aggregated?: boolean;
  /** Shown when there are no sources selected. */
  emptyTitle?: string;
  emptyDescription?: string;
  /** Optional fixed height; defaults to filling the available flex space. */
  height?: string | number;
}

/**
 * LogViewer is the single log-rendering surface used across the app. It takes a
 * set of abstract log sources (one per container/service), streams + merges
 * them, and renders a searchable, filterable, colourised view with show/hide
 * timestamps, follow/auto-refresh, wrap and export. All the streaming and
 * parsing lives in ./logStream so this component is presentation-only.
 */
export default function LogViewer({
  sources,
  aggregated = false,
  emptyTitle = "Pick a container",
  emptyDescription = "Select a source to start streaming its logs here.",
  height,
}: LogViewerProps) {
  // Connection controls.
  const [tail, setTail] = useState(200);
  const [follow, setFollow] = useState(true);
  const [refreshSec, setRefreshSec] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);

  // View controls.
  const [filterText, setFilterText] = useState("");
  const [levelQuick, setLevelQuick] = useState<"all" | LogLevel>("all");
  const [showTs, setShowTs] = useState(false);
  const [wrap, setWrap] = useState(true);

  const filter = useCompiledFilter(filterText);
  const { lines, status, clear } = useLogStream(sources, {
    tail,
    follow,
    reloadKey,
  });

  // Auto-refresh: only meaningful when not following a live stream. Re-open all
  // sockets every refreshSec seconds by bumping the reload key.
  useEffect(() => {
    if (follow || refreshSec <= 0) return;
    const id = setInterval(() => setReloadKey((k) => k + 1), refreshSec * 1000);
    return () => clearInterval(id);
  }, [follow, refreshSec]);

  const filtered = useMemo(() => {
    return lines.filter((l) => {
      if (levelQuick !== "all" && l.level !== levelQuick) return false;
      return matchLine(l, filter);
    });
  }, [lines, filter, levelQuick]);

  // Per-source tag colour by selection order.
  const colorOf = (key: string): string => {
    const idx = sources.findIndex((s) => s.key === key);
    return TAG_COLORS[(idx < 0 ? 0 : idx) % TAG_COLORS.length];
  };
  const multi = sources.length > 1;

  // Auto-scroll while following.
  const viewportRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!follow) return;
    const vp = viewportRef.current;
    if (vp) vp.scrollTo({ top: vp.scrollHeight });
  }, [filtered, follow, wrap]);

  // Viewport geometry for the virtual window, updated on scroll (once per
  // animation frame) and on resize.
  const [view, setView] = useState({ top: 0, height: 600 });
  const rafRef = useRef(0);
  const measure = useCallback(() => {
    rafRef.current = 0;
    const el = viewportRef.current;
    if (!el) return;
    setView((v) =>
      v.top === el.scrollTop && v.height === el.clientHeight
        ? v
        : { top: el.scrollTop, height: el.clientHeight },
    );
  }, []);
  const onScroll = useCallback(() => {
    if (!rafRef.current) rafRef.current = requestAnimationFrame(measure);
  }, [measure]);
  const hasSources = sources.length > 0;
  useEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    measure();
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => {
      ro.disconnect();
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = 0;
    };
  }, [hasSources, measure]);

  // The slice of `filtered` actually rendered, plus spacer heights.
  const win = useMemo(() => {
    const total = filtered.length;
    if (wrap) {
      const start = Math.max(0, total - WRAP_CAP);
      return { start, end: total, padTop: 0, padBottom: 0, hidden: start };
    }
    // While following, anchor the window at the bottom so freshly appended rows
    // render in the same frame the auto-scroll lands there.
    const top = follow ? Math.max(0, total * ROW_H - view.height) : view.top;
    const start = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
    const end = Math.min(total, Math.ceil((top + view.height) / ROW_H) + OVERSCAN);
    return {
      start,
      end: Math.max(start, end),
      padTop: start * ROW_H,
      padBottom: Math.max(0, total - Math.max(start, end)) * ROW_H,
      hidden: 0,
    };
  }, [filtered.length, wrap, follow, view]);
  const visible = filtered.slice(win.start, win.end);

  const download = () => {
    const text = filtered
      .map((l) => (showTs && l.tsLabel ? `${l.tsLabel} ${l.text}` : l.text))
      .join("\n");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `logs-${new Date().toISOString().replace(/[:.]/g, "-")}.log`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Box direction="column" gap={2} style={{ flex: 1, minHeight: 0 }}>
      {/* Toolbar — grouped into a search row and a controls row for legibility. */}
      <div>
        <Box direction="column" gap={2}>
          <Box direction="row" wrap gap={2} align="center">
            <TextInput
              size="sm"
              style={{ flex: 1, minWidth: 200 }}
              placeholder='Filter — text, "phrase", -exclude, /regex/, level:error'
              iconLeft={<Icon name="search-rounded" />}
              suffix={
                filterText ? (
                  <Button
                    size="sm"
                    variant="clear"
                    color="neutral"
                    iconLeft="close-rounded"
                    aria-label="Clear filter"
                    onClick={() => setFilterText("")}
                  />
                ) : (
                  <FilterHelp />
                )
              }
              value={filterText}
              onChange={(e) => setFilterText(e.target.value)}
              errorText={filter.error}
            />
            <RadioGroup
              orientation="horizontal"
              size="sm"
              attached
              variant="flat"
              selectedStyle="outline"
              indicator={false}
              value={levelQuick}
              onChange={(v) => setLevelQuick(v as "all" | LogLevel)}
            >
              <Radio value="all" label="All" />
              <Radio value="error" label="Err" />
              <Radio value="warn" label="Warn" />
              <Radio value="info" label="Info" />
              <Radio value="debug" label="Dbg" />
            </RadioGroup>
          </Box>

          <Box direction="row" wrap gap={4} align="center">
            <Box direction="row" gap={1} align="center">
              <Text variant="body" size="xs" color="muted">
                Tail
              </Text>
              <RadioGroup
                orientation="horizontal"
                size="sm"
                attached
                variant="flat"
                selectedStyle="outline"
                indicator={false}
                value={String(tail)}
                onChange={(v) => setTail(Number(v))}
              >
                {["100", "200", "500", "1000"].map((n) => (
                  <Radio key={n} value={n} label={n} />
                ))}
              </RadioGroup>
            </Box>

            <InputToggle
              variant="inline"
              size="sm"
              label="Follow"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />

            {!follow && (
              <Box direction="row" gap={1} align="center">
                <Text variant="body" size="xs" color="muted">
                  Auto-refresh
                </Text>
                <RadioGroup
                  orientation="horizontal"
                  size="sm"
                  attached
                  variant="flat"
                  selectedStyle="outline"
                  indicator={false}
                  value={String(refreshSec)}
                  onChange={(v) => setRefreshSec(Number(v))}
                >
                  {REFRESH_OPTIONS.map((o) => (
                    <Radio key={o.value} value={o.value} label={o.label} />
                  ))}
                </RadioGroup>
                <Tooltip content="Refresh now">
                  <Button
                    variant="outline"
                    color="neutral"
                    iconLeft="refresh-rounded"
                    aria-label="Refresh now"
                    onClick={() => setReloadKey((k) => k + 1)}
                  />
                </Tooltip>
              </Box>
            )}

            <InputToggle
              variant="inline"
              size="sm"
              label="Timestamps"
              checked={showTs}
              onChange={(e) => setShowTs(e.target.checked)}
            />
            <InputToggle
              variant="inline"
              size="sm"
              label="Wrap"
              checked={wrap}
              onChange={(e) => setWrap(e.target.checked)}
            />

            <Box direction="row" gap={1} align="center" style={{ marginLeft: "auto" }}>
              <Tooltip content="Download visible logs">
                <Button
                  variant="outline"
                  color="neutral"
                  iconLeft="download-rounded"
                  onClick={download}
                  aria-label="Download logs"
                />
              </Tooltip>
              <Tooltip content="Clear">
                <Button
                  variant="outline"
                  color="neutral"
                  iconLeft="delete-outline-rounded"
                  onClick={clear}
                  aria-label="Clear logs"
                />
              </Tooltip>
            </Box>
          </Box>
        </Box>
      </div>

      {/* Source status chips + line count. */}
      {sources.length > 0 && (
        <Box direction="row" wrap gap={2} align="center">
          {sources.map((s) => {
            const st = status[s.key];
            return (
              <Badge key={s.key} size="sm" variant="light" color={toKitColor(colorOf(s.key))}>
                {s.label}
                {aggregated && s.hostName ? `@${s.hostName}` : ""}
                {st && st !== "live" ? ` · ${st}` : ""}
              </Badge>
            );
          })}
          <Text variant="body" size="xs" color="muted" style={{ marginLeft: "auto" }}>
            {filtered.length}
            {filtered.length !== lines.length ? `/${lines.length}` : ""} lines
          </Text>
        </Box>
      )}

      {sources.length === 0 ? (
        <EmptyState
          icon={<Icon name="description-outline-rounded" size="xl" />}
          title={emptyTitle}
          description={emptyDescription}
        />
      ) : (
        <Box
          ref={viewportRef}
          onScroll={onScroll}
          style={{
            flex: 1,
            // A bounded viewport is what makes the virtual window work: without
            // a cap the box grows to the full buffer and every row renders.
            minHeight: 320,
            height,
            maxHeight: height ?? "calc(100svh - 230px)",
            overflow: "auto",
            background: "var(--surface-sunken)",
            border: "1px solid var(--border)",
          }}
        >
          <Box
            display="block"
            padding="xs"
            style={{
              fontFamily: "'Geist Mono Variable', ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: 12,
              lineHeight: `${ROW_H}px`,
            }}
          >
            {filtered.length === 0 ? (
              <Box direction="row" gap={2} padding="xs" align="center">
                <Spinner size="xs" />
                <Text variant="body" size="sm" color="muted">
                  {lines.length === 0
                    ? "Waiting for log output…"
                    : "No lines match the current filter."}
                </Text>
              </Box>
            ) : (
              <>
              {win.hidden > 0 && (
                <Text as="div" variant="body" size="xs" color="muted" style={{ padding: "0 0 4px" }}>
                  {win.hidden} earlier lines not shown while Wrap is on. Turn Wrap off to scroll the
                  whole buffer.
                </Text>
              )}
              {win.padTop > 0 && <div aria-hidden style={{ height: win.padTop }} />}
              {visible.map((l) => (
                <Box
                  key={l.seq}
                  display="block"
                  style={{
                    whiteSpace: wrap ? "pre-wrap" : "pre",
                    wordBreak: wrap ? "break-all" : "normal",
                    height: wrap ? undefined : ROW_H,
                    color: LEVEL_COLORS[l.level] ?? "var(--foreground)",
                  }}
                >
                  {showTs && l.tsLabel && (
                    <Text as="span" color="muted" style={{ fontSize: 11 }}>
                      {l.tsLabel}{" "}
                    </Text>
                  )}
                  {multi && (
                    <Text as="span" color={toKitColor(colorOf(l.sourceKey))} style={{ fontSize: 11 }}>
                      {l.label}
                      {aggregated && l.hostName ? `@${l.hostName}` : ""}{" "}
                    </Text>
                  )}
                  {highlightSegments(l.text, filter).map((seg, i) =>
                    seg.hit ? (
                      <mark
                        key={i}
                        style={{
                          background: "var(--tone-amber-solid)",
                          color: "var(--tone-amber-on)",
                        }}
                      >
                        {seg.text}
                      </mark>
                    ) : (
                      <span key={i}>{seg.text}</span>
                    ),
                  )}
                </Box>
              ))}
              {win.padBottom > 0 && <div aria-hidden style={{ height: win.padBottom }} />}
              </>
            )}
          </Box>
        </Box>
      )}
    </Box>
  );
}

/** A popover cheatsheet for the filter mini-language. */
function FilterHelp() {
  return (
    <Popover placement="bottom-end">
      <Popover.Trigger>
        <Button
          size="sm"
          variant="clear"
          color="neutral"
          iconLeft="help-outline-rounded"
          aria-label="Filter help"
        />
      </Popover.Trigger>
      <Popover.Content>
        <Box direction="column" gap={1} style={{ width: 300 }}>
          <Text variant="body" size="sm" weight="semibold">
            Filter syntax
          </Text>
          <Text variant="body" size="xs">
            Space-separated terms are combined with AND:
          </Text>
          <Box direction="column" gap={1}>
            <FilterRow code="error" desc="contains “error”" />
            <FilterRow code='"connection lost"' desc="exact phrase" />
            <FilterRow code="-debug" desc="does NOT contain “debug”" />
            <FilterRow code="/user \d+/i" desc="regex (i = ignore case)" />
            <FilterRow code="level:error,warn" desc="only these levels" />
          </Box>
        </Box>
      </Popover.Content>
    </Popover>
  );
}

function FilterRow({ code, desc }: { code: string; desc: string }) {
  return (
    <Box direction="row" wrap={false} gap={2} justify="between" align="center">
      <Code style={{ fontSize: 11 }}>{code}</Code>
      <Text variant="body" size="xs" color="muted">
        {desc}
      </Text>
    </Box>
  );
}

/** Build a container-logs source for the shared viewer. */
export type { LogSource };
