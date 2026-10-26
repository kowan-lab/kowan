import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Box, Breadcrumb, Button, Icon, Popup, Table, Text, type Column } from "@/ui/kit";
import { errorMessage, formatBytes, formatDate } from "../../utils";
import { browseSnapshot, type Snapshot, type SnapshotEntry } from "./api";

/** Read-only file list of a snapshot, one directory at a time. */
export default function SnapshotBrowser({ snapshot, hostId, onClose }: { snapshot: Snapshot | null; hostId: string; onClose: () => void }) {
  const root = snapshot?.paths?.[0] ?? "/";
  const [path, setPath] = useState(root);
  useEffect(() => { setPath(root); }, [root]);
  const q = useQuery({
    queryKey: ["volbackups", "ls", snapshot?.repoId, snapshot?.id, path, hostId],
    queryFn: () => browseSnapshot(snapshot!.repoId, snapshot!.id, path, hostId),
    enabled: !!snapshot,
    staleTime: 60_000,
  });
  const rel = path.startsWith(root) ? path.slice(root.length) : path;
  const crumbs = rel.split("/").filter(Boolean);
  const cols: Column<SnapshotEntry>[] = [
    {
      key: "name", header: "Name", grow: true,
      cell: (e) => (
        <Box direction="row" gap={2} align="center">
          <Icon name={e.isDir ? "folder-outline-rounded" : "draft-outline-rounded"} size="sm" />
          <Text size="sm">{e.name}</Text>
        </Box>
      ),
    },
    { key: "size", header: "Size", align: "right", cell: (e) => <Text size="sm" mono>{e.isDir ? "" : formatBytes(e.size)}</Text> },
    { key: "mode", header: "Mode", cell: (e) => <Text size="sm" mono color="muted">{e.mode}</Text> },
    { key: "mtime", header: "Modified", cell: (e) => <Text size="sm" color="muted">{formatDate(e.mtime)}</Text> },
  ];
  return (
    <Popup open={!!snapshot} onOpenChange={(o) => { if (!o) onClose(); }} size="lg">
      <Popup.Content>
        <Popup.Header><Popup.Title>Snapshot {snapshot?.shortId}</Popup.Title><Popup.Close /></Popup.Header>
        <Popup.Body>
          <Box direction="row" gap={2} align="center" wrap>
            <Button size="xs" variant="outline" color="neutral" disabled={path === root} iconLeft="arrow-upward-rounded"
              onClick={() => setPath(path.split("/").slice(0, -1).join("/") || "/")}>Up</Button>
            <Breadcrumb>
              <Breadcrumb.Item><Button variant="link" size="xs" onClick={() => setPath(root)}>{snapshot?.volume || root}</Button></Breadcrumb.Item>
              {crumbs.map((c, i) => (
                <Breadcrumb.Item key={i}><Button variant="link" size="xs" onClick={() => setPath(root + "/" + crumbs.slice(0, i + 1).join("/"))}>{c}</Button></Breadcrumb.Item>
              ))}
            </Breadcrumb>
          </Box>
          {q.isError ? <Text color="danger">{errorMessage(q.error, "Could not list the snapshot")}</Text> : (
            <Table<SnapshotEntry>
              data={q.data?.entries ?? []}
              columns={cols}
              rowKey={(e) => e.path}
              loading={q.isLoading}
              onRowClick={(e) => e.isDir && setPath(e.path)}
              hoverable
              emptyText="Empty directory"
            />
          )}
          {q.data?.truncated && <Text size="xs" color="muted">Showing the first 5000 entries.</Text>}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}
