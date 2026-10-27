import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Code,
  Icon,
  Skeleton,
  Spinner,
  Table,
  Tabs,
  Text,
  Tooltip,
} from "@/ui/kit";
import { Link, useParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { getHostSwarm, listHosts } from "../api/client";
import PageHeader from "../components/PageHeader";
import RenameHostDialog from "../components/hosts/RenameHostDialog";
import UpdatePanel from "../components/updates/UpdatePanel";
import StatusBadge from "../components/StatusBadge";
import { useAuth } from "../auth/AuthContext";
import { LIVE_REFETCH_MS } from "../lib/live";
import type { SwarmNode } from "../types";
import { errorMessage, formatDate, timeAgo } from "../utils";

// ---------------------------------------------------------------------------
// Host detail: the full-page replacement for the old inspect drawer. Reached
// via /hosts/:id — the host IS the entity (identified by its id), not a
// ?host=-scoped view. An Overview tab (identity/status) plus a Swarm tab that
// lists the Docker Swarm nodes when the host is a swarm manager. Swarm cluster
// formation (init/join) happens at the Docker level, outside Kowan — the Swarm
// tab is read-only.
// ---------------------------------------------------------------------------

export default function HostDetail() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState("overview");
  const [renaming, setRenaming] = useState(false);

  // There is no getHost(id) endpoint — derive the host from the shared
  // ["hosts"] list (the same cache the Hosts page fills) by id. Poll it live so
  // the header's status badge and "last seen" stay current.
  const hostsQuery = useQuery({
    queryKey: ["hosts"],
    queryFn: listHosts,
    refetchInterval: LIVE_REFETCH_MS,
  });

  const host = hostsQuery.data?.find((h) => h.id === id) ?? null;

  if (hostsQuery.isLoading) {
    return (
      <Box align="center" justify="center" style={{ minHeight: 320 }}>
        <Spinner />
      </Box>
    );
  }

  if (hostsQuery.isError) {
    return (
      <Box direction="column" gap={5}>
        <PageHeader
          breadcrumbs={[{ label: "Hosts", to: "/hosts" }]}
          title="Host"
          icon={<Icon name="dns-outline" size="lg" />}
        />
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Failed to load hosts"
        >
          {errorMessage(hostsQuery.error, "Could not load the host list.")}
        </Callout>
      </Box>
    );
  }

  if (!host) {
    return (
      <Box direction="column" gap={5}>
        <PageHeader
          breadcrumbs={[{ label: "Hosts", to: "/hosts" }]}
          title="Host"
          icon={<Icon name="dns-outline" size="lg" />}
        />
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Host not found"
        >
          This host is not connected to this console. It may have been removed.
        </Callout>
      </Box>
    );
  }

  const isLocal = host.id === "local";
  const isEdgeHost = host.mode === "edge";

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        breadcrumbs={[{ label: "Hosts", to: "/hosts" }]}
        title={
          <Box direction="row" wrap align="center" gap={2}>
            <span>{host.name}</span>
            <StatusBadge status={host.online ? "online" : "offline"} />
          </Box>
        }
        icon={
          <Icon
            name={
              isLocal
                ? "dns-outline"
                : "storage-rounded"
            }
            size="lg"
          />
        }
        actions={
          <Box direction="row" gap={2}>
            {isAdmin && (
              <Button variant="outline" color="neutral" iconLeft="edit-outline-rounded" onClick={() => setRenaming(true)}>
                Rename
              </Button>
            )}
            {host.online && (
              <Link to={`/hosts/${encodeURIComponent(host.id)}/cleanup`} style={{ textDecoration: "none" }}>
                <Button variant="outline" color="neutral" iconLeft="mop-outline-rounded">Disk cleanup</Button>
              </Link>
            )}
          </Box>
        }
      />

      <RenameHostDialog host={renaming ? host : null} onClose={() => setRenaming(false)} />

      <Card variant="raised">
        <Card.Body>
          <Tabs value={tab} onValueChange={setTab}>
            <Tabs.List>
              <Tabs.Trigger value="overview">Overview</Tabs.Trigger>
              <Tabs.Trigger value="swarm">Swarm</Tabs.Trigger>
            </Tabs.List>

            <Tabs.Panel value="overview">
              <Box style={{ marginTop: 12 }}>
                <Table bordered={false}>
                  <Table.Body>
                    <Table.Row>
                      <Table.Cell style={{ width: 140 }}>
                        <Text size="sm" color="muted">
                          Name
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        <Text size="sm" weight="medium">
                          {host.name}
                        </Text>
                      </Table.Cell>
                    </Table.Row>
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Address
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        {host.address ? (
                          <Code>{host.address}</Code>
                        ) : (
                          <Text size="sm" color="muted">
                            -
                          </Text>
                        )}
                      </Table.Cell>
                    </Table.Row>
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Type
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        <Badge variant="light" color="gray">
                          {host.type || (isLocal ? "local" : "agent")}
                        </Badge>
                      </Table.Cell>
                    </Table.Row>
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Mode
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        <Badge
                          variant="light"
                          color={isEdgeHost ? "purple" : "blue"}
                        >
                          {host.mode || "agent"}
                        </Badge>
                      </Table.Cell>
                    </Table.Row>
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Status
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        <StatusBadge
                          status={host.online ? "online" : "offline"}
                        />
                      </Table.Cell>
                    </Table.Row>
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Last seen
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        {host.lastSeen ? (
                          <Tooltip content={formatDate(host.lastSeen)}>
                            <Text size="sm">{timeAgo(host.lastSeen)}</Text>
                          </Tooltip>
                        ) : (
                          <Text size="sm" color="muted">
                            -
                          </Text>
                        )}
                      </Table.Cell>
                    </Table.Row>
                  </Table.Body>
                </Table>

                {!isLocal && <UpdatePanel component="agent" hostId={host.id} isAdmin={isAdmin} />}
              </Box>
            </Tabs.Panel>

            <Tabs.Panel value="swarm">
              <Box style={{ marginTop: 12 }}>
                <HostSwarmTab hostId={host.id} />
              </Box>
            </Tabs.Panel>
          </Tabs>
        </Card.Body>
      </Card>
    </Box>
  );
}


// Swarm topology for a host. The host must be a reachable swarm manager to
// enumerate nodes; when it is not, the backend returns { active: false } and we
// show a hint instead of a table (joining/initing a swarm is a Docker-level op).
function HostSwarmTab({ hostId }: { hostId: string }) {
  const swarmQuery = useQuery({
    queryKey: ["host-swarm", hostId],
    queryFn: () => getHostSwarm(hostId),
    refetchInterval: LIVE_REFETCH_MS,
  });

  if (swarmQuery.isLoading) {
    return (
      <Box direction="column" gap={2}>
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} height={40} />
        ))}
      </Box>
    );
  }

  if (swarmQuery.isError) {
    return (
      <Callout
        color="danger"
        variant="light"
        iconLeft={<Icon name="warning-outline-rounded" />}
      >
        {errorMessage(swarmQuery.error, "Failed to load swarm topology.")}
      </Callout>
    );
  }

  const data = swarmQuery.data;
  if (!data || !data.active) {
    return (
      <Callout
        color="gray"
        variant="light"
        iconLeft={<Icon name="hub-outline-rounded" />}
      >
        This host is not a swarm manager — join or init a swarm on it (docker
        swarm init/join) to see nodes here.
      </Callout>
    );
  }

  return (
    <Box direction="column" gap={3}>
      <Box direction="row" align="center" gap={2}>
        <Text weight="semibold" size="sm">
          Nodes
        </Text>
        <Badge variant="light" color="gray">
          {data.nodes.length}
        </Badge>
      </Box>
      <Table striped hoverable scrollable style={{ minWidth: 720 }}>
        <Table.Header>
          <Table.Row>
            <Table.Head>Hostname</Table.Head>
            <Table.Head>Role</Table.Head>
            <Table.Head>Availability</Table.Head>
            <Table.Head>Status</Table.Head>
            <Table.Head>Address</Table.Head>
            <Table.Head>Engine</Table.Head>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {data.nodes.map((node) => (
            <SwarmNodeRow key={node.id} node={node} />
          ))}
        </Table.Body>
      </Table>
    </Box>
  );
}

function SwarmNodeRow({ node }: { node: SwarmNode }) {
  const statusColor =
    node.status === "ready" ? "green" : node.status === "down" ? "red" : "gray";
  return (
    <Table.Row>
      <Table.Cell>
        <Text size="sm" weight="medium">
          {node.hostname || "-"}
        </Text>
      </Table.Cell>
      <Table.Cell>
        <Box direction="row" align="center" gap={1} wrap={false}>
          <Badge
            variant="light"
            color={node.role === "manager" ? "blue" : "gray"}
          >
            {node.role}
          </Badge>
          {node.leader && (
            <Badge variant="light" color="purple">
              Leader
            </Badge>
          )}
        </Box>
      </Table.Cell>
      <Table.Cell>
        <Text size="sm">{node.availability || "-"}</Text>
      </Table.Cell>
      <Table.Cell>
        <Badge variant="light" color={statusColor}>
          {node.status || "unknown"}
        </Badge>
      </Table.Cell>
      <Table.Cell>
        <Text size="sm" color="muted">
          {node.address || "-"}
        </Text>
      </Table.Cell>
      <Table.Cell>
        <Text size="sm">{node.engineVersion || "-"}</Text>
      </Table.Cell>
    </Table.Row>
  );
}
