import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Box,
  Button,
  Icon,
  StatePill,
  Table,
  Text,
  type Color,
  type Column,
  type ColumnOption,
} from "@/ui/kit";
import { eventsWebSocketURL, listHostEvents, listHosts } from "../api/client";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import { useHost, isAllHosts } from "../HostContext";
import type { DockerEvent } from "../types";

// Activity: a live, newest-first log of Docker engine events for the selected
// host, or for every online host merged together in the "All hosts" view.
//
// Per host: one REST fetch of the last hour (bounded server-side to 500), then a
// WebSocket that replays from the fetch time and follows live. Duplicates from
// that overlap are dropped by the server-issued event id. Incoming events are
// batched and flushed a few times a second so a burst (a compose up, a prune)
// costs one render, not hundreds.

const MAX_ROWS = 1000;
const FLUSH_MS = 300;
const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 30000;

interface HostRef {
  id: string;
  name: string;
}

type Row = DockerEvent & { key: string; hostId: string; hostName: string; ts: number };
type HostStatus = "connecting" | "live" | "error" | "forbidden";

const TYPE_OPTIONS: ColumnOption[] = [
  { value: "container", label: "Container" },
  { value: "image", label: "Image" },
  { value: "network", label: "Network" },
  { value: "volume", label: "Volume" },
];

// Verb -> tone. Lifecycle starts are green, abnormal ends red, transitions
// amber, things coming into existence blue, removals and the rest gray.
function actionTone(ev: DockerEvent): Color {
  const a = ev.action.toLowerCase();
  if (a === "health_status") {
    const d = (ev.attributes.detail ?? "").toLowerCase();
    return d === "healthy" ? "green" : d === "unhealthy" ? "red" : "amber";
  }
  if (a === "die") return ev.attributes.exitCode === "0" ? "gray" : "red";
  switch (a) {
    case "start":
    case "unpause":
      return "green";
    case "kill":
    case "oom":
      return "red";
    case "restart":
    case "pause":
    case "stop":
      return "amber";
    case "pull":
    case "create":
    case "tag":
    case "push":
    case "import":
    case "load":
    case "build":
    case "connect":
    case "mount":
      return "blue";
    default:
      return "gray";
  }
}

// Actions after which the object no longer exists: no detail link.
const GONE = new Set(["destroy", "delete", "remove", "untag", "prune"]);

function detailHref(r: Row): string | undefined {
  if (GONE.has(r.action)) return undefined;
  const host = `?host=${encodeURIComponent(r.hostId)}`;
  switch (r.type) {
    case "container":
      return r.actorId ? `/containers/${encodeURIComponent(r.actorId)}${host}` : undefined;
    case "image":
      return r.actorId ? `/images/${encodeURIComponent(r.actorId)}${host}` : undefined;
    case "network":
      return r.actorId ? `/networks/${encodeURIComponent(r.actorId)}${host}` : undefined;
    case "volume": {
      const name = r.actorName || r.actorId;
      return name ? `/volumes/${encodeURIComponent(name)}${host}` : undefined;
    }
    default:
      return undefined;
  }
}

function actorLabel(r: Row): string {
  if (r.actorName) return r.actorName;
  return r.actorId.startsWith("sha256:") ? r.actorId.slice(7, 19) : r.actorId.slice(0, 12);
}

function hhmmss(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// Short, human attribute summary: exit code, image, health / exec detail,
// signal, and the container a network event concerns.
function detailParts(r: Row): { text: string; tone?: Color }[] {
  const a = r.attributes;
  const out: { text: string; tone?: Color }[] = [];
  if (a.exitCode !== undefined) out.push({ text: `exit ${a.exitCode}`, tone: a.exitCode === "0" ? undefined : "red" });
  if (a.signal) out.push({ text: `signal ${a.signal}` });
  if (a.detail) out.push({ text: a.detail });
  if (r.type === "container" && r.image) out.push({ text: r.image });
  if (r.type === "network" && a.container) out.push({ text: `container ${a.container.slice(0, 12)}` });
  if (r.type === "volume" && a.driver && a.driver !== "local") out.push({ text: a.driver });
  return out;
}

function toRow(ev: DockerEvent, host: HostRef): Row {
  return { ...ev, key: `${host.id}:${ev.id}`, hostId: host.id, hostName: host.name, ts: Date.parse(ev.time) || 0 };
}

/**
 * Keeps one history fetch + live socket per host and returns the merged,
 * newest-first event list. While `paused`, new events are held back (and
 * counted) instead of shifting the rows under the reader.
 */
function useActivityStream(hosts: HostRef[], paused: boolean) {
  const [rows, setRows] = useState<Row[]>([]);
  const [pending, setPending] = useState(0);
  const [status, setStatus] = useState<Record<string, HostStatus>>({});
  const [loading, setLoading] = useState(true);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const hostKey = hosts.map((h) => `${h.id}=${h.name}`).join(",");

  useEffect(() => {
    let disposed = false;
    const incoming: Row[] = [];
    const seen = new Set<string>();
    const sockets = new Set<WebSocket>();
    const timers = new Set<number>();
    let historyLeft = hosts.length;

    setRows([]);
    setPending(0);
    setLoading(hosts.length > 0);
    setStatus(Object.fromEntries(hosts.map((h) => [h.id, "connecting" as HostStatus])));

    const mark = (id: string, s: HostStatus) =>
      setStatus((prev) => (prev[id] === s ? prev : { ...prev, [id]: s }));

    const push = (ev: DockerEvent, host: HostRef) => {
      const row = toRow(ev, host);
      if (seen.has(row.key)) return;
      seen.add(row.key);
      incoming.push(row);
    };

    const historyDone = () => {
      historyLeft -= 1;
      if (historyLeft <= 0 && !disposed) setLoading(false);
    };

    const connect = (host: HostRef, since: number, attempt: number) => {
      if (disposed) return;
      let lastSec = since;
      let opened = false;
      const sock = new WebSocket(eventsWebSocketURL(host.id, { since }));
      sockets.add(sock);
      sock.onopen = () => {
        opened = true;
        mark(host.id, "live");
      };
      sock.onmessage = (msg) => {
        const data = typeof msg.data === "string" ? msg.data : "";
        if (data.startsWith("error:")) {
          mark(host.id, "error");
          return;
        }
        try {
          const ev = JSON.parse(data) as DockerEvent;
          lastSec = Math.max(lastSec, Math.floor((Date.parse(ev.time) || 0) / 1000));
          push(ev, host);
        } catch {
          /* ignore a malformed frame */
        }
      };
      sock.onclose = () => {
        sockets.delete(sock);
        if (disposed) return;
        mark(host.id, "error");
        const next = opened ? 0 : attempt + 1;
        const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** next);
        const t = window.setTimeout(() => {
          timers.delete(t);
          mark(host.id, "connecting");
          connect(host, lastSec, next);
        }, delay);
        timers.add(t);
      };
    };

    for (const host of hosts) {
      const since = Math.floor(Date.now() / 1000);
      listHostEvents(host.id)
        .then((evs) => {
          if (disposed) return;
          // History arrives newest first; push oldest first so ordering ties
          // resolve the same way as live events.
          for (let i = evs.length - 1; i >= 0; i -= 1) push(evs[i], host);
        })
        .then(() => true)
        .catch((err) => {
          // 403: this principal has no host-wide access (team-scoped user), so
          // the live stream would be refused too. Do not retry it forever.
          const status = (err as { response?: { status?: number } })?.response?.status;
          mark(host.id, status === 403 ? "forbidden" : "error");
          return status !== 403;
        })
        .then((stream) => {
          historyDone();
          if (stream) connect(host, since, 0);
        });
    }

    const flush = window.setInterval(() => {
      if (incoming.length === 0) return;
      if (pausedRef.current) {
        // A long pause must not grow without bound: keep the newest arrivals.
        if (incoming.length > MAX_ROWS) {
          for (const r of incoming.splice(0, incoming.length - MAX_ROWS)) seen.delete(r.key);
        }
        setPending(incoming.length);
        return;
      }
      const batch = incoming.splice(0, incoming.length);
      setPending(0);
      setRows((prev) => {
        const merged = prev.concat(batch);
        merged.sort((a, b) => b.ts - a.ts);
        if (merged.length > MAX_ROWS) {
          for (const r of merged.slice(MAX_ROWS)) seen.delete(r.key);
          merged.length = MAX_ROWS;
        }
        return merged;
      });
    }, FLUSH_MS);

    return () => {
      disposed = true;
      window.clearInterval(flush);
      timers.forEach((t) => window.clearTimeout(t));
      sockets.forEach((s) => s.close());
    };
    // hostKey captures the host set; `hosts` itself is a fresh array each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostKey]);

  return { rows, pending, status, loading };
}

export default function Activity() {
  const { hostId } = useHost();
  const aggregated = isAllHosts(hostId);
  const [paused, setPaused] = useState(false);

  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, refetchInterval: 15000 });

  const hosts = useMemo<HostRef[]>(() => {
    const all = hostsQuery.data ?? [];
    if (aggregated) return all.filter((h) => h.online).map((h) => ({ id: h.id, name: h.name || h.id }));
    const h = all.find((x) => x.id === hostId);
    return [{ id: hostId, name: h?.name || hostId }];
  }, [aggregated, hostId, hostsQuery.data]);

  const ready = !aggregated || hostsQuery.isSuccess;
  const { rows, pending, status, loading } = useActivityStream(ready ? hosts : [], paused);

  const statuses = Object.values(status);
  const liveCount = statuses.filter((s) => s === "live").length;
  const failing = hosts.filter((h) => status[h.id] === "error").map((h) => h.name);

  const actionOptions = useMemo<ColumnOption[]>(() => {
    const set = new Set(rows.map((r) => r.action));
    return [...set].sort().map((a) => ({ value: a, label: a.replace(/_/g, " ") }));
  }, [rows]);

  const columns: Column<Row>[] = [
    {
      key: "time",
      header: "Time",
      width: 84,
      accessor: (r) => hhmmss(r.ts),
      cell: (r) => (
        <Text mono size="sm" color="muted" title={new Date(r.ts).toLocaleString()}>
          {hhmmss(r.ts)}
        </Text>
      ),
    },
    {
      key: "host",
      header: "Host",
      hidden: !aggregated,
      accessor: (r) => r.hostName,
      truncate: true,
      maxWidth: 160,
      filter: { kind: "select", options: hosts.map((h) => ({ value: h.name, label: h.name })) },
    },
    {
      key: "type",
      header: "Type",
      width: 96,
      accessor: (r) => r.type,
      cell: (r) => (
        <Text size="sm" color="muted">
          {r.type}
        </Text>
      ),
      filter: { kind: "select", options: TYPE_OPTIONS },
    },
    {
      key: "action",
      header: "Action",
      width: 140,
      accessor: (r) => r.action,
      cell: (r) => <StatePill size="sm" status={r.action} color={actionTone(r)} label={r.action.replace(/_/g, " ")} />,
      filter: { kind: "select", options: actionOptions },
    },
    {
      key: "actor",
      header: "Object",
      truncate: true,
      maxWidth: 320,
      accessor: (r) => `${r.actorName} ${r.actorId}`,
      cell: (r) => {
        const href = detailHref(r);
        const label = actorLabel(r);
        return href ? (
          <Link to={href} className="row-link font-mono" title={r.actorId}>
            {label}
          </Link>
        ) : (
          <Text mono size="sm" title={r.actorId}>
            {label}
          </Text>
        );
      },
    },
    {
      key: "details",
      header: "Details",
      grow: true,
      truncate: true,
      accessor: (r) => detailParts(r).map((p) => p.text).join(" "),
      cell: (r) => {
        const parts = detailParts(r);
        if (parts.length === 0) return <Text color="muted">—</Text>;
        return (
          <Box gap={3} align="center" style={{ minWidth: 0 }}>
            {parts.map((p, i) => (
              <Text key={i} mono size="sm" color={p.tone ?? "muted"} truncate>
                {p.text}
              </Text>
            ))}
          </Box>
        );
      },
    },
  ];

  const streamPill = paused ? (
    <StatePill status="paused" label="Paused" detail={pending > 0 ? `${pending} new` : undefined} />
  ) : liveCount > 0 ? (
    <StatePill
      status="live"
      color="green"
      label="Live"
      detail={aggregated ? `${liveCount}/${hosts.length} hosts` : undefined}
    />
  ) : statuses.length > 0 && statuses.every((s) => s === "forbidden") ? (
    <StatePill status="forbidden" color="gray" label="No host access" />
  ) : statuses.some((s) => s === "connecting") ? (
    <StatePill status="connecting" color="amber" label="Connecting" />
  ) : hosts.length > 0 ? (
    <StatePill status="disconnected" color="red" label="Disconnected" />
  ) : null;

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Activity"
        description={
          aggregated
            ? "Docker events across every online host, newest first."
            : "Docker events on this host, newest first."
        }
        actions={
          <Button
            variant="outline"
            color="neutral"
            size="sm"
            iconLeft={paused ? "play-arrow-rounded" : "pause-rounded"}
            onClick={() => setPaused((p) => !p)}
            aria-pressed={paused}
          >
            {paused ? (pending > 0 ? `Resume (${pending})` : "Resume") : "Pause"}
          </Button>
        }
      />

      {failing.length > 0 && !loading && (
        <Text size="sm" color="warning">
          No live stream from {failing.join(", ")}. Retrying.
        </Text>
      )}

      <Table<Row>
        columns={columns}
        data={rows}
        rowKey="key"
        globalSearch
        searchPlaceholder="Search events…"
        loading={!ready || (loading && rows.length === 0)}
        stickyHeader
        toolbar={streamPill}
        emptyState={
          <EmptyState
            icon={<Icon name="history-rounded" size="xl" />}
            title={aggregated && hosts.length === 0 ? "No online hosts" : "No events in the last hour"}
            description={
              aggregated && hosts.length === 0
                ? "Events appear here once a host is connected."
                : "Start, stop or pull something and it shows up here as it happens."
            }
          />
        }
      />
    </Box>
  );
}
