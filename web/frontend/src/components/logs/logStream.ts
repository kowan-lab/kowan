import { useEffect, useMemo, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Shared log-streaming primitives used by every log viewer in the app (the Logs
// page, the container detail tab, the deployment detail tab). A "source" is an
// abstract log stream identified by a key and a WebSocket URL builder; the hook
// owns the WebSocket lifecycle, line parsing (timestamp / ANSI / level) and the
// rolling in-memory buffer, so the UI components only render.
// ---------------------------------------------------------------------------

export type LogLevel = "error" | "warn" | "info" | "debug" | "none";

/** One abstract log stream. `url` is called with the current tail/follow so the
 *  same source can be reopened with different backlog/follow settings. */
export interface LogSource {
  key: string;
  label: string;
  hostName?: string;
  url: (opts: { tail: number; follow: boolean }) => string;
}

/** A single parsed log line ready for rendering. */
export interface LogLine {
  seq: number;
  sourceKey: string;
  label: string;
  hostName?: string;
  /** Raw RFC3339 timestamp from Docker, if present. */
  rawTs?: string;
  /** Local HH:MM:SS(.mmm) rendering of the timestamp, if parsed. */
  tsLabel?: string;
  /** The message with timestamp + ANSI escapes removed. */
  text: string;
  level: LogLevel;
}

export type SourceStatus = "connecting" | "live" | "closed" | "error";

const MAX_LINES_DEFAULT = 5000;

// A CSI escape sequence (colour codes etc.) — stripped so logs render cleanly.
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

// Leading RFC3339(Nano) timestamp Docker prepends when Timestamps is enabled.
const TS_RE =
  /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s(.*)$/;

/** Best-effort log level detection from a line's text. */
function detectLevel(text: string): LogLevel {
  if (/\b(error|err|fatal|panic|exception|fail(ed|ure)?)\b/i.test(text)) return "error";
  if (/\b(warn|warning)\b/i.test(text)) return "warn";
  if (/\b(info|notice)\b/i.test(text)) return "info";
  if (/\b(debug|trace)\b/i.test(text)) return "debug";
  return "none";
}

/** Parse one raw stream line into its timestamp, clean text and level. */
function parseLine(raw: string): {
  rawTs?: string;
  tsLabel?: string;
  text: string;
  level: LogLevel;
} {
  let rest = raw;
  let rawTs: string | undefined;
  let tsLabel: string | undefined;

  const m = TS_RE.exec(rest);
  if (m) {
    rawTs = m[1];
    rest = m[2];
    const d = new Date(rawTs);
    if (!Number.isNaN(d.getTime())) {
      tsLabel = d.toLocaleTimeString(undefined, { hour12: false }) +
        "." + String(d.getMilliseconds()).padStart(3, "0");
    }
  }

  const text = rest.replace(ANSI_RE, "");
  return { rawTs, tsLabel, text, level: detectLevel(text) };
}

interface UseLogStreamOptions {
  tail: number;
  follow: boolean;
  /** Bump to force every socket to reopen (manual refresh / auto-refresh). */
  reloadKey?: number;
  max?: number;
}

/**
 * useLogStream opens one WebSocket per source, parses incoming lines and keeps a
 * rolling buffer. Sockets are reconciled incrementally as the source set changes
 * and fully reopened when tail / follow / reloadKey change (those are connection
 * parameters). Lines are flushed to state on a short interval to batch renders
 * under bursty output.
 */
export function useLogStream(
  sources: LogSource[],
  { tail, follow, reloadKey = 0, max = MAX_LINES_DEFAULT }: UseLogStreamOptions,
) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState<Record<string, SourceStatus>>({});

  const socketsRef = useRef<Map<string, WebSocket>>(new Map());
  const pendingRef = useRef<LogLine[]>([]);
  const seqRef = useRef(0);
  // Connection params that force a full reopen when they change.
  const connRef = useRef({ tail, follow, reloadKey });

  // A stable key describing the current source set for the effect dependency.
  const sourcesKey = sources.map((s) => s.key).join(",");
  // Keep the latest source objects addressable without retriggering the effect
  // on identity-only changes.
  const sourcesRef = useRef(sources);
  sourcesRef.current = sources;

  useEffect(() => {
    const sockets = socketsRef.current;
    const conn = connRef.current;

    // A change to any connection parameter reopens everything with a fresh
    // backlog.
    if (
      conn.tail !== tail ||
      conn.follow !== follow ||
      conn.reloadKey !== reloadKey
    ) {
      connRef.current = { tail, follow, reloadKey };
      for (const [, sock] of sockets) sock.close();
      sockets.clear();
      pendingRef.current = [];
      setEntriesReset();
    }

    const want = new Map(sourcesRef.current.map((s) => [s.key, s] as const));

    // Close sockets for removed sources.
    for (const [k, sock] of sockets) {
      if (!want.has(k)) {
        sock.close();
        sockets.delete(k);
      }
    }

    // Open sockets for newly-added sources.
    for (const [k, s] of want) {
      if (sockets.has(k)) continue;
      let sock: WebSocket;
      try {
        sock = new WebSocket(s.url({ tail, follow }));
      } catch {
        setStatus((st) => ({ ...st, [k]: "error" }));
        continue;
      }
      setStatus((st) => ({ ...st, [k]: "connecting" }));
      sock.onopen = () => setStatus((st) => ({ ...st, [k]: "live" }));
      sock.onerror = () => setStatus((st) => ({ ...st, [k]: "error" }));
      sock.onclose = () =>
        setStatus((st) => ({ ...st, [k]: st[k] === "live" ? "closed" : st[k] }));
      sock.onmessage = (ev) => {
        // The server batches lines: one message carries one or more log lines
        // joined with "\n" (flushed every ~50 ms / 64 KB). Splitting on newlines
        // also accepts the older one-line-per-message format unchanged.
        const text = typeof ev.data === "string" ? ev.data : "";
        if (text.length === 0) return;
        const parts = text.indexOf("\n") < 0 ? [text] : text.split(/\r?\n/);
        for (const line of parts) {
          if (line.length === 0) continue;
          const parsed = parseLine(line);
          pendingRef.current.push({
            seq: seqRef.current++,
            sourceKey: s.key,
            label: s.label,
            hostName: s.hostName,
            ...parsed,
          });
        }
      };
      sockets.set(k, sock);
    }
    function setEntriesReset() {
      setLines([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourcesKey, tail, follow, reloadKey]);

  // Close everything on unmount.
  useEffect(() => {
    const sockets = socketsRef.current;
    return () => {
      for (const [, sock] of sockets) sock.close();
      sockets.clear();
    };
  }, []);

  // Flush buffered lines into state on a short interval.
  useEffect(() => {
    const id = setInterval(() => {
      if (pendingRef.current.length === 0) return;
      const incoming = pendingRef.current;
      pendingRef.current = [];
      setLines((prev) => [...prev, ...incoming].slice(-max));
    }, 200);
    return () => clearInterval(id);
  }, [max]);

  const clear = () => {
    pendingRef.current = [];
    setLines([]);
  };

  return { lines, status, clear };
}

// ---------------------------------------------------------------------------
// Filter mini-language
//
// A small, CloudWatch-inspired but simpler syntax for filtering log lines.
// Space-separated terms are ANDed together:
//   foo            line contains "foo" (case-insensitive)
//   "foo bar"      line contains the exact phrase "foo bar"
//   -foo           line does NOT contain "foo"
//   /re?gex/       line matches the regular expression (add i for ignore-case)
//   level:error    restrict to one or more levels (level:error,warn)
// ---------------------------------------------------------------------------

export interface CompiledFilter {
  includes: string[]; // lowercased substrings that must all be present
  excludes: string[]; // lowercased substrings that must all be absent
  regexes: RegExp[]; // regexes that must all match
  levels: Set<LogLevel> | null; // null = any level
  /** Source terms used for highlighting (includes + regexes). */
  highlight: (string | RegExp)[];
  error?: string; // a bad regex, surfaced to the user
}

const LEVELS: LogLevel[] = ["error", "warn", "info", "debug", "none"];

/** Tokenize a filter string, honouring quotes and /regex/ spans. */
function tokenize(input: string): string[] {
  const tokens: string[] = [];
  let i = 0;
  while (i < input.length) {
    if (input[i] === " ") {
      i++;
      continue;
    }
    let start = i;
    let prefix = "";
    if (input[i] === "-") {
      prefix = "-";
      i++;
      start = i;
    }
    if (input[i] === '"') {
      let j = input.indexOf('"', i + 1);
      if (j < 0) j = input.length;
      tokens.push(prefix + input.slice(i, j + 1));
      i = j + 1;
    } else if (input[i] === "/") {
      // Read to the next unescaped slash; allow trailing flag letters.
      let j = i + 1;
      while (j < input.length && !(input[j] === "/" && input[j - 1] !== "\\")) j++;
      // include closing slash + any flag chars up to a space
      let k = j + 1;
      while (k < input.length && input[k] !== " ") k++;
      tokens.push(prefix + input.slice(i, k));
      i = k;
    } else {
      let j = start;
      while (j < input.length && input[j] !== " ") j++;
      tokens.push(prefix + input.slice(start, j));
      i = j;
    }
  }
  return tokens.filter((t) => t.length > 0);
}

export function compileFilter(input: string): CompiledFilter {
  const f: CompiledFilter = {
    includes: [],
    excludes: [],
    regexes: [],
    levels: null,
    highlight: [],
  };
  const trimmed = input.trim();
  if (!trimmed) return f;

  for (const token of tokenize(trimmed)) {
    let t = token;
    const negate = t.startsWith("-");
    if (negate) t = t.slice(1);
    if (!t) continue;

    // level:error,warn
    if (t.toLowerCase().startsWith("level:")) {
      const wanted = t
        .slice("level:".length)
        .split(",")
        .map((s) => s.trim().toLowerCase())
        .filter((s) => (LEVELS as string[]).includes(s)) as LogLevel[];
      if (wanted.length > 0) f.levels = new Set(wanted);
      continue;
    }

    // /regex/flags
    if (t.startsWith("/") && t.lastIndexOf("/") > 0) {
      const close = t.lastIndexOf("/");
      const body = t.slice(1, close);
      const flags = t.slice(close + 1).replace(/[^gimsuy]/g, "");
      try {
        const re = new RegExp(body, flags);
        if (negate) {
          // Mark a negated regex so matchLine inverts it (line must NOT match).
          f.regexes.push(Object.assign(re, { __negate: true }) as RegExp);
        } else {
          f.regexes.push(re);
          f.highlight.push(re);
        }
      } catch {
        f.error = `Invalid regex: ${body}`;
      }
      continue;
    }

    // "quoted phrase" or bare word
    let phrase = t;
    if (phrase.startsWith('"') && phrase.endsWith('"') && phrase.length >= 2) {
      phrase = phrase.slice(1, -1);
    }
    if (!phrase) continue;
    if (negate) {
      f.excludes.push(phrase.toLowerCase());
    } else {
      f.includes.push(phrase.toLowerCase());
      f.highlight.push(phrase);
    }
  }
  return f;
}

/** Whether a parsed line passes the compiled filter. */
export function matchLine(line: LogLine, f: CompiledFilter): boolean {
  if (f.levels && !f.levels.has(line.level)) return false;
  const lower = line.text.toLowerCase();
  for (const inc of f.includes) if (!lower.includes(inc)) return false;
  for (const exc of f.excludes) if (lower.includes(exc)) return false;
  for (const re of f.regexes) {
    re.lastIndex = 0;
    const negate = (re as RegExp & { __negate?: boolean }).__negate;
    const hit = re.test(line.text);
    if (negate ? hit : !hit) return false;
  }
  return true;
}

/** A segment of a line for highlighting filter matches. */
export interface Segment {
  text: string;
  hit: boolean;
}

/** Split text into highlighted / plain segments for the active filter. */
export function highlightSegments(text: string, f: CompiledFilter): Segment[] {
  if (f.highlight.length === 0) return [{ text, hit: false }];
  const ranges: [number, number][] = [];

  for (const h of f.highlight) {
    if (typeof h === "string") {
      const needle = h.toLowerCase();
      const hay = text.toLowerCase();
      let from = 0;
      while (needle && from <= hay.length) {
        const idx = hay.indexOf(needle, from);
        if (idx < 0) break;
        ranges.push([idx, idx + needle.length]);
        from = idx + needle.length;
      }
    } else {
      const re = new RegExp(h.source, h.flags.includes("g") ? h.flags : h.flags + "g");
      let m: RegExpExecArray | null;
      let guard = 0;
      while ((m = re.exec(text)) && guard++ < 1000) {
        if (m[0].length === 0) {
          re.lastIndex++;
          continue;
        }
        ranges.push([m.index, m.index + m[0].length]);
      }
    }
  }
  if (ranges.length === 0) return [{ text, hit: false }];

  // Merge overlapping ranges.
  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [ranges[0]];
  for (let i = 1; i < ranges.length; i++) {
    const last = merged[merged.length - 1];
    if (ranges[i][0] <= last[1]) {
      last[1] = Math.max(last[1], ranges[i][1]);
    } else {
      merged.push(ranges[i]);
    }
  }

  const segments: Segment[] = [];
  let pos = 0;
  for (const [s, e] of merged) {
    if (s > pos) segments.push({ text: text.slice(pos, s), hit: false });
    segments.push({ text: text.slice(s, e), hit: true });
    pos = e;
  }
  if (pos < text.length) segments.push({ text: text.slice(pos), hit: false });
  return segments;
}

/** Memo-friendly hook wrapper around compileFilter. */
export function useCompiledFilter(input: string): CompiledFilter {
  return useMemo(() => compileFilter(input), [input]);
}
