// History tab: every saved version of what the deployment runs, a YAML diff
// between any two of them and a one-click rollback (restored as a new
// revision, then redeployed).

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Callout, Checkbox, Popup, Select, Skeleton, Text, Tooltip } from "@/ui/kit";
import { diffDeploymentRevision, listDeploymentRevisions, rollbackDeployment } from "../../api/client";
import { DiffView } from "../gitops/DiffView";
import EmptyState from "../EmptyState";
import SectionCard from "../SectionCard";
import type { Deployment, DeploymentRevision } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage, formatDate, timeAgo } from "../../utils";
import { cn } from "@/lib/utils";

const SOURCE: Record<string, string> = {
  create: "Created",
  edit: "Edited",
  rollback: "Rollback",
  git: "New commit",
  gitops: "GitOps",
  exposure: "Exposure",
  mcp: "AI assistant",
  update: "Updated",
  preview: "Branch preview",
  promote: "Promotion",
  import: "Imported",
  tags: "New image tag",
  registry: "Hosted registry",
};

const FIELD: Record<string, string> = {
  container: "container",
  compose: "compose",
  targets: "host",
  replicas: "replicas",
  strategy: "strategy",
  type: "type",
  name: "name",
  git: "git source",
  commit: "commit",
};

function who(r: DeploymentRevision): string {
  return r.author || (r.source === "git" ? "git" : "Kowan");
}

export default function DeploymentHistory({ deployment: d, canOperate }: { deployment: Deployment; canOperate: boolean }) {
  const qc = useQueryClient();
  const history = useQuery({
    queryKey: ["deployment-revisions", d.id],
    queryFn: () => listDeploymentRevisions(d.id),
    refetchInterval: 15_000,
  });
  const revisions = useMemo(() => history.data ?? [], [history.data]);
  const [selected, setSelected] = useState<number | null>(null);
  const [against, setAgainst] = useState<number | null>(null);
  const [confirm, setConfirm] = useState<DeploymentRevision | null>(null);
  const [deployNow, setDeployNow] = useState(true);

  // Select the newest revision once the list arrives (and after a rollback adds one).
  const newest = revisions[0]?.number ?? null;
  useEffect(() => {
    setSelected(newest);
    setAgainst(null);
  }, [newest]);

  const current = revisions.find((r) => r.number === selected) ?? null;
  const diff = useQuery({
    queryKey: ["deployment-revision-diff", d.id, selected, against],
    queryFn: () => diffDeploymentRevision(d.id, selected!, against ?? undefined),
    enabled: selected !== null,
  });

  const rollback = useMutation({
    mutationFn: (r: DeploymentRevision) => rollbackDeployment(d.id, r.number, deployNow),
    onSuccess: (_dep, r) => {
      notify({
        color: "green",
        title: `Rolled back to #${r.number}`,
        message: deployNow ? `${d.name} is redeploying with revision #${r.number}.` : `Saved as a new revision; deploy it when ready.`,
      });
      setConfirm(null);
      qc.invalidateQueries({ queryKey: ["deployment-revisions", d.id] });
      qc.invalidateQueries({ queryKey: ["deployment", d.id] });
      qc.invalidateQueries({ queryKey: ["deployments"] });
      qc.invalidateQueries({ queryKey: ["deployment-status", d.id] });
    },
    onError: (err) => notify({ color: "red", title: "Rollback failed", message: errorMessage(err, "Could not roll back") }),
  });

  if (history.isLoading) return <Skeleton height={240} />;
  if (history.isError) return <Callout color="danger" title="Could not load the history">{errorMessage(history.error, "")}</Callout>;
  if (revisions.length === 0) {
    return <EmptyState title="No history yet" description="Each save of this deployment's configuration appears here as a revision you can compare and roll back to." />;
  }

  const others = revisions.filter((r) => r.number !== selected);
  return (
    <div className="grid min-w-0 gap-[12px] lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
      <SectionCard title="Revisions" description="Newest first. Every change to what runs is kept." noBodyPadding>
        <ol className="m-0 list-none p-0" aria-label="Revisions">
          {revisions.map((r) => {
            const on = r.number === selected;
            return (
              <li key={r.number} className="border-b last:border-b-0">
                <button
                  type="button"
                  aria-current={on || undefined}
                  onClick={() => {
                    setSelected(r.number);
                    setAgainst(null);
                  }}
                  className={cn(
                    "flex w-full flex-col gap-[4px] px-[16px] py-[10px] text-left outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:outline-[var(--signal)]",
                    on ? "bg-[var(--tone-primary-soft)] shadow-[inset_2px_0_0_var(--signal)]" : "hover:bg-accent",
                  )}
                >
                  <span className="flex flex-wrap items-center gap-[8px]">
                    <span className="font-mono text-[13px] font-semibold">#{r.number}</span>
                    <span className="text-[13px] font-semibold">{SOURCE[r.source] ?? r.source}</span>
                    {r.live && <Badge size="sm" variant="solid" color="green">Live</Badge>}
                  </span>
                  <span className="text-[12px] text-muted-foreground">
                    <span title={formatDate(r.createdAt)}>{timeAgo(r.createdAt)}</span> · {who(r)}
                    {r.note ? ` · ${r.note}` : ""}
                  </span>
                  {r.changes.length > 0 && (
                    <span className="flex flex-wrap gap-[4px]">
                      {r.changes.map((c) => (
                        <span key={c} className="bg-muted px-[6px] font-mono text-[12px] leading-[18px] text-muted-foreground">
                          {FIELD[c] ?? c}
                        </span>
                      ))}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ol>
      </SectionCard>

      {current && (
        <SectionCard
          title={`Revision #${current.number}`}
          description={
            diff.data
              ? diff.data.from
                ? `Changes from #${diff.data.from}`
                : "The first revision: everything it set up."
              : undefined
          }
          actions={
            <div className="flex flex-wrap items-center gap-[8px]">
              {others.length > 0 && (
                <Select
                  aria-label="Compare with"
                  size="sm"
                  width={170}
                  placeholder="Compare with…"
                  value={against !== null ? String(against) : ""}
                  options={[{ value: "", label: "Previous revision" }, ...others.map((r) => ({ value: String(r.number), label: `#${r.number} · ${SOURCE[r.source] ?? r.source}` }))]}
                  onChange={(v) => setAgainst(v ? Number(v) : null)}
                />
              )}
              {/* The newest revision is the current configuration: nothing to restore. */}
              {canOperate && current.number !== newest && (
                <Tooltip content="Restore this revision as a new one">
                  <Button
                    size="sm"
                    iconLeft="history-rounded"
                    onClick={() => {
                      setDeployNow(d.enabled);
                      setConfirm(current);
                    }}
                  >
                    Roll back to #{current.number}
                  </Button>
                </Tooltip>
              )}
            </div>
          }
        >
          {diff.isLoading ? (
            <Skeleton height={160} />
          ) : diff.isError ? (
            <Callout color="danger">{errorMessage(diff.error, "Could not compare the revisions")}</Callout>
          ) : diff.data && diff.data.lines.some((l) => l.op !== " ") ? (
            <DiffView lines={diff.data.lines} />
          ) : (
            <Text size="sm" color="muted">No differences.</Text>
          )}
        </SectionCard>
      )}

      <Popup open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)} size="sm">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Roll back to #{confirm?.number}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            <Text size="sm">
              The configuration of revision #{confirm?.number} is saved as a new revision. Nothing in the history is deleted, so you can
              roll forward again the same way.
            </Text>
            {confirm?.changes.includes("commit") || confirm?.source === "git" ? (
              <Callout color="warning">A git revision is pinned to the commit it ran, with auto-deploy off, so the next push does not undo it.</Callout>
            ) : null}
            <Checkbox label="Deploy it now" checked={deployNow} onChange={(e) => setDeployNow(e.currentTarget.checked)} />
          </Popup.Body>
          <Popup.Footer>
            <Button variant="clear" onClick={() => setConfirm(null)} disabled={rollback.isPending}>Cancel</Button>
            <Button loading={rollback.isPending} onClick={() => confirm && rollback.mutate(confirm)}>
              Roll back
            </Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>
    </div>
  );
}
