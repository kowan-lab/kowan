import { lazy, Suspense, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  Box,
  Button,
  Callout,
  Select,
  Skeleton,
  StatePill,
  Table,
  Text,
  Tooltip,
  type Column,
} from "@/ui/kit";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { useHost, isAllHosts } from "../HostContext";
import { LIVE_REFETCH_MS } from "../lib/live";
import { errorMessage } from "../utils";
import { getNetMap, type NetGroup, type NetMap, type NetNode } from "../components/netmap/api";

const NetMapGraph = lazy(() => import("../components/netmap/NetMapGraph"));

/** Above this many nodes the graph stops being readable: default to the table. */
const GRAPH_NODE_LIMIT = 300;

interface Row {
  id: string;
  host: string;
  stack: string;
  container: NetNode;
  networks: string[];
  ingress: string[];
  dependsOn: string[];
}

function rowsOf(map: NetMap): Row[] {
  const byId = new Map(map.nodes.map((n) => [n.id, n]));
  const hostName = new Map(map.hosts.map((h) => [h.id, h.name]));
  const rows = new Map<string, Row>();
  for (const n of map.nodes) {
    if (n.kind !== "container") continue;
    rows.set(n.id, { id: n.id, host: hostName.get(n.hostId) ?? n.hostId, stack: n.stack, container: n, networks: [], ingress: [], dependsOn: [] });
  }
  for (const e of map.edges) {
    const a = byId.get(e.from);
    const b = byId.get(e.to);
    if (!a || !b) continue;
    if (e.kind === "network") rows.get(a.id)?.networks.push(b.label);
    if (e.kind === "port" || e.kind === "domain") rows.get(b.id)?.ingress.push(a.label);
    if (e.kind === "depends") rows.get(a.id)?.dependsOn.push(b.label);
  }
  return [...rows.values()];
}

export default function NetworkMap() {
  const { hostId } = useHost();
  const all = isAllHosts(hostId);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const stack = params.get("stack") ?? "";
  const network = params.get("network") ?? "";
  const [forceView, setForceView] = useState<"graph" | "table" | null>(null);

  const setFilter = (k: "stack" | "network", v: string) => {
    const next = new URLSearchParams(params);
    if (v) next.set(k, v);
    else next.delete(k);
    setParams(next, { replace: true });
  };

  const q = useQuery({
    queryKey: ["netmap", all ? "*" : hostId, stack, network],
    queryFn: () => getNetMap(all ? null : hostId, { stack, network }),
    enabled: Boolean(hostId),
    refetchInterval: LIVE_REFETCH_MS,
    placeholderData: (prev) => prev,
  });
  const map = q.data;
  const big = (map?.nodes.length ?? 0) > GRAPH_NODE_LIMIT;
  const view = forceView ?? (big ? "table" : "graph");
  const rows = useMemo(() => (map ? rowsOf(map) : []), [map]);

  const openNode = (n: NetNode) => {
    if (n.kind === "network" && n.ref.networkId) navigate(`/networks/${n.ref.networkId}?host=${encodeURIComponent(n.hostId)}`);
    else if (n.ref.containerId) navigate(`/containers/${n.ref.containerId}?host=${encodeURIComponent(n.hostId)}`);
  };
  const openGroup = (g: NetGroup) => {
    if (g.deploymentId) navigate(`/workloads/dep~${g.deploymentId}`);
  };

  const columns: Column<Row>[] = [
    { key: "container", header: "Container", sortable: true, accessor: (r) => r.container.label,
      cell: (r) => <Text as="button" size="sm" color="info" onClick={() => openNode(r.container)} style={{ background: "none", border: 0, padding: 0, cursor: "pointer" }}>{r.container.label}</Text> },
    { key: "state", header: "State", accessor: (r) => r.container.state, cell: (r) => <StatePill status={r.container.state || "unknown"} size="sm" /> },
    ...(all ? [{ key: "host", header: "Host", sortable: true } as Column<Row>] : []),
    { key: "stack", header: "Stack", sortable: true, accessor: (r) => r.stack || "—" },
    { key: "networks", header: "Networks", accessor: (r) => r.networks.join(", ") },
    { key: "ingress", header: "Ingress", accessor: (r) => r.ingress.join(", ") },
    { key: "dependsOn", header: "Depends on", accessor: (r) => r.dependsOn.join(", ") },
  ];

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Network map"
        breadcrumbs={[{ label: "Networks", to: "/networks" }, { label: "Map" }]}
        description="Networks, containers grouped by stack, depends_on edges, and what reaches them from outside."
        actions={
          <Box direction="row" gap={2} wrap align="center">
            <Select aria-label="Filter by stack" placeholder="All stacks" width={180} clearable searchable value={stack || null}
              options={[{ value: "-", label: "standalone (no stack)" }, ...(map?.stacks ?? []).map((s) => ({ value: s, label: s }))]}
              onChange={(v) => setFilter("stack", v ?? "")} />
            <Select aria-label="Filter by network" placeholder="All networks" width={180} clearable searchable value={network || null}
              options={(map?.networks ?? []).map((s) => ({ value: s, label: s }))}
              onChange={(v) => setFilter("network", v ?? "")} />
            <Button variant="outline" color="neutral" onClick={() => setForceView(view === "graph" ? "table" : "graph")}>
              {view === "graph" ? "Table" : "Graph"}
            </Button>
            <Tooltip content="Refresh">
              <Button variant="outline" color="neutral" aria-label="Refresh" iconLeft="refresh-rounded" loading={q.isFetching} onClick={() => void q.refetch()} />
            </Tooltip>
          </Box>
        }
      />
      {map && (
        <Box direction="row" gap={2} wrap align="center">
          <Badge variant="outline" color="gray">{map.counts.containers} containers</Badge>
          <Badge variant="outline" color="gray">{map.counts.networks} networks</Badge>
          <Badge variant="outline" color="gray">{map.counts.ingress} ports / domains</Badge>
          {big && view === "table" && <Text size="xs" color="muted">Large host: showing the table. Switch to the graph if you need it.</Text>}
          {map.hosts.filter((h) => h.error).map((h) => <Badge key={h.id} variant="outline" color="red">{h.name}: {h.error}</Badge>)}
        </Box>
      )}
      {q.isLoading ? (
        <Skeleton height={420} />
      ) : q.isError ? (
        <Callout color="danger">{errorMessage(q.error, "Failed to load the map.")}</Callout>
      ) : map && view === "graph" ? (
        <SectionCard plain noBodyPadding>
          {map.nodes.length === 0 ? (
            <Box padding={4}><Text size="sm" color="muted">Nothing matches these filters.</Text></Box>
          ) : (
            <Suspense fallback={<Skeleton height={420} />}>
              <NetMapGraph map={map} onOpenNode={openNode} onOpenGroup={openGroup} />
            </Suspense>
          )}
          <Box direction="row" gap={4} wrap padding={2} style={{ borderTop: "1px solid var(--border)" }}>
            <Text size="xs" color="muted">▣ container · ≋ network · ⇥ published port · ◎ domain</Text>
            <Text size="xs" color="muted">dashed arrow: depends_on · dotted: proxy route · drag to pan, wheel to zoom, Tab + Enter to open</Text>
          </Box>
        </SectionCard>
      ) : map ? (
        <SectionCard plain noBodyPadding>
          <Table<Row> data={rows} columns={columns} rowKey={(r) => r.id} globalSearch pagination defaultPageSize={50} emptyText="Nothing matches these filters." />
        </SectionCard>
      ) : null}
    </Box>
  );
}
