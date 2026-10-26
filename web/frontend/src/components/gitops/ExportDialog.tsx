import { useEffect, useMemo, useState } from "react";
import { Box, Button, Callout, Checkbox, Popup, Select, Spinner, Tabs, Text, TextInput } from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import {
  exportGitOpsPreview,
  exportGitOpsPush,
  exportGitOpsZip,
  listGitOpsResources,
  saveBlob,
} from "../../api/client";
import type { GitOpsExportFile, GitOpsSync } from "../../types";
import { errorMessage } from "../../utils";
import { notify } from "../../ui/toast";

/**
 * Export current state as kowan/v1 YAML: pick resources, preview the files,
 * download a zip or commit them to a branch of a sync's repository.
 */
export function ExportDialog({ open, onClose, syncs }: { open: boolean; onClose: () => void; syncs: GitOpsSync[] }) {
  const res = useQuery({ queryKey: ["gitops-resources"], queryFn: listGitOpsResources, enabled: open });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState("download");
  const [preview, setPreview] = useState<{ files: GitOpsExportFile[]; warnings: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [syncId, setSyncId] = useState("");
  const [branch, setBranch] = useState("kowan/export");
  const [message, setMessage] = useState("");
  const [result, setResult] = useState<string | null>(null);

  const items = useMemo(() => res.data ?? [], [res.data]);
  useEffect(() => {
    if (open) {
      setPicked(new Set(items.map((i) => `${i.kind}/${i.id}`)));
      setPreview(null);
      setResult(null);
      setSyncId((s) => s || syncs[0]?.id || "");
    }
  }, [open, items, syncs]);

  const groups = useMemo(() => {
    const m = new Map<string, typeof items>();
    for (const i of items) m.set(i.kind, [...(m.get(i.kind) ?? []), i]);
    return [...m.entries()];
  }, [items]);
  const selection = items.filter((i) => picked.has(`${i.kind}/${i.id}`)).map((i) => ({ kind: i.kind, id: i.id }));
  const toggle = (key: string, on: boolean) =>
    setPicked((p) => {
      const n = new Set(p);
      if (on) n.add(key);
      else n.delete(key);
      return n;
    });

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      notify({ color: "red", title: "Export failed", message: errorMessage(err, "Export failed") });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Popup open={open} onOpenChange={(o) => { if (!o) onClose(); }} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Export resources</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <Text size="sm" color="muted">
            Renders the selected resources as <span className="font-mono">apiVersion: kowan/v1</span> files, one per resource. Secrets are never
            exported: channels keep their <span className="font-mono">[[secret]]</span> references, registry passwords and trigger tokens stay in Kowan.
          </Text>
          {res.isLoading ? (
            <Spinner />
          ) : (
            <div className="max-h-96 overflow-y-auto border">
              {groups.map(([kind, list]) => {
                const keys = list.map((i) => `${kind}/${i.id}`);
                const all = keys.every((k) => picked.has(k));
                return (
                  <div key={kind} className="border-b last:border-b-0">
                    <div className="flex items-center gap-3 bg-muted/50 px-5 py-2">
                      <Checkbox
                        label={<span className="font-mono text-[12px] tracking-[0.06em] uppercase">{kind} · {list.length}</span>}
                        checked={all}
                        onChange={(e) => keys.forEach((k) => toggle(k, e.currentTarget.checked))}
                      />
                    </div>
                    <div className="grid grid-cols-1 gap-x-6 px-5 py-2 sm:grid-cols-2">
                      {list.map((i) => (
                        <Checkbox
                          key={i.id}
                          label={
                            <span className="text-[13px]">
                              {i.name}
                              {i.managedBy && <span className="ml-2 text-[12px] text-muted-foreground">(managed)</span>}
                            </span>
                          }
                          checked={picked.has(`${kind}/${i.id}`)}
                          onChange={(e) => toggle(`${kind}/${i.id}`, e.currentTarget.checked)}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
              {groups.length === 0 && <Text size="sm" color="muted" className="block p-5">Nothing to export yet.</Text>}
            </div>
          )}

          <Tabs variant="segmented" value={tab} onValueChange={setTab}>
            <Tabs.List>
              <Tabs.Trigger value="download">Download</Tabs.Trigger>
              <Tabs.Trigger value="push">Commit to a branch</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Panel value="download">
              <Box direction="row" gap={2} wrap style={{ marginTop: 8 }}>
                <Button
                  disabled={selection.length === 0}
                  loading={busy}
                  onClick={() => run(async () => saveBlob(await exportGitOpsZip(selection), "kowan-export.zip"))}
                >
                  Download zip
                </Button>
                <Button
                  variant="outline"
                  disabled={selection.length === 0 || busy}
                  onClick={() => run(async () => setPreview(await exportGitOpsPreview(selection)))}
                >
                  Preview files
                </Button>
              </Box>
            </Tabs.Panel>
            <Tabs.Panel value="push">
              {syncs.length === 0 ? (
                <Text size="sm" color="muted">Create a sync first: exports are pushed to its repository with its credential.</Text>
              ) : (
                <Box direction="column" gap={3} style={{ marginTop: 8 }}>
                  <Select
                    label="Repository (sync)"
                    options={syncs.map((s) => ({ value: s.id, label: `${s.name} · ${s.repoUrl}` }))}
                    value={syncId}
                    onChange={(v) => setSyncId(v ?? "")}
                  />
                  <Box direction="row" wrap grow gap={4} align="start">
                    <TextInput label="Branch" value={branch} onChange={(e) => setBranch(e.target.value)} description="Created from the sync's branch when missing." />
                    <TextInput label="Commit message" value={message} onChange={(e) => setMessage(e.target.value)} placeholder="kowan: export resources" />
                  </Box>
                  <Box direction="row">
                    <Button
                      disabled={selection.length === 0 || !syncId || !branch.trim()}
                      loading={busy}
                      onClick={() =>
                        run(async () => {
                          const r = await exportGitOpsPush(selection, { syncId, branch: branch.trim(), message });
                          setResult(r.changed ? `Pushed ${r.files} file(s) to ${r.branch} at ${r.commit.slice(0, 8)}.` : `${r.branch} is already up to date (${r.commit.slice(0, 8)}).`);
                          if (r.warnings.length) setPreview({ files: [], warnings: r.warnings });
                        })
                      }
                    >
                      Commit and push
                    </Button>
                  </Box>
                  {result && <Callout color="success">{result}</Callout>}
                </Box>
              )}
            </Tabs.Panel>
          </Tabs>

          {preview && preview.warnings.length > 0 && (
            <Callout color="warning" title="Left out of the export">
              <ul className="m-0 list-none p-0 text-[13px]">{preview.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
            </Callout>
          )}
          {preview && preview.files.length > 0 && (
            <div className="flex flex-col gap-3">
              {preview.files.map((f) => (
                <div key={f.path} className="border">
                  <div className="border-b bg-muted/50 px-5 py-2 font-mono text-[12px]">{f.path}</div>
                  <pre className="m-0 max-h-84 overflow-auto px-5 py-3 font-mono text-[13px] leading-[18px]">{f.content}</pre>
                </div>
              ))}
            </div>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="outline" onClick={onClose}>Close</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
