import { useMemo, useState } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
  Icon,
  Popup,
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
import { DeployProxy } from "../components/exposure/ExposeDialog";
import {
  getAllExposure,
  getCertificates,
  getHostExposure,
  type CertRow,
  type ExposedDomain,
  type ExposedPort,
  type HostExposure,
} from "../components/exposure/api";

const SCOPE_LABEL: Record<ExposedPort["scope"], string> = {
  public: "public",
  interface: "one interface",
  local: "local only",
};

function ownerLink(hostId: string, row: { containerId: string; containerName: string; deploymentId: string; deploymentName: string; stack: string }) {
  return (
    <Box direction="column" gap={0} style={{ minWidth: 0 }}>
      <Link to={`/containers/${row.containerId}?host=${encodeURIComponent(hostId)}`}>
        <Text size="sm" truncate>{row.containerName}</Text>
      </Link>
      {row.deploymentId ? (
        <Link to={`/workloads/dep~${row.deploymentId}`}>
          <Text size="xs" color="muted">deployment {row.deploymentName || row.stack}</Text>
        </Link>
      ) : row.stack ? (
        <Text size="xs" color="muted">stack {row.stack}</Text>
      ) : null}
    </Box>
  );
}

export default function Exposure() {
  const { hostId } = useHost();
  const all = isAllHosts(hostId);
  const q = useQuery({
    queryKey: ["exposure", all ? "*" : hostId],
    queryFn: async (): Promise<HostExposure[]> => (all ? getAllExposure() : [await getHostExposure(hostId)]),
    enabled: Boolean(hostId),
    refetchInterval: LIVE_REFETCH_MS,
  });
  const hosts = q.data ?? [];
  const totals = useMemo(
    () =>
      hosts.reduce(
        (a, h) => ({
          pub: a.pub + (h.summary.publicPorts ?? 0),
          risky: a.risky + (h.summary.riskyPublic ?? 0),
          domains: a.domains + (h.summary.domains ?? 0),
        }),
        { pub: 0, risky: 0, domains: 0 },
      ),
    [hosts],
  );

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Exposure"
        description="Everything reachable from outside: published ports by bind address, and domains routed by Traefik."
        actions={
          <Tooltip content="Refresh">
            <Button variant="outline" color="neutral" aria-label="Refresh" iconLeft="refresh-rounded"
              loading={q.isFetching} onClick={() => void q.refetch()} />
          </Tooltip>
        }
      />
      {q.data && (
        <Box direction="row" gap={2} wrap>
          <Badge variant="outline" color="gray" size="lg">{totals.pub} public ports</Badge>
          <Badge variant="outline" color={totals.risky ? "warning" : "gray"} size="lg">{totals.risky} sensitive services public</Badge>
          <Badge variant="outline" color="gray" size="lg">{totals.domains} domains</Badge>
        </Box>
      )}
      {q.isLoading ? (
        <Skeleton height={260} />
      ) : q.isError ? (
        <Callout color="danger">{errorMessage(q.error, "Failed to load exposure.")}</Callout>
      ) : (
        hosts.map((h) => <HostBand key={h.hostId} host={h} showName={all} />)
      )}
    </Box>
  );
}

function HostBand({ host, showName }: { host: HostExposure; showName: boolean }) {
  const [deployOpen, setDeployOpen] = useState(false);
  const [wantCerts, setWantCerts] = useState(false);
  const hasTls = host.domains.some((d) => d.tls);
  const certs = useQuery({
    queryKey: ["exposure-certs", host.hostId],
    queryFn: () => getCertificates(host.hostId),
    enabled: wantCerts && !host.error,
    staleTime: 60_000,
  });
  const certBy = useMemo(() => {
    const m = new Map<string, CertRow>();
    for (const r of certs.data?.domains ?? []) m.set(`${r.router}|${r.domain}`, r);
    return m;
  }, [certs.data]);

  const portCols: Column<ExposedPort>[] = [
    {
      key: "bind", header: "Bind", sortable: true,
      accessor: (p) => `${p.ip}:${p.publicPort}`,
      cell: (p) => <Code>{p.ip.includes(":") ? `[${p.ip}]` : p.ip}:{p.publicPort}/{p.protocol}</Code>,
    },
    { key: "privatePort", header: "Container port", numeric: true, sortable: true },
    {
      key: "scope", header: "Reach", sortable: true,
      filter: { kind: "select", options: [
        { value: "public", label: "public" }, { value: "interface", label: "one interface" }, { value: "local", label: "local only" },
      ] },
      cell: (p) => (
        <Box direction="row" gap={1} align="center" wrap>
          <StatePill status={p.scope} label={SCOPE_LABEL[p.scope]} color={p.warn ? "warning" : p.scope === "local" ? "gray" : "blue"} size="sm" />
          {p.risk && (
            <Badge size="sm" variant={p.warn ? "solid" : "outline"} color={p.warn ? "warning" : "gray"}
              iconLeft={p.warn ? "warning-outline-rounded" : undefined}
              title={p.warn ? `${p.risk.label} is reachable from any interface` : undefined}>
              {p.risk.label}
            </Badge>
          )}
        </Box>
      ),
    },
    { key: "owner", header: "Container", accessor: (p) => p.containerName, sortable: true, cell: (p) => ownerLink(host.hostId, p) },
    { key: "state", header: "State", cell: (p) => <StatePill status={p.state} size="sm" /> },
  ];

  const domainCols: Column<ExposedDomain>[] = [
    {
      key: "domain", header: "Domain", sortable: true,
      cell: (d) => (
        <Box direction="column" gap={0}>
          <a href={`${d.tls ? "https" : "http"}://${d.domain}`} target="_blank" rel="noreferrer"><Text size="sm">{d.domain}</Text></a>
          <Text size="xs" color="muted">{d.proxy} · {d.router}{d.entrypoints.length ? ` · ${d.entrypoints.join(",")}` : ""}</Text>
        </Box>
      ),
    },
    {
      key: "tls", header: "HTTPS",
      cell: (d) => {
        const c = certBy.get(`${d.router}|${d.domain}`);
        const cert = c?.certificate;
        if (!d.tls) return <StatePill status="http" label="http only" color="warning" size="sm" />;
        if (cert) {
          const color = cert.status === "valid" ? "green" : cert.status === "expiring" ? "warning" : "red";
          return (
            <Tooltip content={`${cert.issuer} · expires ${cert.notAfter}`}>
              <span><StatePill status={cert.status} color={color} detail={`${cert.daysLeft}d${cert.staging ? " · staging" : ""}`} size="sm" /></span>
            </Tooltip>
          );
        }
        return <Text size="xs" color="muted">{d.certResolver ? `resolver ${d.certResolver}` : "tls (no resolver)"}</Text>;
      },
    },
    {
      key: "router", header: "Router",
      cell: (d) => {
        const c = certBy.get(`${d.router}|${d.domain}`);
        if (!d.enabled) return <StatePill status="disabled" color="gray" size="sm" />;
        if (!d.proxyRunning) return <StatePill status="no proxy" color="red" size="sm" />;
        if (c?.routerStatus) {
          return (
            <Tooltip content={c.routerErrors.join("; ") || "reported by the Traefik API"}>
              <span><StatePill status={c.routerStatus} color={c.routerStatus === "enabled" ? "green" : "warning"} size="sm" /></span>
            </Tooltip>
          );
        }
        return <Text size="xs" color="muted">configured</Text>;
      },
    },
    { key: "owner", header: "Routes to", accessor: (d) => d.containerName, sortable: true, cell: (d) => ownerLink(host.hostId, d) },
    { key: "port", header: "Port", cell: (d) => <Text size="xs" color="muted">{d.port || "auto"}</Text> },
    { key: "by", header: "Set by", cell: (d) => <Text size="xs" color="muted">{d.managedByKowan ? "Kowan" : "labels"}</Text> },
  ];

  const running = host.proxies.filter((p) => p.state === "running");

  return (
    <SectionCard
      title={showName ? host.hostName : "Published ports"}
      description={host.error ? undefined : `${host.summary.publicPorts ?? 0} public · ${host.summary.interfacePorts ?? 0} on one interface · ${host.summary.localPorts ?? 0} local only`}
      actions={
        host.summary.riskyPublic ? (
          <Badge color="warning" variant="outline" iconLeft="warning-outline-rounded">
            {host.summary.riskyPublic} sensitive {host.summary.riskyPublic === 1 ? "service" : "services"} public
          </Badge>
        ) : undefined
      }
      noBodyPadding
    >
      {host.error ? (
        <Box padding={3}><Callout color="danger">{host.error}</Callout></Box>
      ) : (
        <>
          <Table<ExposedPort>
            data={host.ports}
            columns={portCols}
            rowKey={(p) => `${p.containerId}-${p.ip}-${p.publicPort}-${p.protocol}`}
            emptyText="No published ports."
          />
          <Box direction="row" justify="between" align="center" wrap gap={2} padding={3}
            style={{ borderTop: "1px solid var(--border)", background: "color-mix(in oklab, var(--muted) 50%, transparent)" }}>
            <Box direction="row" gap={2} align="center" wrap>
              <Text size="sm" weight="semibold">Domains</Text>
              {running.length === 0 ? (
                <Text size="xs" color="muted">No Traefik running on this host.</Text>
              ) : (
                running.map((p) => (
                  <Tooltip key={p.containerId} content={`${p.image} · networks ${p.networks.join(", ")}${p.certResolvers.length ? ` · resolvers ${p.certResolvers.join(", ")}` : ""}`}>
                    <span>
                      <Badge variant="outline" color="gray" iconLeft="route-outline">
                        {p.kind} · {p.name}
                      </Badge>
                    </span>
                  </Tooltip>
                ))
              )}
            </Box>
            <Box direction="row" gap={2}>
              {running.length === 0 && (
                <Button size="sm" variant="outline" color="neutral" iconLeft="add-rounded" onClick={() => setDeployOpen(true)}>
                  Deploy Traefik
                </Button>
              )}
              {hasTls && (
                <Button size="sm" variant="outline" color="neutral" iconLeft="lock-outline"
                  loading={certs.isFetching} onClick={() => (wantCerts ? void certs.refetch() : setWantCerts(true))}>
                  Check certificates
                </Button>
              )}
            </Box>
          </Box>
          {certs.data && certs.data.notes.length > 0 && (
            <Box padding={2}>
              <Callout color="gray">{certs.data.notes.join(" · ")}</Callout>
            </Box>
          )}
          {certs.isError && (
            <Box padding={2}><Callout color="danger">{errorMessage(certs.error, "Could not read certificates.")}</Callout></Box>
          )}
          <Table<ExposedDomain>
            data={host.domains}
            columns={domainCols}
            rowKey={(d) => `${d.containerId}-${d.router}-${d.domain}`}
            emptyText="No domains routed. Use Expose on a container or deployment."
          />
        </>
      )}
      <Popup open={deployOpen} onOpenChange={setDeployOpen} size="md">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title><Icon name="route-outline" size="sm" /> Deploy Traefik on {host.hostName}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            <DeployProxy hostId={host.hostId} view={host} />
          </Popup.Body>
        </Popup.Content>
      </Popup>
    </SectionCard>
  );
}
