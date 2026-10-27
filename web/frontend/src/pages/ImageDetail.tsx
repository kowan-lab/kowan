import { useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Checkbox,
  Code,
  Divider,
  Icon,
  Spinner,
  Table,
  Text,
} from "@/ui/kit";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { inspectImage, removeImage } from "../api/client";
import PageHeader from "../components/PageHeader";
import ConfirmModal from "../components/ConfirmModal";
import ImageVulnerabilities from "../components/security/ImageVulnerabilities";
import { notify } from "../ui/toast";
import { useHost, isAllHosts } from "../HostContext";
import { liveUnlessGone } from "../lib/live";
import { truncate, type ImageInspectJSON } from "../lib/images";
import { errorMessage, formatBytes, formatDate, shortId } from "../utils";

// Image detail: the full-page replacement for the old inspect drawer. Reached
// via /images/:id?host=<hostId> (the id is Docker's sha256:… digest).
export default function ImageDetail() {
  const { id = "" } = useParams();
  const [searchParams] = useSearchParams();
  const { hostId: ctxHost } = useHost();
  const host = searchParams.get("host") || ctxHost;
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [removeOpen, setRemoveOpen] = useState(false);
  const [forceRemove, setForceRemove] = useState(false);

  // Images are per-daemon; "All hosts" can't resolve one (only reachable via a
  // hand-typed URL without ?host= — list links always carry it).
  const hostResolved = !isAllHosts(host);

  const inspectQuery = useQuery({
    queryKey: ["image-inspect", host, id],
    queryFn: () => inspectImage(host, id),
    enabled: Boolean(id) && hostResolved,
    refetchInterval: liveUnlessGone,
  });

  const removeMutation = useMutation({
    mutationFn: () => removeImage(host, id, forceRemove),
    onSuccess: () => {
      notify({ color: "green", title: "Image removed", message: "The image was removed" });
      queryClient.invalidateQueries({
        predicate: (q) => String(q.queryKey[0]).startsWith("images"),
      });
      navigate("/images");
    },
    onError: (err) =>
      notify({ color: "red", title: "Remove failed", message: errorMessage(err, "Could not remove image") }),
  });

  const inspect = (inspectQuery.data?.inspect ?? {}) as ImageInspectJSON;
  const history = inspectQuery.data?.history ?? [];
  const tags = (inspect.RepoTags ?? []).filter((t) => t && t !== "<none>:<none>");
  const title = tags[0] ?? shortId(id);

  const config = inspect.Config;
  const env = config?.Env ?? [];
  const labelEntries = Object.entries(config?.Labels ?? {});
  const exposed = Object.keys(config?.ExposedPorts ?? {});
  const layerCount = inspect.RootFS?.Layers?.length ?? 0;
  const cmdString = (parts?: string[]) => (parts && parts.length > 0 ? parts.join(" ") : null);
  const entrypoint = cmdString(config?.Entrypoint);
  const cmd = cmdString(config?.Cmd);

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        breadcrumbs={[{ label: "Images", to: "/images" }]}
        title={
          <Box direction="row" wrap align="center" gap={2}>
            <span>{title}</span>
            {inspect.Architecture && <Badge variant="light" size="sm">{inspect.Architecture}</Badge>}
            {inspect.Os && <Badge variant="light" size="sm" color="magenta">{inspect.Os}</Badge>}
            <Text color="muted" size="xs" mono>
              {shortId(id)}
            </Text>
          </Box>
        }
        icon={<Icon name="image-outline-rounded" size="lg" />}
        actions={
          <Box direction="row" wrap align="center" gap={2}>
            <Link to="/images" style={{ textDecoration: "none" }}>
              <Button variant="clear" color="neutral" iconLeft="arrow-back-rounded">
                Back
              </Button>
            </Link>
            <Button
              variant="outline"
              color="red"
              iconLeft="delete-outline-rounded"
              onClick={() => {
                setForceRemove(false);
                setRemoveOpen(true);
              }}
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
          Pick a specific host from the switcher (top-right), or open this image from the
          images list — it can't be resolved while “All hosts” is selected.
        </Callout>
      ) : inspectQuery.isLoading ? (
        <Box direction="row" justify="center" style={{ padding: "48px 0" }}>
          <Spinner />
        </Box>
      ) : inspectQuery.isError ? (
        <Callout
          color="danger"
          iconLeft={<Icon name="error-outline-rounded" />}
          title="Failed to inspect image"
        >
          {errorMessage(inspectQuery.error, "Could not inspect this image on the selected host.")}
        </Callout>
      ) : (
        <Card variant="raised">
          <Card.Body>
            <Box direction="column" gap={6}>
              <Box direction="column" gap={1}>
                {tags.length > 0 && (
                  <Box direction="row" wrap gap={1} style={{ marginBottom: 4 }}>
                    {tags.map((tag) => (
                      <Badge key={tag} variant="light">
                        {tag}
                      </Badge>
                    ))}
                  </Box>
                )}
                <Box direction="row" justify="between" gap={1} wrap="nowrap">
                  <Text size="sm" color="muted">ID</Text>
                  <Code style={{ wordBreak: "break-all" }}>{inspect.Id ?? id ?? "—"}</Code>
                </Box>
                <Box direction="row" wrap gap={1}>
                  <Badge variant="light" color="gray">
                    {layerCount} layer{layerCount === 1 ? "" : "s"}
                  </Badge>
                </Box>
                <Box direction="row" justify="between" gap={1}>
                  <Text size="sm" color="muted">Size</Text>
                  <Text size="sm">
                    {typeof inspect.Size === "number" ? formatBytes(inspect.Size) : "—"}
                  </Text>
                </Box>
                <Box direction="row" justify="between" gap={1}>
                  <Text size="sm" color="muted">Created</Text>
                  <Text size="sm">{inspect.Created ? formatDate(inspect.Created) : "—"}</Text>
                </Box>
                {inspect.Author && (
                  <Box direction="row" justify="between" gap={1}>
                    <Text size="sm" color="muted">Author</Text>
                    <Text size="sm">{inspect.Author}</Text>
                  </Box>
                )}
              </Box>

              {(inspect.RepoDigests?.length ?? 0) > 0 && (
                <>
                  <Divider label="Repository digests" />
                  <Box direction="column" gap={1}>
                    {inspect.RepoDigests?.map((digest) => (
                      <Code key={digest} style={{ wordBreak: "break-all" }}>
                        {digest}
                      </Code>
                    ))}
                  </Box>
                </>
              )}

              {(config?.WorkingDir || config?.User || entrypoint || cmd || exposed.length > 0) && (
                <>
                  <Divider label="Configuration" />
                  <Box direction="column" gap={1}>
                    {config?.WorkingDir && (
                      <Box direction="row" justify="between" gap={1}>
                        <Text size="sm" color="muted">Working dir</Text>
                        <Code>{config.WorkingDir}</Code>
                      </Box>
                    )}
                    {config?.User && (
                      <Box direction="row" justify="between" gap={1}>
                        <Text size="sm" color="muted">User</Text>
                        <Code>{config.User}</Code>
                      </Box>
                    )}
                    {entrypoint && (
                      <Box direction="row" justify="between" gap={1} wrap="nowrap">
                        <Text size="sm" color="muted">Entrypoint</Text>
                        <Code style={{ wordBreak: "break-all" }}>{entrypoint}</Code>
                      </Box>
                    )}
                    {cmd && (
                      <Box direction="row" justify="between" gap={1} wrap="nowrap">
                        <Text size="sm" color="muted">Cmd</Text>
                        <Code style={{ wordBreak: "break-all" }}>{cmd}</Code>
                      </Box>
                    )}
                    {exposed.length > 0 && (
                      <Box direction="row" justify="between" gap={1} wrap="nowrap">
                        <Text size="sm" color="muted">Exposed ports</Text>
                        <Box direction="row" wrap gap={1} justify="end">
                          {exposed.map((port) => (
                            <Badge key={port} variant="light" color="cyan">
                              {port}
                            </Badge>
                          ))}
                        </Box>
                      </Box>
                    )}
                  </Box>
                </>
              )}

              {env.length > 0 && (
                <>
                  <Divider label="Environment" />
                  <Table>
                    <Table.Body>
                      {env.map((entry) => {
                        const eq = entry.indexOf("=");
                        const key = eq >= 0 ? entry.slice(0, eq) : entry;
                        const value = eq >= 0 ? entry.slice(eq + 1) : "";
                        return (
                          <Table.Row key={entry}>
                            <Table.Cell style={{ verticalAlign: "top" }}>
                              <Code>{key}</Code>
                            </Table.Cell>
                            <Table.Cell style={{ wordBreak: "break-all" }}>
                              <Text size="sm">{value}</Text>
                            </Table.Cell>
                          </Table.Row>
                        );
                      })}
                    </Table.Body>
                  </Table>
                </>
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

              <Divider label="History" />
              {history.length === 0 ? (
                <Text size="sm" color="muted">
                  No history available.
                </Text>
              ) : (
                <Box display="block" style={{ maxHeight: 420, overflow: "auto" }}>
                  <Table striped hoverable>
                    <Table.Header>
                      <Table.Row>
                        <Table.Head>Created</Table.Head>
                        <Table.Head>Created by</Table.Head>
                        <Table.Head align="right">Size</Table.Head>
                      </Table.Row>
                    </Table.Header>
                    <Table.Body>
                      {history.map((layer, i) => (
                        <Table.Row key={`${layer.created}-${i}`}>
                          <Table.Cell style={{ whiteSpace: "nowrap" }}>
                            <Text size="xs">{formatDate(layer.created)}</Text>
                          </Table.Cell>
                          <Table.Cell>
                            <Code style={{ wordBreak: "break-all" }}>
                              {truncate(layer.createdBy || "—")}
                            </Code>
                          </Table.Cell>
                          <Table.Cell align="right" style={{ whiteSpace: "nowrap" }}>
                            <Text size="xs">{formatBytes(layer.size)}</Text>
                          </Table.Cell>
                        </Table.Row>
                      ))}
                    </Table.Body>
                  </Table>
                </Box>
              )}
            </Box>
          </Card.Body>
        </Card>
      )}

      {hostResolved && inspectQuery.isSuccess && (
        <ImageVulnerabilities hostId={host} imageId={inspect.Id ?? id} />
      )}

      <ConfirmModal
        opened={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title="Remove image"
        confirmLabel="Remove"
        loading={removeMutation.isPending}
        lockWhileLoading
        onConfirm={() => removeMutation.mutate()}
        message={
          <>
            Are you sure you want to remove <Code>{title}</Code>? This action cannot be undone.
          </>
        }
      >
        <Checkbox
          label="Force remove (even if used by stopped containers)"
          checked={forceRemove}
          onChange={(e) => setForceRemove(e.target.checked)}
          disabled={removeMutation.isPending}
        />
      </ConfirmModal>
    </Box>
  );
}
