import { useEffect, useState } from "react";
import { Box, Button, InputToggle, Popup, Select, Text, TextInput } from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { connectionTypes, listConnections } from "../../api/client";
import type { GitOpsSync, GitOpsSyncInput } from "../../types";

const INTERVALS = [
  { value: "0", label: "Manual / webhook only" },
  { value: "60", label: "Every minute" },
  { value: "300", label: "Every 5 minutes" },
  { value: "900", label: "Every 15 minutes" },
  { value: "3600", label: "Every hour" },
];

export function emptySyncInput(): GitOpsSyncInput {
  return { name: "", repoUrl: "", ref: "main", path: "", credentialId: "", intervalSec: 300, autoApply: false, prune: true, enabled: true };
}

export function syncToInput(s: GitOpsSync): GitOpsSyncInput {
  const { name, repoUrl, ref, path, credentialId, intervalSec, autoApply, prune, enabled } = s;
  return { name, repoUrl, ref, path, credentialId, intervalSec, autoApply, prune, enabled };
}

/** Create / edit a sync: repository, branch, path, credential, cadence. */
export function SyncFormDialog({
  open,
  initial,
  title,
  submitting,
  error,
  onClose,
  onSubmit,
}: {
  open: boolean;
  initial: GitOpsSyncInput;
  title: string;
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (v: GitOpsSyncInput) => void;
}) {
  const [v, setV] = useState<GitOpsSyncInput>(initial);
  useEffect(() => {
    if (open) setV(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  const set = <K extends keyof GitOpsSyncInput>(k: K, val: GitOpsSyncInput[K]) => setV((p) => ({ ...p, [k]: val }));

  const conns = useQuery({ queryKey: ["connections"], queryFn: listConnections, retry: false, enabled: open });
  const types = useQuery({ queryKey: ["connection-types"], queryFn: connectionTypes, retry: false, enabled: open });
  const gitTypes = new Set((types.data ?? []).filter((t) => t.capabilities.includes("gitFetch")).map((t) => t.type));
  const credOptions = [
    { value: "", label: "None (public repository)" },
    ...(conns.data ?? [])
      .filter((c) => gitTypes.has(c.type) || c.id === v.credentialId)
      .map((c) => ({ value: c.id, label: `${c.name} · ${c.type}` })),
  ];
  const intervalOptions = INTERVALS.some((o) => Number(o.value) === v.intervalSec)
    ? INTERVALS
    : [...INTERVALS, { value: String(v.intervalSec), label: `Every ${v.intervalSec}s` }];

  const valid = v.name.trim() !== "" && v.repoUrl.trim() !== "";

  return (
    <Popup open={open} onOpenChange={(o) => { if (!o) onClose(); }} size="md">
      <Popup.Content>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) onSubmit({ ...v, name: v.name.trim(), repoUrl: v.repoUrl.trim(), ref: v.ref.trim() || "main", path: v.path.trim() });
          }}
        >
          <Popup.Header>
            <Popup.Title>{title}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            <TextInput label="Name" required data-autofocus value={v.name} onChange={(e) => set("name", e.target.value)} placeholder="infra" />
            <TextInput
              label="Repository URL"
              required
              value={v.repoUrl}
              onChange={(e) => set("repoUrl", e.target.value)}
              placeholder="https://github.com/acme/infra.git"
              description="HTTPS or SSH clone URL holding apiVersion: kowan/v1 resource files."
            />
            <Box direction="row" wrap grow align="start" gap={4}>
              <TextInput label="Branch" value={v.ref} onChange={(e) => set("ref", e.target.value)} placeholder="main" />
              <TextInput
                label="Path"
                value={v.path}
                onChange={(e) => set("path", e.target.value)}
                placeholder="kowan/"
                description="Folder scanned for *.yaml (empty = whole repository)."
              />
            </Box>
            <Select
              label="Credential"
              description="A stored git connection, for private repositories and for pushing exports."
              options={credOptions}
              value={v.credentialId}
              onChange={(val) => set("credentialId", val ?? "")}
              searchable
            />
            <Select
              label="Check for changes"
              options={intervalOptions}
              value={String(v.intervalSec)}
              onChange={(val) => set("intervalSec", Number(val ?? 0))}
            />
            <InputToggle
              label="Apply automatically"
              description="Apply a clean plan as soon as a new commit is detected (interval or webhook). Off: changes wait for your approval."
              checked={v.autoApply}
              onChange={(e) => set("autoApply", e.currentTarget.checked)}
            />
            <InputToggle
              label="Delete removed resources"
              description="Delete resources this sync created when their file disappears from git. Off: they are released and kept."
              checked={v.prune}
              onChange={(e) => set("prune", e.currentTarget.checked)}
            />
            <InputToggle
              label="Enabled"
              description="A disabled sync ignores its interval and webhook."
              checked={v.enabled}
              onChange={(e) => set("enabled", e.currentTarget.checked)}
            />
            {error && <Text size="sm" color="danger">{error}</Text>}
          </Popup.Body>
          <Popup.Footer>
            <Button variant="outline" onClick={onClose} disabled={submitting}>Cancel</Button>
            <Button type="submit" loading={submitting} disabled={!valid}>Save</Button>
          </Popup.Footer>
        </form>
      </Popup.Content>
    </Popup>
  );
}
