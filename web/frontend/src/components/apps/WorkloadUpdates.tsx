import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Checkbox, Popup, Select, Skeleton, Text } from "@/ui/kit";
import {
  applyTagUpdates,
  getTagUpdates,
  previewTagUpdates,
  updateContainer,
  updateDeploymentImages,
  updateStack,
  type WorkloadUpdateResult,
} from "../../api/client";
import type { App } from "../../lib/apps";
import { UPDATES_KEY } from "../../lib/updates";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import { DiffView } from "../gitops/DiffView";
import { PullsDuring } from "../shell/PullsPanel";
import { Btn } from "./ui";

const KEEP = "__keep";

/**
 * "Update · N" in the workload header: every service with a newer image for
 * its tag, in one go. A deployment is redeployed once (verified, rolled back
 * on failure); a stack Kowan does not manage has each such container
 * recreated; a lone container is updated on its own.
 */
export function UpdateWorkloadButton({ app }: { app: App }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const outdated = app.containers.filter((c) => c.update?.updateAvailable);
  const run = useMutation({
    mutationFn: async (): Promise<WorkloadUpdateResult> => {
      if (app.deployment) return updateDeploymentImages(app.deployment.id);
      if (app.kind === "stack" && app.project) return updateStack(app.hostIds[0] ?? "", app.project);
      const c = outdated[0];
      await updateContainer(c.hostId, c.container.id);
      return { outcome: "updated", message: `${c.name} updated` };
    },
    onSuccess: (r) => {
      setOpen(false);
      const ok = r.outcome === "updated" || (r.updated ?? 0) > 0;
      notify({
        color: ok ? "green" : "gray",
        title: ok ? `${app.name} updated` : "Nothing to update",
        message: r.message ?? (r.updated != null ? `${r.updated} container${r.updated === 1 ? "" : "s"} updated` : ""),
      });
      void qc.invalidateQueries({ queryKey: [UPDATES_KEY] });
      void qc.invalidateQueries({ queryKey: ["containers"] });
      void qc.invalidateQueries({ queryKey: ["deployment"] });
    },
    onError: (err) => notify({ color: "red", title: `Could not update ${app.name}`, message: errorMessage(err, "Update failed") }),
  });
  if (!outdated.length || app.stale) return null;
  return (
    <>
      <Btn onClick={() => setOpen(true)}>Update · {outdated.length}</Btn>
      <Popup open={open} onOpenChange={(o) => !o && setOpen(false)} size="md">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Update {app.name}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            <Text size="sm">Newer images were published for the tags these services run:</Text>
            <ul className="m-0 flex list-none flex-col gap-[4px] p-0 text-[13px]" aria-label="Services to update">
              {outdated.map((c) => (
                <li key={c.container.id}>
                  <span className="font-semibold">{c.service || c.name}</span> <code className="text-muted-foreground">{c.container.image}</code>
                </li>
              ))}
            </ul>
            <Text size="sm" color="muted">
              {app.deployment
                ? "They are pulled and the deployment is redeployed once. If a service does not come up healthy, it goes back to its current image."
                : "Each container is recreated with its own settings on the new image, and goes back to the old one if it does not come up."}
            </Text>
            <PullsDuring hostIds={app.hostIds} active={run.isPending} />
          </Popup.Body>
          <Popup.Footer>
            <Button variant="clear" onClick={() => setOpen(false)} disabled={run.isPending}>Cancel</Button>
            <Button loading={run.isPending} onClick={() => run.mutate()}>Update</Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>
    </>
  );
}

/**
 * "New versions · N" for a deployment whose images are pinned to version
 * tags (api:2.0) when its registry has newer ones (2.1). Moving to one edits
 * the deployment, so the dialog shows what changes, service by service and as
 * a diff, before applying it as a new revision.
 */
export function NewVersionsButton({ app }: { app: App }) {
  const d = app.deployment;
  const rows = useQuery({
    queryKey: ["tag-updates", d?.id],
    queryFn: () => getTagUpdates(d!.id),
    enabled: !!d && !d.git?.repoUrl,
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
  const [open, setOpen] = useState(false);
  const withNewer = (rows.data ?? []).filter((r) => r.newer.length > 0);
  if (!d || !withNewer.length) return null;
  return (
    <>
      <Btn onClick={() => setOpen(true)}>New versions · {withNewer.length}</Btn>
      {open && <NewVersionsDialog deploymentId={d.id} name={d.name} rows={rows.data ?? []} onClose={() => setOpen(false)} />}
    </>
  );
}

function NewVersionsDialog({ deploymentId, name, rows, onClose }: {
  deploymentId: string; name: string; rows: NonNullable<Awaited<ReturnType<typeof getTagUpdates>>>; onClose: () => void;
}) {
  const qc = useQueryClient();
  const candidates = rows.filter((r) => r.newer.length > 0);
  const others = rows.filter((r) => r.newer.length === 0 && (r.note || r.error));
  const [picked, setPicked] = useState<Record<string, string>>(
    () => Object.fromEntries(candidates.map((r) => [r.service, r.latest ?? r.newer[0]])),
  );
  const [deploy, setDeploy] = useState(true);
  const tags = useMemo(() => Object.fromEntries(Object.entries(picked).filter(([, t]) => t !== KEEP)), [picked]);
  const plan = useQuery({
    queryKey: ["tag-updates-plan", deploymentId, JSON.stringify(tags)],
    queryFn: () => previewTagUpdates(deploymentId, tags),
    enabled: Object.keys(tags).length > 0,
    retry: false,
  });
  const apply = useMutation({
    mutationFn: () => applyTagUpdates(deploymentId, tags, deploy),
    onSuccess: (r) => {
      notify({
        color: "green",
        title: deploy ? `Deploying ${name} with the new versions` : `${name} saved with the new versions`,
        message: r.changes.map((c) => `${c.service || "image"} → ${c.to.split(":").pop()}`).join(", "),
      });
      void qc.invalidateQueries({ queryKey: ["tag-updates", deploymentId] });
      void qc.invalidateQueries({ queryKey: ["deployment", deploymentId] });
      void qc.invalidateQueries({ queryKey: ["deployments"] });
      onClose();
    },
    onError: (err) => notify({ color: "red", title: "Could not apply the new versions", message: errorMessage(err, "") }),
  });
  const changes = plan.data?.changes ?? [];
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>New versions for {name}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <div className="flex flex-col gap-[8px]" aria-label="Services">
            {candidates.map((r) => (
              <div key={r.service} className="flex flex-wrap items-center gap-[12px] text-[13px]">
                <span className="min-w-[120px] font-semibold">{r.service || name}</span>
                <code className="text-muted-foreground">{r.image}</code>
                <Select
                  aria-label={`New tag for ${r.service || name}`}
                  width={200}
                  size="sm"
                  value={picked[r.service]}
                  onChange={(v) => setPicked((p) => ({ ...p, [r.service]: v }))}
                  options={[{ value: KEEP, label: `Keep ${r.current}` }, ...r.newer.map((t, i) => ({ value: t, label: i === 0 ? `${t} (newest)` : t }))]}
                />
              </div>
            ))}
            {others.map((r) => (
              <Text key={r.service} size="xs" color="muted">
                {r.service || name}: {r.error ?? r.note}
              </Text>
            ))}
          </div>

          <div className="mt-[8px] flex flex-col gap-[8px] border-t pt-[12px]">
            <Text size="xs" color="muted" transform="uppercase">What will change</Text>
            {Object.keys(tags).length === 0 ? (
              <Text size="sm" color="muted">Nothing: every service keeps its tag.</Text>
            ) : plan.isLoading ? (
              <Skeleton height={80} />
            ) : plan.isError ? (
              <Callout color="danger">{errorMessage(plan.error, "Could not plan the change")}</Callout>
            ) : (
              <>
                <ul className="m-0 flex list-none flex-col gap-[4px] p-0 text-[13px]" aria-label="Changes">
                  {changes.map((c) => (
                    <li key={c.service}>
                      <span className="font-semibold">{c.service || name}</span>{" "}
                      <code className="text-muted-foreground line-through">{c.from.split(":").pop()}</code> →{" "}
                      <code className="font-semibold text-[var(--tone-green-text)]">{c.to.split(":").pop()}</code>
                    </li>
                  ))}
                </ul>
                <DiffView lines={plan.data!.diff} />
                <Text size="sm" color="muted">Saved as a new revision: you can roll back to the current one from History.</Text>
              </>
            )}
            <Checkbox label={`Redeploy ${name} now`} checked={deploy} onChange={(e) => setDeploy(e.currentTarget.checked)} />
          </div>
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={apply.isPending}>Cancel</Button>
          <Button loading={apply.isPending} disabled={!changes.length || plan.isFetching} onClick={() => apply.mutate()}>
            {deploy ? `Apply ${changes.length} and redeploy` : `Apply ${changes.length}`}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
