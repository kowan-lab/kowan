import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Code,
  Icon,
  Spinner,
  Table,
  Text,
} from "@/ui/kit";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { inspectVolume, removeVolume } from "../api/client";
import PageHeader from "../components/PageHeader";
import ConfirmModal from "../components/ConfirmModal";
import VolumeBackupSection from "../components/volbackup/VolumeBackupSection";
import { notify } from "../ui/toast";
import { useHost, isAllHosts } from "../HostContext";
import { liveUnlessGone } from "../lib/live";
import { errorMessage, formatDate } from "../utils";

// ===========================================================================
// Volume detail: the full-page replacement for the old inspect drawer. Reached
// via /volumes/:name?host=<hostId>; a volume's identifier is its name, and the
// host is carried in the query string so aggregated ("All hosts") rows link to
// the right daemon. Read-only file browsing lives on the dedicated volume
// explorer, reachable from the "Browse files" action here.
// ===========================================================================

export default function VolumeDetail() {
  const { name = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { hostId: ctxHost } = useHost();
  // The host comes from the link (?host=); fall back to the globally selected
  // host so a hand-typed URL still resolves against something sensible.
  const host = searchParams.get("host") || ctxHost;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [removeOpen, setRemoveOpen] = useState(false);

  // Volumes are per-daemon; "All hosts" can't resolve one (only reachable via a
  // hand-typed URL without ?host= — list links always carry it).
  const hostResolved = !isAllHosts(host);

  // Same key the drawer used, so the inspect cache is shared with the list's
  // hover-prefetch and the two views stay in sync.
  const inspectQuery = useQuery({
    queryKey: ["vol-inspect", host, name],
    queryFn: () => inspectVolume(host, name),
    enabled: Boolean(name) && hostResolved,
    refetchInterval: liveUnlessGone,
  });

  const removeMutation = useMutation({
    mutationFn: () => removeVolume(host, name),
    onSuccess: () => {
      notify({ color: "green", title: "Volume removed", message: `Volume "${name}" was removed.` });
      queryClient.invalidateQueries({
        predicate: (q) => String(q.queryKey[0]).startsWith("volumes"),
      });
      navigate("/volumes");
    },
    onError: (err) =>
      notify({
        color: "red",
        title: "Failed to remove volume",
        message: errorMessage(err, "Could not remove the volume."),
      }),
  });

  // The inspect JSON comes straight from the Engine, which uses capitalised
  // keys; tolerate both shapes (the drawer did the same).
  const info = inspectQuery.data;
  const driver = info?.["driver"] ?? info?.["Driver"];
  // A typed label for the header badge (the raw inspect values are `unknown`).
  const driverText = driver == null ? "" : String(driver);
  const mountpoint = info?.["mountpoint"] ?? info?.["Mountpoint"];
  const scope = info?.["scope"] ?? info?.["Scope"];
  const createdAt = info?.["createdAt"] ?? info?.["CreatedAt"];
  const labels = (info?.["labels"] ?? info?.["Labels"]) as
    | Record<string, string>
    | undefined
    | null;
  const labelEntries = labels ? Object.entries(labels) : [];

  // Deep-link into the dedicated file explorer for this volume.
  const explorerPath = `/volume-explorer?${new URLSearchParams({
    host,
    name,
  }).toString()}`;

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        breadcrumbs={[{ label: "Volumes", to: "/volumes" }]}
        title={
          <Box direction="row" wrap align="center" gap={2}>
            <span>{name}</span>
            {driverText && (
              <Badge variant="light" color="blue" size="sm">
                {driverText}
              </Badge>
            )}
          </Box>
        }
        icon={<Icon name="database-outline" size="lg" />}
        actions={
          <Box direction="row" wrap align="center" gap={2}>
            <Link to="/volumes" style={{ textDecoration: "none" }}>
              <Button variant="clear" color="neutral" iconLeft="arrow-back-rounded">
                Back
              </Button>
            </Link>
            <Link to={explorerPath} style={{ textDecoration: "none" }}>
              <Button
                variant="outline"
                color="neutral"
                iconLeft="folder-open-outline-rounded"
              >
                Browse files
              </Button>
            </Link>
            <Button
              variant="outline"
              color="red"
              iconLeft="delete-outline-rounded"
              onClick={() => setRemoveOpen(true)}
            >
              Remove
            </Button>
          </Box>
        }
      />

      {!hostResolved ? (
        <Callout
          color="warning"
          iconLeft={<Icon name="info-outline-rounded" />}
          title="Select a host"
        >
          Pick a specific host from the switcher (top-right), or open this volume from the
          volumes list — it can't be resolved while “All hosts” is selected.
        </Callout>
      ) : inspectQuery.isLoading ? (
        <Box direction="row" justify="center" style={{ padding: "48px 0" }}>
          <Spinner />
        </Box>
      ) : inspectQuery.isError ? (
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Failed to inspect volume"
        >
          {errorMessage(inspectQuery.error, "Could not inspect this volume on the selected host.")}
        </Callout>
      ) : (
        <Card variant="raised">
          <Card.Body>
            <Box direction="column" gap={2}>
              <Text weight="semibold" size="sm" style={{ marginBottom: 8 }}>
                Details
              </Text>
              <Table bordered={false}>
                <Table.Body>
                  <Table.Row>
                    <Table.Cell style={{ width: 120 }}>
                      <Text size="sm" color="muted">
                        Driver
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Badge variant="light" color="blue">
                        {String(driver ?? "local")}
                      </Badge>
                    </Table.Cell>
                  </Table.Row>
                  <Table.Row>
                    <Table.Cell>
                      <Text size="sm" color="muted">
                        Scope
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="sm">{String(scope ?? "-")}</Text>
                    </Table.Cell>
                  </Table.Row>
                  <Table.Row>
                    <Table.Cell>
                      <Text size="sm" color="muted">
                        Created
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="sm">
                        {createdAt ? formatDate(String(createdAt)) : "-"}
                      </Text>
                    </Table.Cell>
                  </Table.Row>
                  <Table.Row>
                    <Table.Cell>
                      <Text size="sm" color="muted">
                        Mountpoint
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      {mountpoint ? (
                        <Code>{String(mountpoint)}</Code>
                      ) : (
                        <Text size="sm" color="muted">
                          -
                        </Text>
                      )}
                    </Table.Cell>
                  </Table.Row>
                  {labelEntries.length > 0 && (
                    <Table.Row>
                      <Table.Cell>
                        <Text size="sm" color="muted">
                          Labels
                        </Text>
                      </Table.Cell>
                      <Table.Cell>
                        <Box direction="row" wrap gap={1}>
                          {labelEntries.map(([k, v]) => (
                            <Badge key={k} variant="light" color="gray">
                              {k}={String(v)}
                            </Badge>
                          ))}
                        </Box>
                      </Table.Cell>
                    </Table.Row>
                  )}
                </Table.Body>
              </Table>
            </Box>
          </Card.Body>
        </Card>
      )}

      {hostResolved && name && <VolumeBackupSection hostId={host} volume={name} />}

      <ConfirmModal
        opened={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title="Remove volume"
        confirmLabel="Remove"
        loading={removeMutation.isPending}
        lockWhileLoading
        onConfirm={() => removeMutation.mutate()}
        message={
          <>
            Are you sure you want to remove the volume{" "}
            <Text as="span" weight="semibold">
              {name}
            </Text>
            ? This will force-remove it and cannot be undone.
          </>
        }
      />
    </Box>
  );
}
