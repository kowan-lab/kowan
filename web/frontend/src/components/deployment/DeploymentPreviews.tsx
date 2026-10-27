// Preview environments on a git deployment's overview: the settings (which
// branches, pull requests, how long an idle preview lives, how many), the
// running previews, and a way to start one by hand. A preview's own overview
// shows PreviewBanner instead: what it previews and when it goes away.

import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, InputToggle, NumberInput, Popup, Select, StatePill, Text, TextInput } from "@/ui/kit";
import { createDeploymentPreview, deleteDeployment, listDeploymentPreviews, listDeployments, updateDeployment, type DeploymentInput } from "../../api/client";
import { appHref } from "../../lib/apps";
import { LIVE_REFETCH_MS } from "../../lib/live";
import type { Deployment, PreviewSettings } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage, timeAgo } from "../../utils";
import ConfirmModal from "../ConfirmModal";
import { deploymentFormFromDeployment, deploymentFormToInput } from "../DeploymentForm";
import { Band, btnClass } from "../apps/ui";

export const DEFAULT_PREVIEWS: PreviewSettings = { enabled: true, branches: "", pullRequests: true, ttlHours: 72, max: 5 };

const TTLS = [
  { value: "0", label: "Never" },
  { value: "24", label: "After 1 day" },
  { value: "72", label: "After 3 days" },
  { value: "168", label: "After 7 days" },
  { value: "720", label: "After 30 days" },
];

const STATUS_TONE: Record<string, "green" | "red" | "gray" | "amber"> = {
  deployed: "green", error: "red", stopped: "gray", pending: "amber", deploying: "amber",
};

export function ttlLabel(hours: number): string {
  if (!hours) return "kept until removed";
  if (hours % 24 === 0) return `removed after ${hours / 24} day${hours === 24 ? "" : "s"} without pushes`;
  return `removed after ${hours}h without pushes`;
}

export function settingsSummary(p: PreviewSettings): string {
  const parts: string[] = [];
  if (p.branches.trim()) parts.push(`branches ${p.branches.trim()}`);
  if (p.pullRequests) parts.push("pull requests");
  return `${parts.join(" and ") || "nothing"} · ${ttlLabel(p.ttlHours)} · up to ${p.max}`;
}

/** The deployment's update body with new preview settings. */
function withPreviews(d: Deployment, previews: PreviewSettings): DeploymentInput {
  const input = deploymentFormToInput(deploymentFormFromDeployment(d));
  return { ...input, name: d.name, git: input.git ? { ...input.git, previews } : null } as DeploymentInput;
}

export default function DeploymentPreviews({ deployment: d, canOperate }: { deployment: Deployment; canOperate: boolean }) {
  const qc = useQueryClient();
  const settings = d.git?.previews ?? null;
  const on = !!settings?.enabled;
  const [editing, setEditing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<Deployment | null>(null);
  const q = useQuery({
    queryKey: ["deployment-previews", d.id],
    queryFn: () => listDeploymentPreviews(d.id),
    refetchInterval: LIVE_REFETCH_MS,
    enabled: on,
  });
  const previews = q.data ?? [];

  const remove = useMutation({
    mutationFn: (p: Deployment) => deleteDeployment(p.id),
    onSuccess: (_, p) => {
      setRemoving(null);
      qc.invalidateQueries({ queryKey: ["deployment-previews", d.id] });
      qc.invalidateQueries({ queryKey: ["deployments"] });
      notify({ color: "green", title: "Preview removed", message: `${p.name} and its containers are gone.` });
    },
    onError: (e) => {
      setRemoving(null);
      notify({ color: "red", title: "Not removed", message: errorMessage(e, "") });
    },
  });

  return (
    <Band
      card
      padded
      title="Preview environments"
      count={on ? `${previews.length} of ${settings!.max}` : "off"}
      right={
        canOperate && (
          <>
            <button type="button" className={btnClass({ size: "sm" })} onClick={() => setEditing(true)}>
              {on ? "Settings" : "Set up previews"}
            </button>
            {on && (
              <button type="button" className={btnClass({ size: "sm" })} onClick={() => setCreating(true)} disabled={previews.length >= settings!.max}>
                New preview
              </button>
            )}
          </>
        )
      }
    >
      <div className="flex flex-col gap-[10px] px-[16px] py-[12px]">
        {!on ? (
          <Text size="sm" color="muted">
            Run other branches and pull requests next to this deployment, each as its own workload on the same host, with
            free host ports. The push webhook creates them and removes them when the branch or pull request closes.
          </Text>
        ) : (
          <>
            <Text size="sm" color="muted">Previews for {settingsSummary(settings!)}.</Text>
            {!d.redeployToken && canOperate && (
              <Callout color="warning" title="No webhook yet">
                Previews start from the git host's push and pull request events. Create the deploy webhook on this page and
                add it to the repository, with pull request events if you want those.
              </Callout>
            )}
            {previews.length === 0 ? (
              <Text size="sm">No previews running.</Text>
            ) : (
              <ul className="m-0 flex list-none flex-col p-0" aria-label="Previews">
                {previews.map((p) => (
                  <li key={p.id} className="flex flex-wrap items-center gap-x-[14px] gap-y-[4px] border-b py-[8px] last:border-b-0">
                    <StatePill status={p.status} label={p.status || "stopped"} color={STATUS_TONE[p.status] ?? "gray"} size="sm" />
                    <Link to={appHref(`dep~${p.id}`)} className="font-semibold text-[var(--link)] hover:underline">{p.name}</Link>
                    <Text as="span" size="xs" mono color="muted">{p.preview?.ref}</Text>
                    {p.preview?.pullRequest ? (
                      p.preview.url ? (
                        <a href={p.preview.url} target="_blank" rel="noreferrer" className="text-[13px] text-[var(--link)] hover:underline">
                          #{p.preview.pullRequest} {p.preview.title}
                        </a>
                      ) : (
                        <Text as="span" size="sm">#{p.preview.pullRequest} {p.preview.title}</Text>
                      )
                    ) : null}
                    <Text as="span" size="xs" color="muted" className="ml-auto">last push {timeAgo(p.preview?.lastActivityAt)}</Text>
                    {canOperate && (
                      <Button size="sm" variant="clear" color="danger" onClick={() => setRemoving(p)} aria-label={`Remove ${p.name}`}>
                        Remove
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
      {editing && <SettingsDialog deployment={d} onClose={() => setEditing(false)} />}
      {creating && <NewPreviewDialog deployment={d} onClose={() => setCreating(false)} />}
      <ConfirmModal
        opened={!!removing}
        onClose={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing)}
        loading={remove.isPending}
        title="Remove preview?"
        message={removing ? `${removing.name} is stopped and its containers removed. A new push to ${removing.preview?.ref} creates it again.` : ""}
        confirmLabel="Remove"
        confirmColor="red"
      />
    </Band>
  );
}

function SettingsDialog({ deployment: d, onClose }: { deployment: Deployment; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<PreviewSettings>(() => (d.git?.previews?.enabled ? d.git.previews : DEFAULT_PREVIEWS));
  const [error, setError] = useState("");
  const set = (p: Partial<PreviewSettings>) => setF((x) => ({ ...x, ...p }));
  const save = useMutation({
    mutationFn: (p: PreviewSettings) => updateDeployment(d.id, withPreviews(d, p)),
    onSuccess: (saved) => {
      qc.setQueryData(["deployment", d.id], saved);
      qc.invalidateQueries({ queryKey: ["deployments"] });
      qc.invalidateQueries({ queryKey: ["deployment-previews", d.id] });
      notify({ color: "green", title: saved.git?.previews?.enabled ? "Previews on" : "Previews off",
               message: saved.git?.previews?.enabled ? settingsSummary(saved.git.previews) : "Running previews are removed within minutes." });
      onClose();
    },
    onError: (e) => setError(errorMessage(e, "Not saved")),
  });
  const submit = (enabled: boolean) => {
    if (enabled && !f.branches.trim() && !f.pullRequests) {
      setError("Pick branch patterns, pull requests, or both.");
      return;
    }
    save.mutate({ ...f, branches: f.branches.trim(), enabled });
  };
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Preview environments</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <TextInput
            label="Branches"
            value={f.branches}
            onChange={(e) => set({ branches: e.target.value })}
            placeholder="feature/*, fix-*"
            description={`Comma-separated patterns. A push to a matching branch (not ${d.git?.ref || "main"}) creates a preview. Empty: no branch previews.`}
          />
          <InputToggle
            label="Pull and merge requests"
            checked={f.pullRequests}
            onChange={(e) => set({ pullRequests: e.target.checked })}
          />
          <Select label="Remove idle previews" options={TTLS} value={String(f.ttlHours)} onChange={(v) => v && set({ ttlHours: Number(v) })} />
          <NumberInput label="At most" min={1} max={20} value={f.max} onChange={(v) => set({ max: Number(v) || 1 })} description="Previews running at once. Pushes beyond it are ignored." />
          {error && <Callout color="danger">{error}</Callout>}
        </Popup.Body>
        <Popup.Footer>
          {d.git?.previews?.enabled && (
            <Button variant="clear" color="danger" onClick={() => submit(false)} disabled={save.isPending}>Turn off</Button>
          )}
          <Button variant="clear" onClick={onClose} disabled={save.isPending}>Cancel</Button>
          <Button loading={save.isPending} onClick={() => submit(true)}>Save</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

function NewPreviewDialog({ deployment: d, onClose }: { deployment: Deployment; onClose: () => void }) {
  const qc = useQueryClient();
  const [branch, setBranch] = useState("");
  const [error, setError] = useState("");
  const create = useMutation({
    mutationFn: (b: string) => createDeploymentPreview(d.id, b),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["deployment-previews", d.id] });
      qc.invalidateQueries({ queryKey: ["deployments"] });
      notify({ color: "green", title: "Preview deploying", message: `${p.name} runs ${p.preview?.ref}.` });
      onClose();
    },
    onError: (e) => setError(errorMessage(e, "Not created")),
  });
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="sm">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>New preview</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <form id="new-preview" onSubmit={(e) => { e.preventDefault(); if (branch.trim()) create.mutate(branch.trim()); }}>
            <TextInput label="Branch" value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="feature/checkout" description="Deployed from the same repository and compose file." />
          </form>
          {error && <Callout color="danger">{error}</Callout>}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={create.isPending}>Cancel</Button>
          <Button type="submit" form="new-preview" loading={create.isPending} disabled={!branch.trim()}>Create preview</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

/** On a preview's own overview: what it previews and when it goes away. */
export function PreviewBanner({ deployment: d }: { deployment: Deployment }) {
  const deps = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const p = d.preview!;
  const parent = deps.data?.find((x) => x.id === p.parentId);
  const ttl = parent?.git?.previews?.ttlHours;
  return (
    <Callout color="info" title={p.pullRequest ? `Preview of pull request #${p.pullRequest}` : `Preview of ${p.ref}`}>
      {parent ? (
        <>Runs <code>{p.ref}</code> of <Link to={appHref(`dep~${parent.id}`)} className="text-[var(--link)] hover:underline">{parent.name}</Link> on its own ports.</>
      ) : (
        <>Runs <code>{p.ref}</code>; its deployment is gone, so it is removed shortly.</>
      )}{" "}
      {p.pullRequest && p.url ? <><a href={p.url} target="_blank" rel="noreferrer" className="text-[var(--link)] hover:underline">{p.title || "Open the pull request"}</a>. </> : null}
      {ttl !== undefined && <>It is {ttlLabel(ttl)}{ttl ? ` (last push ${timeAgo(p.lastActivityAt)})` : ""}.</>}
    </Callout>
  );
}
