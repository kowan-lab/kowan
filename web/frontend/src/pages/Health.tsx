import { useQuery } from "@tanstack/react-query";
import { Box, Callout, Icon, Table, Text, type Column } from "@/ui/kit";
import { getSystemHealth } from "../api/client";
import { Band, Fact, LBL, Pill, Tag } from "../components/apps/ui";
import PageHeader from "../components/PageHeader";
import { errorMessage, formatBytes, formatDate, timeAgo } from "../utils";
import type { SystemHealth } from "../types";

type HostRow = SystemHealth["hosts"][number];
type LoopRow = SystemHealth["loops"][number];

function every(s: number): string {
  if (s < 60) return `every ${s}s`;
  if (s < 3600) return `every ${Math.round(s / 60)} min`;
  return `every ${Math.round(s / 3600)} h`;
}

/** Kowan's own health: storage, hosts and their latency, background loops, work in flight. */
export default function Health() {
  const q = useQuery({ queryKey: ["system-health"], queryFn: getSystemHealth, refetchInterval: 10_000 });
  const h = q.data;
  const hostCols: Column<HostRow>[] = [
    { key: "name", header: "Host", accessor: (r) => r.name, cell: (r) => <Text weight="semibold">{r.name}</Text> },
    { key: "state", header: "State", cell: (r) => <Pill tone={r.online ? "g" : "r"}>{r.online ? "Online" : "Offline"}</Pill> },
    { key: "ping", header: "Round trip", accessor: (r) => r.pingMs ?? Infinity,
      cell: (r) => <Text size="sm" color={r.pingMs != null && r.pingMs > 500 ? "warning" : undefined}>{r.pingMs != null ? `${r.pingMs} ms` : r.error || "—"}</Text> },
    { key: "seen", header: "Last seen", cell: (r) => <Text size="sm">{r.lastSeen ? timeAgo(r.lastSeen) : "—"}</Text> },
    { key: "crash", header: "Crash watch", cell: (r) => <Text size="sm" color={r.crashWatch ? undefined : "muted"}>{r.crashWatch ? "Following" : "Not connected"}</Text> },
    { key: "groups", header: "Groups", cell: (r) => <Box direction="row" gap={1} wrap>{r.tags.map((t) => <Tag key={t}>{t}</Tag>)}</Box> },
  ];
  const loopCols: Column<LoopRow>[] = [
    { key: "name", header: "Loop", accessor: (r) => r.name,
      cell: (r) => <Box direction="column"><Text weight="semibold">{r.name}</Text><Text size="xs" color="muted">{r.description}</Text></Box> },
    { key: "last", header: "Last run", cell: (r) => <Text size="sm" title={r.lastRun ? formatDate(r.lastRun) : undefined}>{r.lastRun ? timeAgo(r.lastRun) : "not yet"}</Text> },
    { key: "every", header: "Runs", cell: (r) => <Text size="sm">{every(r.everySeconds)}</Text> },
    { key: "state", header: "State", cell: (r) => <Pill tone={r.late ? "a" : "g"}>{r.late ? "Late" : "On time"}</Pill> },
  ];
  const disk = h?.storage.disk;
  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Health"
        description="How Kowan itself is doing: its databases and disk, the round trip to each host, the background loops and the work in flight."
        icon={<Icon name="monitor-heart-outline-rounded" size="lg" />}
      />
      {q.isError && <Callout color="danger" title="Could not load the health">{errorMessage(q.error, "")}</Callout>}
      {h && (
        <>
          {h.status === "ok" ? (
            <Callout color="green" title="Everything is running">No loop is late, every host answers and there is room on the disk.</Callout>
          ) : (
            <Callout color="yellow" title="Needs a look">
              <ul className="m-0 pl-[18px]">{h.problems.map((p) => <li key={p}>{p}</li>)}</ul>
            </Callout>
          )}
          <Band card padded title="Server">
            <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-[12px]">
              <Fact label="Version" mono>{h.server.version}</Fact>
              <Fact label="Running since">{timeAgo(h.server.startedAt)}</Fact>
              <Fact label="Python" mono>{h.server.python}</Fact>
              {h.storage.files.map((f) => (
                <Fact key={f.name} label={`${f.name} · ${f.description}`}>{formatBytes(f.bytes)}</Fact>
              ))}
              {disk && disk.total > 0 && (
                <Fact label="Free disk (data folder)">{`${formatBytes(disk.free)} of ${formatBytes(disk.total)}`}</Fact>
              )}
            </div>
            <span className={LBL}>
              Running now: {h.work.runbooksRunning} runbook{h.work.runbooksRunning === 1 ? "" : "s"}, {h.work.pulls} pull{h.work.pulls === 1 ? "" : "s"},{" "}
              {h.work.websockets} open WebSocket{h.work.websockets === 1 ? "" : "s"} · {h.work.crashSnapshotsTaken} crash snapshot
              {h.work.crashSnapshotsTaken === 1 ? "" : "s"} since start
            </span>
          </Band>
          <Band card title="Hosts" count={h.hosts.length}>
            <Table<HostRow> data={h.hosts} columns={hostCols} rowKey={(r) => r.id} />
          </Band>
          <Band card title="Background loops" count={h.loops.length}>
            <Table<LoopRow> data={h.loops} columns={loopCols} rowKey={(r) => r.name} />
          </Band>
        </>
      )}
    </Box>
  );
}
