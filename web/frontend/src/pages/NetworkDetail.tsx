import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Code,
  Divider,
  Icon,
  Spinner,
  Table,
  Text,
  Tooltip,
} from "@/ui/kit";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { disconnectContainer, inspectNetwork, removeNetwork } from "../api/client";
import PageHeader from "../components/PageHeader";
import ConfirmModal from "../components/ConfirmModal";
import { notify } from "../ui/toast";
import { useHost, isAllHosts } from "../HostContext";
import { liveUnlessGone } from "../lib/live";
import type { Network } from "../types";
import { errorMessage, shortId } from "../utils";

// Network detail: the full-page replacement for the old inspect drawer. Reached
// via /networks/:id?host=<hostId> (the host is carried in the query string so an
// aggregated "All hosts" row links back to the right daemon).

// Docker's built-in networks (bridge/host/none) are permanent and cannot be
// removed; only user-defined networks expose a Remove action.
const BUILTIN_NETWORKS = new Set(["bridge", "host", "none"]);
const isRemovable = (n: Network) => !BUILTIN_NETWORKS.has(n.name);

export default function NetworkDetail() {
  const { id = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { hostId: ctxHost } = useHost();
  // The host comes from the link (?host=); fall back to the globally selected
  // host so a hand-typed URL still resolves against something sensible.
  const host = searchParams.get("host") || ctxHost;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [removeOpen, setRemoveOpen] = useState(false);

  // Networks are per-daemon; "All hosts" can't resolve one (only reachable via a
  // hand-typed URL without ?host= — list links always carry it).
  const hostResolved = !isAllHosts(host);

  const inspectQuery = useQuery({
    queryKey: ["network-inspect", host, id],
    queryFn: () => inspectNetwork(host, id),
    enabled: Boolean(id) && hostResolved,
    refetchInterval: liveUnlessGone,
  });

  const removeMutation = useMutation({
    mutationFn: () => removeNetwork(host, id),
    onSuccess: () => {
      notify({ color: "green", title: "Network removed", message: "The network was removed" });
      queryClient.invalidateQueries({
        predicate: (q) => String(q.queryKey[0]).startsWith("networks"),
      });
      navigate("/networks");
    },
    onError: (err) =>
      notify({ color: "red", title: "Remove failed", message: errorMessage(err, "Could not remove network") }),
  });

  const disconnectMutation = useMutation({
    mutationFn: (containerId: string) => disconnectContainer(host, id, containerId, true),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Container disconnected",
        message: "The container was detached from the network",
      });
      queryClient.invalidateQueries({ queryKey: ["network-inspect", host, id] });
    },
    onError: (err) =>
      notify({ color: "red", title: "Disconnect failed", message: errorMessage(err, "Could not disconnect container") }),
  });

  const network = inspectQuery.data;
  const name = network?.name || shortId(id);
  const ipam = network?.ipam ?? [];
  const containers = network?.containers ?? [];
  const labelEntries = Object.entries(network?.labels ?? {});

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title={
          <Box direction="row" wrap align="center" gap={2}>
            <span>{name}</span>
            {network?.driver && (
              <Badge variant="light" size="sm">
                {network.driver}
              </Badge>
            )}
            {network?.scope && (
              <Badge variant="light" size="sm" color="magenta">
                {network.scope}
              </Badge>
            )}
            <Text color="muted" size="xs" mono>
              {shortId(id)}
            </Text>
          </Box>
        }
        icon={<Icon name="lan-outline-rounded" size="lg" />}
        actions={
          <Box direction="row" wrap align="center" gap={2}>
            <Link to="/networks" style={{ textDecoration: "none" }}>
              <Button variant="clear" color="neutral" iconLeft="arrow-back-rounded">
                Back
              </Button>
            </Link>
            {network && isRemovable(network) && (
              <Button
                variant="outline"
                color="red"
                iconLeft="delete-outline-rounded"
                onClick={() => setRemoveOpen(true)}
              >
                Remove
              </Button>
            )}
          </Box>
        }
      />

      {!hostResolved ? (
        <Callout
          color="warning"
          iconLeft={<Icon name="info-outline-rounded" />}
          title="Select a host"
        >
          Pick a specific host from the switcher (top-right), or open this network from the
          networks list — it can't be resolved while “All hosts” is selected.
        </Callout>
      ) : inspectQuery.isLoading ? (
        <Box direction="row" justify="center" style={{ padding: "48px 0" }}>
          <Spinner />
        </Box>
      ) : inspectQuery.isError ? (
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Failed to inspect network"
        >
          {errorMessage(inspectQuery.error, "Could not inspect this network on the selected host.")}
        </Callout>
      ) : (
        <Card variant="raised">
          <Card.Body>
            <Box direction="column" gap={6}>
              <Box direction="column" gap={2}>
                <Box direction="row" wrap gap={1}>
                  <Badge variant="light">{network?.driver || "-"}</Badge>
                  <Badge variant="light" color="neutral">
                    {network?.scope || "-"}
                  </Badge>
                  {network?.internal && (
                    <Badge variant="light" color="warning">
                      internal
                    </Badge>
                  )}
                  {network?.attachable && (
                    <Badge variant="light" color="teal">
                      attachable
                    </Badge>
                  )}
                </Box>
                <Box direction="row" justify="between" gap={1} wrap="nowrap">
                  <Text size="sm" color="muted">
                    ID
                  </Text>
                  <Code style={{ wordBreak: "break-all" }}>{network?.id || id}</Code>
                </Box>
              </Box>

              <Divider label="IPAM" />
              {ipam.length === 0 ? (
                <Text size="sm" color="muted">
                  No IPAM configuration.
                </Text>
              ) : (
                <Table bordered="all">
                  <Table.Header>
                    <Table.Row>
                      <Table.Head>Subnet</Table.Head>
                      <Table.Head>Gateway</Table.Head>
                      <Table.Head>IP range</Table.Head>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {ipam.map((cfg, i) => (
                      <Table.Row key={i}>
                        <Table.Cell>{cfg.subnet ? <Code>{cfg.subnet}</Code> : "-"}</Table.Cell>
                        <Table.Cell>{cfg.gateway ? <Code>{cfg.gateway}</Code> : "-"}</Table.Cell>
                        <Table.Cell>{cfg.ipRange ? <Code>{cfg.ipRange}</Code> : "-"}</Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table>
              )}

              <Divider label="Attached containers" />
              {containers.length === 0 ? (
                <Text size="sm" color="muted">
                  No containers attached.
                </Text>
              ) : (
                <Table bordered="all">
                  <Table.Header>
                    <Table.Row>
                      <Table.Head>Name</Table.Head>
                      <Table.Head>IPv4</Table.Head>
                      <Table.Head shrink />
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {containers.map((c) => (
                      <Table.Row key={c.id}>
                        <Table.Cell>
                          <Text size="sm">{c.name || shortId(c.id)}</Text>
                        </Table.Cell>
                        <Table.Cell>
                          {c.ipv4Address ? <Code>{c.ipv4Address}</Code> : <Text color="muted">-</Text>}
                        </Table.Cell>
                        <Table.Cell>
                          <Tooltip content="Disconnect">
                            <Button
                              variant="clear"
                              color="danger"
                              iconLeft="delete-outline-rounded"
                              aria-label={`Disconnect ${c.name || c.id}`}
                              loading={
                                disconnectMutation.isPending &&
                                disconnectMutation.variables === c.id
                              }
                              onClick={() => disconnectMutation.mutate(c.id)}
                            />
                          </Tooltip>
                        </Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table>
              )}

              {labelEntries.length > 0 && (
                <>
                  <Divider label="Labels" />
                  <Table>
                    <Table.Body>
                      {labelEntries.map(([key, value]) => (
                        <Table.Row key={key}>
                          <Table.Cell style={{ verticalAlign: "top" }}>
                            <Code>{key}</Code>
                          </Table.Cell>
                          <Table.Cell style={{ wordBreak: "break-all" }}>
                            <Text size="sm">{value}</Text>
                          </Table.Cell>
                        </Table.Row>
                      ))}
                    </Table.Body>
                  </Table>
                </>
              )}
            </Box>
          </Card.Body>
        </Card>
      )}

      <ConfirmModal
        opened={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title="Remove network"
        confirmLabel="Remove"
        loading={removeMutation.isPending}
        lockWhileLoading
        onConfirm={() => removeMutation.mutate()}
        message={
          <>
            Are you sure you want to remove network{" "}
            <Text as="span" weight="semibold">
              {name}
            </Text>
            ? This action cannot be undone.
          </>
        }
      />
    </Box>
  );
}
