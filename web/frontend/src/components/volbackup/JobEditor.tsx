import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Badge, Box, Button, Divider, InputToggle, NumberInput, Popup, Select, TagsInput, Text, TextInput } from "@/ui/kit";
import { listContainers, listDeployments, listHosts, listVolumes } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import CronField from "../schedules/CronField";
import {
  createJob, listRepos, previewSelector, updateJob,
  type BackupJob, type HookCmd, type JobInput, type Retention, type SelectorMode,
} from "./api";

const LABEL_PROJECT = "com.docker.compose.project";

export function blankJob(partial: Partial<JobInput> = {}): JobInput {
  return {
    name: "", hostId: "local", repoId: "", selector: { mode: "names", volumes: [] }, schedule: "0 3 * * *", timezone: "",
    retention: { keepDaily: 7, keepWeekly: 4, keepMonthly: 6 }, hooks: { stopContainers: false, pre: [], post: [] }, tags: [], enabled: true,
    ...partial,
  };
}

function HookList({ label, value, onChange, containers }: { label: string; value: HookCmd[]; onChange: (v: HookCmd[]) => void; containers: string[] }) {
  return (
    <Box direction="column" gap={2}>
      <Box direction="row" align="center" justify="between">
        <Text size="sm" weight="semibold">{label}</Text>
        <Button size="xs" variant="outline" color="neutral" iconLeft="add-rounded"
          onClick={() => onChange([...value, { container: containers[0] ?? "", command: "" }])}>Add command</Button>
      </Box>
      {value.map((h, i) => (
        <Box key={i} direction="row" gap={2} align="end" wrap>
          <Select width={200} aria-label="Container" value={h.container} options={containers.map((c) => ({ value: c, label: c }))}
            onChange={(v) => onChange(value.map((x, j) => (j === i ? { ...x, container: v } : x)))} searchable />
          <TextInput aria-label="Command" placeholder="pg_dump -U postgres app > /var/lib/postgresql/data/dump.sql" value={h.command}
            onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, command: e.target.value } : x)))} />
          <Button variant="clear" color="danger" aria-label="Remove" iconLeft="close-rounded" onClick={() => onChange(value.filter((_, j) => j !== i))} />
        </Box>
      ))}
    </Box>
  );
}

/** Create / edit a backup job; `initial` pre-fills a new one (e.g. from a deployment). */
export default function JobEditor({ open, onClose, onSaved, job, initial }: {
  open: boolean; onClose: () => void; onSaved: () => void; job?: BackupJob | null; initial?: Partial<JobInput>;
}) {
  const [f, setF] = useState<JobInput>(blankJob());
  useEffect(() => {
    if (!open) return;
    setF(job ? {
      name: job.name, hostId: job.hostId, repoId: job.repoId, selector: job.selector, schedule: job.schedule, timezone: job.timezone,
      retention: job.retention, hooks: { stopContainers: !!job.hooks.stopContainers, pre: job.hooks.pre ?? [], post: job.hooks.post ?? [] },
      tags: job.tags, enabled: job.enabled,
    } : blankJob(initial));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, job]);
  const set = (p: Partial<JobInput>) => setF((x) => ({ ...x, ...p }));

  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: open });
  const repos = useQuery({ queryKey: ["volbackups", "repos"], queryFn: listRepos, enabled: open });
  const volumes = useQuery({ queryKey: ["volumes", f.hostId], queryFn: () => listVolumes(f.hostId), enabled: open && !!f.hostId });
  const containers = useQuery({ queryKey: ["containers", f.hostId, "all"], queryFn: () => listContainers(f.hostId, true), enabled: open && !!f.hostId });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, enabled: open && f.selector.mode === "deployment" });
  const containerNames = useMemo(() => (containers.data ?? []).map((c) => (c.names?.[0] ?? c.id).replace(/^\//, "")).sort(), [containers.data]);
  const stacks = useMemo(() => Array.from(new Set((containers.data ?? []).map((c) => c.labels?.[LABEL_PROJECT]).filter(Boolean) as string[])).sort(), [containers.data]);
  const repoOptions = (repos.data ?? []).filter((r) => r.type !== "local" || r.hostId === f.hostId)
    .map((r) => ({ value: r.id, label: r.name, trailing: r.type }));

  const selKey = JSON.stringify(f.selector);
  const preview = useQuery({
    queryKey: ["volbackups", "preview", f.hostId, selKey],
    queryFn: () => previewSelector(f.hostId, f.selector),
    enabled: open && !!f.hostId && (f.selector.mode !== "names" || (f.selector.volumes ?? []).length > 0) &&
      (f.selector.mode !== "deployment" || !!f.selector.deploymentId) && (f.selector.mode !== "stack" || !!f.selector.stack),
    retry: false,
  });

  const save = useMutation({
    mutationFn: () => (job ? updateJob(job.id, f) : createJob(f)),
    onSuccess: () => { notify({ color: "green", title: "Backup job saved", message: f.name }); onSaved(); onClose(); },
    onError: (e) => notify({ color: "red", title: "Save failed", message: errorMessage(e, "Could not save the job") }),
  });
  const ret = (k: keyof Retention) => (
    <NumberInput label={k.replace("keep", "")} min={0} value={f.retention[k] ?? 0} style={{ width: 90 }}
      onChange={(v) => set({ retention: { ...f.retention, [k]: Number(v) || 0 } })} />
  );

  return (
    <Popup open={open} onOpenChange={(o) => { if (!o) onClose(); }} size="lg">
      <Popup.Content>
        <Popup.Header><Popup.Title>{job ? `Edit ${job.name}` : "New backup job"}</Popup.Title><Popup.Close /></Popup.Header>
        <Popup.Body>
          <Box direction="row" wrap gap={3}>
            <TextInput label="Name" required value={f.name} onChange={(e) => set({ name: e.target.value })} maxWidth={260} />
            <Select label="Host" width={180} value={f.hostId} onChange={(v) => set({ hostId: v, repoId: "" })}
              options={(hosts.data ?? []).map((h) => ({ value: h.id, label: h.name }))} />
            <Select label="Repository" width={240} value={f.repoId} onChange={(v) => set({ repoId: v })} options={repoOptions}
              emptyText="No repository usable from this host" />
          </Box>
          <Divider label="Volumes" />
          <Box direction="row" wrap gap={3} align="start">
            <Select label="Select by" width={200} value={f.selector.mode}
              options={[
                { value: "names", label: "Volume names" }, { value: "deployment", label: "Deployment" },
                { value: "stack", label: "Compose stack" }, { value: "label", label: "Label kowan.backup=true" },
              ]}
              onChange={(v) => set({ selector: { mode: v as SelectorMode, volumes: [] } })} />
            {f.selector.mode === "names" && (
              <Box direction="column" gap={1} style={{ flex: 1, minWidth: 260 }}>
                <Select label="Add volume" value="" placeholder="Pick a volume…" searchable
                  options={(volumes.data ?? []).filter((v) => !(f.selector.volumes ?? []).includes(v.name)).map((v) => ({ value: v.name, label: v.name }))}
                  onChange={(v) => v && set({ selector: { ...f.selector, volumes: [...(f.selector.volumes ?? []), v] } })} />
                <Box direction="row" wrap gap={1}>
                  {(f.selector.volumes ?? []).map((v) => (
                    <Badge key={v} variant="outline" color="gray" onDismiss={() => set({ selector: { ...f.selector, volumes: (f.selector.volumes ?? []).filter((x) => x !== v) } })}>{v}</Badge>
                  ))}
                </Box>
              </Box>
            )}
            {f.selector.mode === "deployment" && (
              <Select label="Deployment" width={260} value={f.selector.deploymentId ?? ""} searchable
                options={(deployments.data ?? []).map((d) => ({ value: d.id, label: d.name }))}
                onChange={(v) => set({ selector: { mode: "deployment", deploymentId: v } })} />
            )}
            {f.selector.mode === "stack" && (
              <Select label="Stack" width={260} value={f.selector.stack ?? ""} options={stacks.map((s) => ({ value: s, label: s }))}
                onChange={(v) => set({ selector: { mode: "stack", stack: v } })} />
            )}
          </Box>
          {f.selector.mode !== "names" && (
            <Text size="xs" color={preview.isError ? "danger" : "muted"}>
              {preview.isError ? errorMessage(preview.error, "Could not resolve volumes")
                : preview.data ? (preview.data.length ? `Matches now: ${preview.data.join(", ")}` : "Matches no volume right now")
                : "Resolving…"}
            </Text>
          )}
          <Divider label="Schedule and retention" />
          <CronField cron={f.schedule} timezone={f.timezone} allowEmpty label="Schedule (cron)"
            onCron={(schedule) => set({ schedule })} onTimezone={(timezone) => set({ timezone })} />
          <Box direction="row" wrap gap={2} align="end">
            {ret("keepLast")}{ret("keepDaily")}{ret("keepWeekly")}{ret("keepMonthly")}{ret("keepYearly")}
            <Text size="xs" color="muted" style={{ paddingBottom: 8 }}>restic forget --prune after each backup; all 0 = keep everything</Text>
          </Box>
          <Divider label="Hooks" />
          <InputToggle label="Stop containers using these volumes during the backup" description="They are started again afterwards, even if the backup fails."
            checked={!!f.hooks.stopContainers} onChange={(e) => set({ hooks: { ...f.hooks, stopContainers: e.target.checked } })} />
          <HookList label="Before backup (exec in a running container)" value={f.hooks.pre ?? []} containers={containerNames}
            onChange={(pre) => set({ hooks: { ...f.hooks, pre } })} />
          <HookList label="After backup" value={f.hooks.post ?? []} containers={containerNames}
            onChange={(post) => set({ hooks: { ...f.hooks, post } })} />
          <Divider label="Options" />
          <TagsInput label="Tags" description="Added to every snapshot." value={f.tags} onChange={(tags) => set({ tags })} />
          <InputToggle label="Enabled" checked={f.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        </Popup.Body>
        <Popup.Footer>
          <Button variant="outline" color="neutral" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!f.name.trim() || !f.repoId} onClick={() => save.mutate()}>{job ? "Save" : "Create job"}</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
