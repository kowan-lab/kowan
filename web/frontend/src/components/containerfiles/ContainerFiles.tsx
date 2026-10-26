import { Suspense, lazy, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Box,
  Breadcrumb,
  Button,
  Callout,
  Code,
  Icon,
  Popup,
  Skeleton,
  Table,
  Text,
  TextArea,
  TextInput,
  Tooltip,
  toast,
} from "@/ui/kit";
import { useAuth } from "../../auth/AuthContext";
import { saveBlob } from "../../api/client";
import { errorMessage, formatBytes, formatDate } from "../../utils";
import {
  deleteContainerPath,
  downloadContainerPath,
  listContainerDir,
  makeContainerDir,
  readContainerFile,
  renameContainerPath,
  uploadContainerFile,
  writeContainerFile,
  type ContainerFileEntry,
} from "./api";

// CodeMirror is heavy: only YAML files pull it in, on demand.
const YamlEditor = lazy(() => import("../YamlEditor"));

const YAML_RE = /\.(ya?ml)$/i;

function parentOf(path: string): string {
  const segs = path.split("/").filter(Boolean);
  return "/" + segs.slice(0, -1).join("/");
}

function entryIcon(e: ContainerFileEntry): string {
  if (e.isDir) return "folder-outline-rounded";
  if (e.isLink) return "link-rounded";
  return "draft-outline-rounded";
}

type Dialog =
  | { kind: "mkdir" }
  | { kind: "rename"; entry: ContainerFileEntry }
  | { kind: "delete"; entry: ContainerFileEntry }
  | null;

export interface ContainerFilesProps {
  hostId: string;
  containerId: string;
  active?: boolean;
}

/**
 * File browser for a container's filesystem (running or stopped), built on the
 * Docker archive API. Keyboard: ↑/↓ move, Enter opens, Backspace goes up,
 * F2 renames, Delete removes, "/" focuses the path box.
 */
export default function ContainerFiles({ hostId, containerId, active = true }: ContainerFilesProps) {
  const { user } = useAuth();
  const canWrite = user?.role === "admin" || user?.role === "operator";
  const queryClient = useQueryClient();
  const [dir, setDir] = useState("/");
  const [pathInput, setPathInput] = useState("/");
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<ContainerFileEntry | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [nameInput, setNameInput] = useState("");
  const [downloading, setDownloading] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const pathRef = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const listKey = ["container-files", hostId, containerId, dir];
  const listQuery = useQuery({
    queryKey: listKey,
    queryFn: () => listContainerDir(hostId, containerId, dir),
    enabled: active && Boolean(hostId && containerId),
    retry: false,
  });
  const listing = listQuery.data;
  const entries = useMemo(() => listing?.entries ?? [], [listing]);
  const running = listing?.running ?? false;
  // The server resolves symlinked directories; follow it so the breadcrumb is true.
  useEffect(() => {
    if (listing && listing.path !== dir) {
      setDir(listing.path);
    }
  }, [listing, dir]);
  useEffect(() => {
    setPathInput(dir);
    setCursor(0);
  }, [dir]);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["container-files", hostId, containerId] });

  const navigate = (path: string) => {
    setSelected(null);
    setDir(path || "/");
  };

  const open = (e: ContainerFileEntry) => {
    if (e.isDir) navigate(e.path);
    else if (e.isLink) navigate(e.path); // the server follows the link
    else setSelected(e);
  };

  const download = async (path: string, name: string, isDir: boolean) => {
    setDownloading(path);
    try {
      const blob = await downloadContainerPath(hostId, containerId, path);
      saveBlob(blob, isDir ? `${name || "rootfs"}.tar` : name);
    } catch (err) {
      toast.error("Download failed", { description: errorMessage(err, "Could not download.") });
    } finally {
      setDownloading(null);
    }
  };

  const upload = useMutation({
    mutationFn: ({ file, to }: { file: File; to: string }) => uploadContainerFile(hostId, containerId, to, file),
    onSuccess: (_d, { file, to }) => {
      toast.success("Uploaded", { description: `${file.name} → ${to}` });
      void refresh();
    },
    onError: (err) => toast.error("Upload failed", { description: errorMessage(err, "Could not upload.") }),
  });

  const act = useMutation({
    mutationFn: async (d: NonNullable<Dialog>) => {
      if (d.kind === "mkdir") return makeContainerDir(hostId, containerId, dir, nameInput.trim());
      if (d.kind === "rename") return renameContainerPath(hostId, containerId, d.entry.path, nameInput.trim());
      return deleteContainerPath(hostId, containerId, d.entry.path);
    },
    onSuccess: (_d, d) => {
      const verb = d.kind === "mkdir" ? "Folder created" : d.kind === "rename" ? "Renamed" : "Deleted";
      toast.success(verb);
      if (d.kind !== "mkdir" && selected && selected.path === d.entry.path) setSelected(null);
      setDialog(null);
      void refresh();
    },
    onError: (err) => toast.error("Action failed", { description: errorMessage(err, "The action failed.") }),
  });

  const openDialog = (d: NonNullable<Dialog>) => {
    setNameInput(d.kind === "rename" ? d.entry.name : "");
    setDialog(d);
  };

  const segments = dir.split("/").filter(Boolean);

  const onListKey = (ev: KeyboardEvent<HTMLDivElement>) => {
    if (ev.target !== ev.currentTarget) return;
    const n = entries.length;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      setCursor((c) => Math.min(n - 1, c + 1));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (ev.key === "Enter" && entries[cursor]) {
      ev.preventDefault();
      open(entries[cursor]);
    } else if (ev.key === "Backspace" && dir !== "/") {
      ev.preventDefault();
      navigate(parentOf(dir));
    } else if (ev.key === "F2" && canWrite && entries[cursor]) {
      ev.preventDefault();
      openDialog({ kind: "rename", entry: entries[cursor] });
    } else if (ev.key === "Delete" && canWrite && entries[cursor]) {
      ev.preventDefault();
      openDialog({ kind: "delete", entry: entries[cursor] });
    } else if (ev.key === "/") {
      ev.preventDefault();
      pathRef.current?.focus();
      pathRef.current?.select();
    }
  };

  useEffect(() => {
    const row = listRef.current?.querySelector<HTMLElement>(`[data-row="${cursor}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  return (
    <Box direction="column" gap={0} style={{ height: "100%", minHeight: 0 }}>
      {/* Toolbar */}
      <Box direction="row" wrap justify="between" align="center" gap={2} padding={2} style={{ borderBottom: "1px solid var(--border)" }}>
        <Box direction="row" align="center" gap={2} wrap style={{ minWidth: 0 }}>
          <Tooltip content="Up one level (Backspace)">
            <Button variant="clear" color="neutral" size="sm" aria-label="Up one level" iconLeft="arrow-upward-rounded"
              disabled={dir === "/"} onClick={() => navigate(parentOf(dir))} />
          </Tooltip>
          <Breadcrumb separator="/">
            <Breadcrumb.Item>
              <Text as="button" size="sm" color="info" aria-label="Root directory" onClick={() => navigate("/")}
                style={{ cursor: "pointer", background: "none", border: 0, padding: 0, display: "inline-flex" }}>
                <Icon name="folder-outline-rounded" size="sm" />
              </Text>
            </Breadcrumb.Item>
            {segments.map((seg, i) => {
              const p = "/" + segments.slice(0, i + 1).join("/");
              return (
                <Breadcrumb.Item key={p}>
                  <Text as="button" size="sm" color={i === segments.length - 1 ? "default" : "info"} onClick={() => navigate(p)}
                    style={{ cursor: "pointer", background: "none", border: 0, padding: 0 }}>
                    {seg}
                  </Text>
                </Breadcrumb.Item>
              );
            })}
          </Breadcrumb>
          {listing && (
            <Tooltip content={listing.method === "exec" ? "Listed with ls inside the running container" : "Listed from the archive API (works without a shell or while stopped)"}>
              <span><Badge size="sm" variant="outline" color="gray">{listing.method}</Badge></span>
            </Tooltip>
          )}
          {listing && !running && <Badge size="sm" variant="outline" color="gray">stopped · rename/delete off</Badge>}
        </Box>
        <Box direction="row" align="center" gap={1} wrap>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              navigate(pathInput.trim() || "/");
              listRef.current?.focus();
            }}
          >
            <TextInput size="sm" width={220} aria-label="Go to path" value={pathInput}
              onChange={(e) => setPathInput(e.currentTarget.value)}
              {...({ ref: pathRef } as Record<string, unknown>)} />
          </form>
          <Tooltip content="Refresh">
            <Button variant="clear" color="neutral" size="sm" aria-label="Refresh" iconLeft="refresh-rounded"
              loading={listQuery.isFetching} onClick={() => void refresh()} />
          </Tooltip>
          <Tooltip content="Download this folder as .tar">
            <Button variant="clear" color="neutral" size="sm" aria-label="Download folder" iconLeft="folder-zip-outline-rounded"
              loading={downloading === dir} onClick={() => void download(dir, segments[segments.length - 1] ?? "rootfs", true)} />
          </Tooltip>
          {canWrite && (
            <>
              <Button variant="outline" color="neutral" size="sm" iconLeft="add-rounded"
                onClick={() => openDialog({ kind: "mkdir" })}>
                New folder
              </Button>
              <input ref={fileInput} type="file" hidden onChange={(e) => {
                const f = e.currentTarget.files?.[0];
                if (f) upload.mutate({ file: f, to: dir });
                e.currentTarget.value = "";
              }} />
              <Button size="sm" iconLeft="upload-rounded" loading={upload.isPending} onClick={() => fileInput.current?.click()}>
                Upload
              </Button>
            </>
          )}
        </Box>
      </Box>

      <Box direction="row" align="stretch" wrap={false} style={{ flex: 1, minHeight: 0 }}>
        {/* Listing */}
        <div
          ref={listRef}
          tabIndex={0}
          role="grid"
          aria-label={`Files in ${dir}`}
          onKeyDown={onListKey}
          className="outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
          style={{ flex: selected ? "0 0 52%" : "1 1 100%", minWidth: 0, overflow: "auto", borderRight: selected ? "1px solid var(--border)" : undefined }}
        >
          {listQuery.isLoading ? (
            <Box direction="column" gap={2} padding={3}>
              {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} height={24} />)}
            </Box>
          ) : listQuery.isError ? (
            <Box padding={3}>
              <Callout color="danger">{errorMessage(listQuery.error, "Failed to list this folder.")}</Callout>
            </Box>
          ) : (
            <>
              {listing?.truncated && (
                <Box padding={2}>
                  <Callout color="warning">This folder is large: only the first {entries.length} entries are shown.</Callout>
                </Box>
              )}
              <Table bordered={false}>
                <Table.Header>
                  <Table.Row>
                    <Table.Head shrink />
                    <Table.Head>Name</Table.Head>
                    <Table.Head minWidth={80} align="right">Size</Table.Head>
                    <Table.Head minWidth={110}>Mode</Table.Head>
                    <Table.Head minWidth={130}>Modified</Table.Head>
                    <Table.Head shrink />
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {entries.map((e, i) => (
                    <Table.Row
                      key={e.path}
                      data-row={i}
                      selected={i === cursor || selected?.path === e.path}
                      onClick={() => {
                        setCursor(i);
                        open(e);
                      }}
                    >
                      <Table.Cell shrink>
                        <Icon name={entryIcon(e)} size="sm" color={e.isDir ? "blue" : undefined} />
                      </Table.Cell>
                      <Table.Cell>
                        <Text size="sm">{e.name}</Text>
                        {e.isLink && e.linkTarget && <Text size="xs" color="muted"> → {e.linkTarget}</Text>}
                      </Table.Cell>
                      <Table.Cell align="right">
                        <Text size="xs" color="muted" tabularNums>{e.kind === "file" ? formatBytes(e.size) : ""}</Text>
                      </Table.Cell>
                      <Table.Cell><Code>{e.mode || "—"}</Code></Table.Cell>
                      <Table.Cell><Text size="xs" color="muted">{e.modTime ? formatDate(e.modTime * 1000) : ""}</Text></Table.Cell>
                      <Table.Cell shrink>
                        <Box direction="row" gap={0} wrap={false} onClick={(ev) => ev.stopPropagation()}>
                          <Tooltip content={e.isDir ? "Download as .tar" : "Download"}>
                            <Button variant="clear" color="neutral" size="xs" aria-label={`Download ${e.name}`}
                              iconLeft={e.isDir ? "folder-zip-outline-rounded" : "download-rounded"}
                              loading={downloading === e.path} onClick={() => void download(e.path, e.name, e.isDir)} />
                          </Tooltip>
                          {canWrite && (
                            <>
                              <Tooltip content={running ? "Rename (F2)" : "Start the container to rename"}>
                                <span>
                                  <Button variant="clear" color="neutral" size="xs" aria-label={`Rename ${e.name}`} disabled={!running}
                                    iconLeft="edit-outline-rounded" onClick={() => openDialog({ kind: "rename", entry: e })} />
                                </span>
                              </Tooltip>
                              <Tooltip content={running ? "Delete (Del)" : "Start the container to delete"}>
                                <span>
                                  <Button variant="clear" color="danger" size="xs" aria-label={`Delete ${e.name}`} disabled={!running}
                                    iconLeft="delete-outline-rounded" onClick={() => openDialog({ kind: "delete", entry: e })} />
                                </span>
                              </Tooltip>
                            </>
                          )}
                        </Box>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                  {entries.length === 0 && (
                    <Table.Row>
                      <Table.Cell colSpan={6}><Text size="sm" color="muted">This folder is empty.</Text></Table.Cell>
                    </Table.Row>
                  )}
                </Table.Body>
              </Table>
            </>
          )}
        </div>

        {selected && (
          <FilePane
            key={selected.path}
            hostId={hostId}
            containerId={containerId}
            entry={selected}
            canWrite={canWrite}
            downloading={downloading === selected.path}
            onDownload={() => void download(selected.path, selected.name, false)}
            onClose={() => {
              setSelected(null);
              listRef.current?.focus();
            }}
          />
        )}
      </Box>

      <Popup open={dialog !== null} onOpenChange={(o) => !o && setDialog(null)} size="sm">
        <Popup.Content>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (dialog) act.mutate(dialog);
            }}
          >
            <Popup.Header>
              <Popup.Title>
                {dialog?.kind === "mkdir" ? "New folder" : dialog?.kind === "rename" ? "Rename" : "Delete"}
              </Popup.Title>
              <Popup.Close />
            </Popup.Header>
            <Popup.Body>
              {dialog?.kind === "delete" ? (
                <Text size="sm">
                  Permanently delete <Code>{dialog.entry.path}</Code>{dialog.entry.isDir ? " and everything inside it" : ""}? This cannot be undone.
                </Text>
              ) : (
                <TextInput data-autofocus label={dialog?.kind === "mkdir" ? `Name (in ${dir})` : "New name"} value={nameInput}
                  onChange={(e) => setNameInput(e.currentTarget.value)} required />
              )}
            </Popup.Body>
            <Popup.Footer>
              <Button variant="clear" color="neutral" onClick={() => setDialog(null)}>Cancel</Button>
              <Button type="submit" color={dialog?.kind === "delete" ? "danger" : undefined} loading={act.isPending}
                disabled={dialog?.kind !== "delete" && !nameInput.trim()}>
                {dialog?.kind === "mkdir" ? "Create" : dialog?.kind === "rename" ? "Rename" : "Delete"}
              </Button>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>
    </Box>
  );
}

function FilePane({
  hostId, containerId, entry, canWrite, downloading, onDownload, onClose,
}: {
  hostId: string;
  containerId: string;
  entry: ContainerFileEntry;
  canWrite: boolean;
  downloading: boolean;
  onDownload: () => void;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const q = useQuery({
    queryKey: ["container-file", hostId, containerId, entry.path],
    queryFn: () => readContainerFile(hostId, containerId, entry.path),
    enabled: canWrite,
    retry: false,
  });
  const [draft, setDraft] = useState<string | null>(null);
  const original = q.data?.content ?? "";
  const value = draft ?? original;
  const dirty = draft !== null && draft !== original;
  const save = useMutation({
    mutationFn: () => writeContainerFile(hostId, containerId, q.data?.path ?? entry.path, value),
    onSuccess: () => {
      toast.success("Saved", { description: q.data?.path ?? entry.path });
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ["container-file", hostId, containerId, entry.path] });
      void queryClient.invalidateQueries({ queryKey: ["container-files", hostId, containerId] });
    },
    onError: (err) => toast.error("Save failed", { description: errorMessage(err, "Could not save the file.") }),
  });
  const isYaml = YAML_RE.test(entry.name);

  return (
    <Box direction="column" gap={2} padding={3} style={{ flex: "1 1 48%", minWidth: 0, minHeight: 0 }}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && dirty && canWrite) {
          e.preventDefault();
          save.mutate();
        } else if (e.key === "Escape" && !dirty) {
          onClose();
        }
      }}>
      <Box direction="row" justify="between" align="center" wrap={false} gap={2}>
        <Box direction="row" gap={2} align="center" wrap={false} style={{ minWidth: 0 }}>
          <Icon name="draft-outline-rounded" size="sm" />
          <Text weight="semibold" size="sm" truncate>{entry.name}</Text>
          <Badge size="sm" variant="outline" color="gray">{formatBytes(q.data?.size ?? entry.size)}</Badge>
          {dirty && <Badge size="sm" variant="outline" color="warning">modified</Badge>}
        </Box>
        <Box direction="row" gap={1} align="center" wrap={false}>
          {dirty && (
            <>
              <Button size="sm" variant="clear" color="neutral" onClick={() => setDraft(null)}>Revert</Button>
              <Button size="sm" loading={save.isPending} onClick={() => save.mutate()}>Save</Button>
            </>
          )}
          <Tooltip content="Download">
            <Button variant="clear" color="neutral" size="sm" aria-label="Download file" iconLeft="download-rounded"
              loading={downloading} onClick={onDownload} />
          </Tooltip>
          <Tooltip content="Close (Esc)">
            <Button variant="clear" color="neutral" size="sm" aria-label="Close preview" iconLeft="close-rounded" onClick={onClose} />
          </Tooltip>
        </Box>
      </Box>
      {!canWrite ? (
        <Callout color="gray">Viewing file contents needs the operator role. You can still see the listing.</Callout>
      ) : q.isLoading ? (
        <Skeleton height={240} />
      ) : q.isError ? (
        <Callout color="danger">{errorMessage(q.error, "Failed to read this file.")}</Callout>
      ) : q.data?.tooLarge ? (
        <Callout color="gray">This file is {formatBytes(q.data.size)}, above the {formatBytes(q.data.limit)} editor limit. Download it instead.</Callout>
      ) : q.data?.binary ? (
        <Callout color="gray">This looks like a binary file. Download it instead.</Callout>
      ) : (
        <Box direction="column" style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
          {isYaml ? (
            <Suspense fallback={<Skeleton height={240} />}>
              <YamlEditor value={value} onChange={(v) => setDraft(v)} minHeight="320px" />
            </Suspense>
          ) : (
            <TextArea
              aria-label={`Contents of ${entry.name}`}
              value={value}
              readOnly={false}
              spellCheck={false}
              wrap="off"
              rows={24}
              onChange={(e) => setDraft(e.currentTarget.value)}
            />
          )}
          <Text size="xs" color="muted" style={{ marginTop: 6 }}>
            Saving keeps the file's owner and mode. Ctrl/⌘+S saves, Esc closes.
          </Text>
        </Box>
      )}
    </Box>
  );
}
