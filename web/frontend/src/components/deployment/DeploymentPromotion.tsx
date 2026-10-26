// Promotion on a deployment's overview: which deployment it promotes to
// (staging -> production) and the Promote action, which shows exactly what
// the target would change (images or git commit, as a diff) before doing it.
// A deployment others promote into says so.

import { useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Checkbox, Popup, Select, Skeleton, Text } from "@/ui/kit";
import { getPromotionPlan, listDeployments, promoteDeployment, setDeploymentPromotion } from "../../api/client";
import { appHref } from "../../lib/apps";
import type { Deployment } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import { DiffView } from "../gitops/DiffView";
import { Band, btnClass } from "../apps/ui";

/** Deployments ``d`` can promote into: same kind, not itself, not previews. */
export function promotionCandidates(d: Deployment, all: Deployment[]): Deployment[] {
  const git = !!d.git?.repoUrl;
  return all.filter((x) => x.id !== d.id && !x.preview && x.type === d.type && !!x.git?.repoUrl === git);
}

export default function DeploymentPromotion({ deployment: d, canOperate }: { deployment: Deployment; canOperate: boolean }) {
  const qc = useQueryClient();
  const deps = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const [picking, setPicking] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const all = deps.data ?? [];
  const target = d.promoteTo ? all.find((x) => x.id === d.promoteTo) : undefined;
  const sources = all.filter((x) => x.promoteTo === d.id);
  const candidates = promotionCandidates(d, all);

  const pair = useMutation({
    mutationFn: (to: string) => setDeploymentPromotion(d.id, to),
    onSuccess: (saved) => {
      setPicking(false);
      qc.setQueryData(["deployment", d.id], saved);
      qc.invalidateQueries({ queryKey: ["deployments"] });
      const name = all.find((x) => x.id === saved.promoteTo)?.name;
      notify({ color: "green", title: name ? `Promotes to ${name}` : "Promotion target cleared" });
    },
    onError: (e) => notify({ color: "red", title: "Not saved", message: errorMessage(e, "") }),
  });

  if (d.preview || deps.isLoading) return null;
  if (!target && !sources.length && (!canOperate || !candidates.length)) return null;

  return (
    <Band
      title="Promotion"
      count={target ? `to ${target.name}` : sources.length ? `from ${sources.map((s) => s.name).join(", ")}` : "not set"}
      right={
        canOperate && (
          <>
            {(target || candidates.length > 0) && (
              <button type="button" className={btnClass({ size: "sm" })} onClick={() => setPicking((p) => !p)}>
                {target ? "Change" : "Promote to…"}
              </button>
            )}
            {target && (
              <button type="button" className={btnClass({ size: "sm", solid: true })} onClick={() => setPromoting(true)}>
                Promote to {target.name}
              </button>
            )}
          </>
        )
      }
    >
      <div className="flex flex-col gap-[10px] px-[16px] py-[12px]">
        {target ? (
          <Text size="sm" color="muted">
            Promoting runs in <Link to={appHref(`dep~${target.id}`)} className="text-[var(--link)] hover:underline">{target.name}</Link> what
            this deployment runs: its {d.git?.repoUrl ? "git commit" : d.type === "container" ? "image" : "service images"}. {target.name} keeps
            its own environment, ports and volumes.
          </Text>
        ) : !sources.length ? (
          <Text size="sm" color="muted">
            Pick the deployment that gets what this one runs once it is tested, typically production for a staging deployment.
          </Text>
        ) : null}
        {sources.length > 0 && (
          <Text size="sm" color="muted">
            Receives promotions from{" "}
            {sources.map((s, i) => (
              <span key={s.id}>
                {i > 0 && ", "}
                <Link to={appHref(`dep~${s.id}`)} className="text-[var(--link)] hover:underline">{s.name}</Link>
              </span>
            ))}
            .
          </Text>
        )}
        {picking && (
          <div className="flex flex-wrap items-end gap-[8px]">
            <Select
              size="sm"
              width={260}
              label="Promotes to"
              placeholder="Pick a deployment"
              options={[...(target ? [{ value: "", label: "Nothing" }] : []), ...candidates.map((c) => ({ value: c.id, label: c.name }))]}
              value={d.promoteTo ?? ""}
              disabled={pair.isPending}
              onChange={(v) => v !== undefined && v !== (d.promoteTo ?? "") && pair.mutate(v)}
            />
          </div>
        )}
      </div>
      {promoting && target && <PromoteDialog source={d} target={target} onClose={() => setPromoting(false)} />}
    </Band>
  );
}

function PromoteDialog({ source, target, onClose }: { source: Deployment; target: Deployment; onClose: () => void }) {
  const qc = useQueryClient();
  const [deploy, setDeploy] = useState(true);
  const plan = useQuery({
    queryKey: ["promotion-plan", source.id, target.id],
    queryFn: () => getPromotionPlan(source.id, target.id),
    retry: false,
  });
  const go = useMutation({
    mutationFn: () => promoteDeployment(source.id, target.id, deploy),
    onSuccess: (dep) => {
      qc.invalidateQueries({ queryKey: ["deployments"] });
      qc.invalidateQueries({ queryKey: ["deployment", dep.id] });
      qc.invalidateQueries({ queryKey: ["deployment-revisions", dep.id] });
      notify({ color: "green", title: `Promoted to ${target.name}`, message: deploy ? "It is redeploying now." : "Saved; deploy it to apply." });
      onClose();
    },
    onError: (e) => notify({ color: "red", title: "Not promoted", message: errorMessage(e, "") }),
  });
  const changes = plan.data?.changes ?? [];
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Promote {source.name} to {target.name}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {plan.isLoading ? (
            <Skeleton height={120} />
          ) : plan.isError ? (
            <Callout color="danger" title="Cannot promote">{errorMessage(plan.error, "")}</Callout>
          ) : changes.length === 0 ? (
            <Callout color="info">{target.name} already runs what {source.name} runs.</Callout>
          ) : (
            <>
              <ul className="m-0 flex list-none flex-col gap-[4px] p-0 text-[13px]" aria-label="Changes">
                {changes.map((c) => (
                  <li key={c.what}>
                    <span className="font-semibold">{c.what}</span>{" "}
                    <code className="text-muted-foreground">{c.from || "—"}</code> → <code>{c.to}</code>
                  </li>
                ))}
              </ul>
              {plan.data!.pinned && (
                <Text size="sm" color="muted">{target.name} is pinned to this commit with auto-deploy off, so pushes to its branch do not replace it.</Text>
              )}
              {plan.data!.notes.map((n) => (
                <Text key={n} size="sm" color="warning">{n}</Text>
              ))}
              <DiffView lines={plan.data!.diff} />
              <Checkbox label={`Deploy ${target.name} now`} checked={deploy} onChange={(e) => setDeploy(e.target.checked)} />
            </>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={go.isPending}>Cancel</Button>
          <Button loading={go.isPending} disabled={!changes.length} onClick={() => go.mutate()}>
            Promote to {target.name}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
