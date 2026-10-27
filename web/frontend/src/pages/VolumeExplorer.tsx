import { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Box,
  Breadcrumb,
  Button,
  Callout,
  Card,
  Code,
  Icon,
  Select,
  Skeleton,
  Table,
  Text,
  Tooltip,
} from "@/ui/kit";
import { notify } from "../ui/toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import {
  browseVolume,
  downloadVolumeArchive,
  downloadVolumeFile,
  listHosts,
  listVolumes,
  readVolumeFile,
  saveBlob,
  uploadVolumeFile,
} from "../api/client";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import { useHost, isAllHosts } from "../HostContext";
import type { Host, Volume, VolumeFile } from "../types";
import { errorMessage, formatBytes } from "../utils";


// Preview only reasonably small text files inline; larger ones are download-only.
const MAX_PREVIEW_BYTES = 256 * 1024;

/** Heuristic: a NUL byte means the payload is binary and should not be shown. */
function looksBinary(s: string): boolean {
  return s.includes("\u0000");
}

export default function VolumeExplorer() {
  const { hostId: globalHost } = useHost();
  const [params, setParams] = useSearchParams();

  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });

  // Selected host: query param wins, else the global host (unless "all"), else
  // the first online host once the host list loads.
  const paramHost = params.get("host") ?? "";
  const [host, setHost] = useState<string>(
    paramHost || (isAllHosts(globalHost) ? "" : globalHost),
  );
  useEffect(() => {
    if (!host && hostsQuery.data && hostsQuery.data.length > 0) {
      const first = hostsQuery.data.find((h) => h.online) ?? hostsQuery.data[0];
      setHost(first.id);
    }
  }, [host, hostsQuery.data]);

  const volumesQuery = useQuery({
    queryKey: ["volumes", host],
    queryFn: () => listVolumes(host),
    enabled: host !== "",
  });

  const paramName = params.get("name") ?? "";
  const [volume, setVolume] = useState<string>(paramName);
  // When the host changes, drop a volume selection that no longer belongs to it.
  useEffect(() => {
    if (!volume) return;
    const vols = volumesQuery.data;
    if (vols && !vols.some((v) => v.name === volume)) setVolume("");
  }, [volume, volumesQuery.data]);

  const setSelection = (nextHost: string, nextVolume: string) => {
    setHost(nextHost);
    setVolume(nextVolume);
    const next = new URLSearchParams(params);
    if (nextHost) next.set("host", nextHost);
    else next.delete("host");
    if (nextVolume) next.set("name", nextVolume);
    else next.delete("name");
    setParams(next, { replace: true });
  };

  const hostOptions = useMemo(
    () =>
      (hostsQuery.data ?? []).map((h: Host) => ({
        value: h.id,
        label: h.name || h.id,
      })),
    [hostsQuery.data],
  );
  const volumeOptions = useMemo(
    () => (volumesQuery.data ?? []).map((v: Volume) => ({ value: v.name, label: v.name })),
    [volumesQuery.data],
  );

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Volume Explorer"
        description="Browse a volume's filesystem, preview files, upload new ones, and download files or whole folders as a zip."
        icon={<Icon name="folder-open-outline-rounded" size="lg" />}
        actions={
          <Box direction="row" wrap gap={3}>
            <Select
              placeholder="Host"
              options={hostOptions}
              value={host || undefined}
              onChange={(v) => setSelection((v as string) ?? "", "")}
              searchable
              width={200}
            />
            <Select
              placeholder={host ? "Volume" : "Pick a host first"}
              options={volumeOptions}
              value={volume || undefined}
              onChange={(v) => setSelection(host, (v as string) ?? "")}
              searchable
              disabled={host === "" || volumesQuery.isLoading}
              width={240}
            />
          </Box>
        }
      />

      {host === "" || volume === "" ? (
        <Card variant="raised">
          <Card.Body>
            <EmptyState
              icon={<Icon name="folder-open-outline-rounded" size="xl" />}
              title="Pick a host and volume"
              description="Choose a host and one of its volumes above to start exploring its files."
            />
          </Card.Body>
        </Card>
      ) : (
        <ExplorerBody key={`${host}/${volume}`} host={host} volume={volume} />
      )}
    </Box>
  );
}

interface ExplorerBodyProps {
  host: string;
  volume: string;
}

function ExplorerBody({ host, volume }: ExplorerBodyProps) {
  const queryClient = useQueryClient();
  const [currentPath, setCurrentPath] = useState("/");
  const [selectedFile, setSelectedFile] = useState<VolumeFile | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const browseQuery = useQuery({
    queryKey: ["volexp-browse", host, volume, currentPath],
    queryFn: () => browseVolume(host, volume, currentPath),
  });

  const previewQuery = useQuery({
    queryKey: ["volexp-file", host, volume, selectedFile?.path],
    queryFn: () => readVolumeFile(host, volume, selectedFile!.path),
    enabled: selectedFile !== null && selectedFile.size <= MAX_PREVIEW_BYTES,
  });

  const refreshBrowse = () =>
    queryClient.invalidateQueries({
      queryKey: ["volexp-browse", host, volume, currentPath],
    });

  const uploadMutation = useMutation({
    // Carry the target dir in the variables: the user may navigate elsewhere
    // while the upload is in flight.
    mutationFn: ({ file, dir }: { file: File; dir: string }) => uploadVolumeFile(host, volume, dir, file),
    onSuccess: (_d, { file, dir }) => {
      notify({
        color: "green",
        title: "Uploaded",
        message: `"${file.name}" was written to ${dir}.`,
      });
      void queryClient.invalidateQueries({ queryKey: ["volexp-browse", host, volume, dir] });
      // An upload can overwrite the file currently shown in the preview pane.
      void queryClient.invalidateQueries({ queryKey: ["volexp-file", host, volume] });
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Upload failed",
        message: errorMessage(err, "Could not upload the file."),
      });
    },
  });

  const [downloading, setDownloading] = useState<string | null>(null);
  const download = async (kind: "file" | "zip", entry: VolumeFile | "current") => {
    const path = entry === "current" ? currentPath : entry.path;
    const baseName = entry === "current" ? volume : entry.name;
    const key = `${kind}:${path}`;
    setDownloading(key);
    try {
      if (kind === "file") {
        const blob = await downloadVolumeFile(host, volume, path);
        saveBlob(blob, baseName);
      } else {
        const blob = await downloadVolumeArchive(host, volume, path);
        saveBlob(blob, `${baseName}.zip`);
      }
    } catch (err) {
      notify({
        color: "red",
        title: "Download failed",
        message: errorMessage(err, "Could not download."),
      });
    } finally {
      setDownloading(null);
    }
  };

  const segments = currentPath.split("/").filter((s) => s.length > 0);
  const navigateTo = (path: string) => {
    setSelectedFile(null);
    setCurrentPath(path);
  };
  const goUp = () => {
    const parent = "/" + segments.slice(0, -1).join("/");
    navigateTo(parent === "/" ? "/" : parent);
  };

  const entries = browseQuery.data?.entries ?? [];

  return (
    <Card variant="raised" style={{ overflow: "hidden" }}>
      {/* Toolbar */}
      <Box
        direction="row"
        wrap
        justify="between"
        align="center"
        padding={3}
        style={{ borderBottom: "1px solid var(--border)" }}
      >
        <Breadcrumb separator="/">
          <Breadcrumb.Item>
            <Text as="a" size="sm" color="info" onClick={() => navigateTo("/")} style={{ cursor: "pointer" }}>
              <Box direction="row" align="center" gap={1} wrap={false}>
                <Icon name="database-outline" size="sm" />
                {volume}
              </Box>
            </Text>
          </Breadcrumb.Item>
          {segments.map((seg, i) => {
            const path = "/" + segments.slice(0, i + 1).join("/");
            return (
              <Breadcrumb.Item key={path}>
                <Text as="a" size="sm" color="info" onClick={() => navigateTo(path)} style={{ cursor: "pointer" }}>
                  {seg}
                </Text>
              </Breadcrumb.Item>
            );
          })}
        </Breadcrumb>

        <Box direction="row" wrap gap={2} align="center">
          <Tooltip content="Refresh">
            <Button
              variant="clear"
              color="neutral"
              aria-label="Refresh"
              iconLeft="refresh-rounded"
              loading={browseQuery.isFetching}
              onClick={() => void refreshBrowse()}
            />
          </Tooltip>
          <Tooltip content="Download this folder as .zip">
            <Button
              variant="clear"
              color="neutral"
              aria-label="Download folder as zip"
              iconLeft="folder-zip-outline-rounded"
              loading={downloading === `zip:${currentPath}`}
              onClick={() => void download("zip", "current")}
            />
          </Tooltip>
          <input
            ref={fileInputRef}
            type="file"
            hidden
            onChange={(e) => {
              const f = e.currentTarget.files?.[0];
              if (f) uploadMutation.mutate({ file: f, dir: currentPath });
              e.currentTarget.value = "";
            }}
          />
          <Button
            size="sm"
            color="info"
            iconLeft="upload-rounded"
            loading={uploadMutation.isPending}
            onClick={() => fileInputRef.current?.click()}
          >
            Upload here
          </Button>
        </Box>
      </Box>

      <Box
        direction="row"
        align="stretch"
        gap={0}
        wrap={false}
        style={{ minHeight: "calc(100vh - 280px)" }}
      >
        {/* File list */}
        <Box
          style={{
            flex: selectedFile ? "0 0 55%" : "1 1 100%",
            borderRight: selectedFile ? "1px solid var(--border)" : undefined,
            minWidth: 0,
          }}
        >
          <Box style={{ height: "calc(100vh - 280px)", overflow: "auto" }}>
            {browseQuery.isLoading ? (
              <Box direction="column" gap={2} padding={4}>
                {Array.from({ length: 8 }).map((_, i) => (
                  <Skeleton key={i} height={28} />
                ))}
              </Box>
            ) : browseQuery.isError ? (
              <Box padding={4}>
                <Callout color="danger" iconLeft={<Icon name="warning-outline-rounded" size="sm" />}>
                  {errorMessage(browseQuery.error, "Failed to browse this volume.")}
                </Callout>
              </Box>
            ) : (
              <>
                {browseQuery.data?.truncated && (
                  <Box padding={2}>
                    <Callout color="warning">This folder is large: only the first {entries.length} entries are shown.</Callout>
                  </Box>
                )}
                <Table hoverable stickyHeader>
                <Table.Header>
                  <Table.Row>
                    <Table.Head minWidth={32} />
                    <Table.Head>Name</Table.Head>
                    <Table.Head minWidth={110}>Size</Table.Head>
                    <Table.Head minWidth={130}>Mode</Table.Head>
                    <Table.Head minWidth={96} />
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {segments.length > 0 && (
                    <Table.Row style={{ cursor: "pointer" }} onClick={goUp}>
                      <Table.Cell>
                        <Icon name="arrow-upward-rounded" size="sm" />
                      </Table.Cell>
                      <Table.Cell>
                        <Text size="sm">..</Text>
                      </Table.Cell>
                      <Table.Cell />
                      <Table.Cell />
                      <Table.Cell />
                    </Table.Row>
                  )}
                  {entries.map((entry) => {
                    const isSelected = selectedFile?.path === entry.path;
                    return (
                      <Table.Row
                        key={entry.path}
                        selected={isSelected}
                        style={{ cursor: "pointer" }}
                        onClick={() =>
                          entry.isDir ? navigateTo(entry.path) : setSelectedFile(entry)
                        }
                      >
                        <Table.Cell>
                          {entry.isDir ? (
                            <Icon name="folder-outline-rounded" size="sm" color="blue" />
                          ) : (
                            <Icon name="draft-outline-rounded" size="sm" />
                          )}
                        </Table.Cell>
                        <Table.Cell>
                          <Text size="sm">{entry.name}</Text>
                        </Table.Cell>
                        <Table.Cell>
                          <Text size="xs" color="muted">
                            {entry.isDir ? "" : formatBytes(entry.size)}
                          </Text>
                        </Table.Cell>
                        <Table.Cell>
                          <Code>{entry.mode}</Code>
                        </Table.Cell>
                        <Table.Cell>
                          <Box
                            direction="row"
                            gap={1}
                            justify="end"
                            wrap={false}
                            align="center"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {entry.isDir ? (
                              <Tooltip content="Download as .zip">
                                <Button
                                  variant="clear"
                                  color="neutral"
                                  aria-label={`Download ${entry.name} as zip`}
                                  iconLeft="folder-zip-outline-rounded"
                                  loading={downloading === `zip:${entry.path}`}
                                  onClick={() => void download("zip", entry)}
                                />
                              </Tooltip>
                            ) : (
                              <Tooltip content="Download file">
                                <Button
                                  variant="clear"
                                  color="neutral"
                                  aria-label={`Download ${entry.name}`}
                                  iconLeft="download-rounded"
                                  loading={downloading === `file:${entry.path}`}
                                  onClick={() => void download("file", entry)}
                                />
                              </Tooltip>
                            )}
                          </Box>
                        </Table.Cell>
                      </Table.Row>
                    );
                  })}
                  {entries.length === 0 && (
                    <Table.Row>
                      <Table.Cell colSpan={5}>
                        <Text size="sm" color="muted" style={{ padding: "12px" }}>
                          This folder is empty.
                        </Text>
                      </Table.Cell>
                    </Table.Row>
                  )}
                </Table.Body>
                </Table>
              </>
            )}
          </Box>
        </Box>

        {/* Preview pane */}
        {selectedFile && (
          <Box style={{ flex: "1 1 45%", minWidth: 0 }}>
            <Box direction="column" gap={2} padding={4} style={{ height: "calc(100vh - 280px)" }}>
              <Box direction="row" justify="between" wrap={false} align="center">
                <Box direction="row" gap={2} wrap={false} align="center" style={{ minWidth: 0 }}>
                  <Icon name="draft-outline-rounded" size="sm" />
                  <Text weight="semibold" size="sm" truncate>
                    {selectedFile.name}
                  </Text>
                  <Badge variant="light" color="gray" size="sm">
                    {formatBytes(selectedFile.size)}
                  </Badge>
                </Box>
                <Box direction="row" gap={2} wrap={false} align="center">
                  <Tooltip content="Download file">
                    <Button
                      variant="clear"
                      color="neutral"
                      aria-label="Download file"
                      iconLeft="download-rounded"
                      loading={downloading === `file:${selectedFile.path}`}
                      onClick={() => void download("file", selectedFile)}
                    />
                  </Tooltip>
                  <Button
                    size="sm"
                    variant="clear"
                    onClick={() => setSelectedFile(null)}
                  >
                    Close
                  </Button>
                </Box>
              </Box>

              {selectedFile.size > MAX_PREVIEW_BYTES ? (
                <Callout color="gray" iconLeft={<Icon name="draft-outline-rounded" size="sm" />}>
                  This file is {formatBytes(selectedFile.size)} — too large to preview.
                  Use the download button to save it.
                </Callout>
              ) : previewQuery.isLoading ? (
                <Skeleton height={240} />
              ) : previewQuery.isError ? (
                <Callout color="danger" iconLeft={<Icon name="warning-outline-rounded" size="sm" />}>
                  {errorMessage(previewQuery.error, "Failed to read this file.")}
                </Callout>
              ) : looksBinary(previewQuery.data ?? "") ? (
                <Callout color="gray" iconLeft={<Icon name="draft-outline-rounded" size="sm" />}>
                  This looks like a binary file. Use the download button to save it.
                </Callout>
              ) : (
                <Box style={{ height: "100%", overflow: "auto" }}>
                  <Code
                    block
                    style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}
                  >
                    {previewQuery.data ?? ""}
                  </Code>
                </Box>
              )}
            </Box>
          </Box>
        )}
      </Box>
    </Card>
  );
}
