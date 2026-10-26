// Deployment sections of a workload's overview: the health callout (last
// deploy error / queued), the git source and the redeploy webhook. Every band
// is a solid surface between hairlines (DESIGN.md).

import { useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Callout, Code, CopyButton, Text, Tooltip } from "@/ui/kit";
import { createDeployHook, deleteDeployHook } from "../../api/client";
import { notify } from "../../ui/toast";
import ConfirmModal from "../ConfirmModal";
import EmptyState from "../EmptyState";
import SectionCard from "../SectionCard";
import type { Deployment } from "../../types";
import { errorMessage, formatDate, timeAgo } from "../../utils";
import { useCanOperate, useDeploymentStatus, useLifecycle } from "./shared";

/* ── small layout pieces ──────────────────────────────────────────────── */

/** Key/value rows: label column + value, stacking on narrow screens. */
function KV({ rows }: { rows: { label: string; value: ReactNode; hint?: ReactNode }[] }) {
  return (
    <dl className="m-0">
      {rows.map((r) => (
        <div key={r.label} className="grid gap-x-6 gap-y-2 border-b px-[var(--page-px,16px)] py-3 last:border-0 sm:grid-cols-[180px_minmax(0,1fr)]">
          <dt className="pt-[2px] text-[12px] font-medium tracking-[0.06em] text-muted-foreground font-mono uppercase">{r.label}</dt>
          <dd className="m-0 flex min-w-0 flex-col gap-2 text-[13px]">
            <div className="flex min-w-0 items-center gap-2">{r.value}</div>
            {r.hint && <div className="text-[12px] text-muted-foreground">{r.hint}</div>}
          </dd>
        </div>
      ))}
    </dl>
  );
}

const Muted = ({ children }: { children: ReactNode }) => <span className="text-muted-foreground">{children}</span>;

/** Only when something needs attention: a failed deploy, a paused deployment
 *  that is down, or a deploy waiting for a worker. */
export function DeploymentHealth({ deployment: d, onShowLogs }: { deployment: Deployment; onShowLogs: () => void }) {
  const status = useDeploymentStatus(d);
  const life = useLifecycle(d);
  const canOperate = useCanOperate();

  if (d.status === "error") {
    const msg = (status.data ?? []).map((t) => t.message).find((m) => m) ?? "";
    return (
      <Callout color="danger" title="The last operation failed">
        <Box direction="column" gap={2}>
          {msg ? (
            <pre className="m-0 max-h-60 overflow-auto font-mono text-[13px] break-words whitespace-pre-wrap">{msg}</pre>
          ) : (
            <Text size="sm">No error detail was kept (the server restarted, or containers exist but did not stay up). Check the container logs.</Text>
          )}
          <Box direction="row" gap={2} wrap>
            {canOperate && (
              <Button size="sm" iconLeft="rocket-launch-outline-rounded" loading={life.acting("deploy")} onClick={() => life.run("deploy")}>
                Redeploy
              </Button>
            )}
            <Button size="sm" variant="outline" color="neutral" iconLeft="description-outline-rounded" onClick={onShowLogs}>
              View logs
            </Button>
          </Box>
        </Box>
      </Callout>
    );
  }
  if (d.status === "pending") {
    return (
      <Callout color="warning" title="Deploy queued">
        Waiting for a free deploy slot; it starts as soon as another deploy finishes.
      </Callout>
    );
  }
  return null;
}

/* ── git source ───────────────────────────────────────────────────────── */

export function DeploymentGitSource({ deployment: d }: { deployment: Deployment }) {
  const git = d.git!;
  const commit = d.deployedCommit ?? "";
  return (
    <SectionCard
      title="Git source"
      description={git.autoDeploy ? `Pushes to ${git.ref || "main"} redeploy automatically.` : "Push to deploy is off; use More › Pull latest commit."}
      noBodyPadding
    >
      <KV
        rows={[
          {
            label: "Repository",
            value: (
              <>
                <span className="min-w-0 font-mono break-all">{git.repoUrl}</span>
                <CopyButton value={git.repoUrl} label="Copy repository URL" />
              </>
            ),
          },
          { label: "Ref · file", value: <span className="font-mono">{git.ref || "main"} · {git.path || "docker-compose.yml"}</span> },
          {
            label: "Deployed commit",
            value: commit ? (
              <>
                <Code>{commit}</Code>
                <CopyButton value={commit} label="Copy commit SHA" />
              </>
            ) : (
              <Muted>Not deployed yet</Muted>
            ),
          },
          {
            label: "Last checked",
            value: d.lastSyncAt ? (
              <Tooltip content={formatDate(d.lastSyncAt)}>
                <span>{timeAgo(d.lastSyncAt)}</span>
              </Tooltip>
            ) : (
              <Muted>Never</Muted>
            ),
          },
        ]}
      />
    </SectionCard>
  );
}

/* ── redeploy webhook ─────────────────────────────────────────────────── */

export function DeploymentWebhook({ deployment: d }: { deployment: Deployment }) {
  const qc = useQueryClient();
  const canOperate = useCanOperate();
  const [showSecret, setShowSecret] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const token = d.redeployToken ?? "";
  const url = token ? `${window.location.origin}/hooks/deploy/${token}` : "";
  const isGit = d.type === "compose" && !!d.git?.repoUrl;
  const secret = d.git?.webhookSecret ?? "";

  const refresh = () => qc.invalidateQueries({ queryKey: ["deployment", d.id] });
  const create = useMutation({
    mutationFn: () => createDeployHook(d.id),
    onSuccess: () => {
      notify({
        color: "green",
        title: token ? "Webhook rotated" : "Webhook created",
        message: token ? "The old URL no longer works. Update it wherever it is used." : "POST to the URL to redeploy.",
      });
      setConfirmRotate(false);
      refresh();
    },
    onError: (err) => notify({ color: "red", title: "Webhook failed", message: errorMessage(err, "Could not create the webhook") }),
  });
  const remove = useMutation({
    mutationFn: () => deleteDeployHook(d.id),
    onSuccess: () => {
      notify({ color: "gray", title: "Webhook removed", message: "The URL no longer redeploys this deployment." });
      refresh();
    },
    onError: (err) => notify({ color: "red", title: "Remove failed", message: errorMessage(err, "Could not remove the webhook") }),
  });

  return (
    <SectionCard
      title="Redeploy webhook"
      description={
        isGit
          ? "One URL for CI and for your git host's push webhook."
          : "POST to this URL from CI to redeploy with the saved configuration."
      }
      noBodyPadding
      actions={
        canOperate && (
          <>
            {token && (
              <Button size="sm" variant="clear" color="neutral" loading={remove.isPending} onClick={() => remove.mutate()}>
                Remove
              </Button>
            )}
            <Button
              size="sm"
              variant="outline"
              color="neutral"
              iconLeft="key-outline-rounded"
              loading={create.isPending && !token}
              onClick={() => (token ? setConfirmRotate(true) : create.mutate())}
            >
              {token ? "Rotate" : "Create webhook"}
            </Button>
          </>
        )
      }
    >
      {token ? (
        <KV
          rows={[
            {
              label: "URL",
              value: (
                <>
                  <span className="min-w-0 font-mono break-all">{url}</span>
                  <CopyButton value={url} label="Copy webhook URL" />
                </>
              ),
              hint: <span className="font-mono">curl -X POST {url}</span>,
            },
            ...(isGit
              ? [
                  {
                    label: "Push secret",
                    value: secret ? (
                      <>
                        <span className="min-w-0 font-mono break-all">{showSecret ? secret : "•".repeat(24)}</span>
                        <Button
                          variant="clear"
                          color="neutral"
                          size="xs"
                          aria-label={showSecret ? "Hide secret" : "Show secret"}
                          iconLeft={showSecret ? "visibility-off-outline-rounded" : "visibility-outline-rounded"}
                          onClick={() => setShowSecret((v) => !v)}
                        />
                        <CopyButton value={secret} label="Copy push secret" />
                      </>
                    ) : (
                      <Muted>None</Muted>
                    ),
                    hint: "GitHub / Gitea: content type application/json, this secret (HMAC SHA-256). GitLab: paste it as the secret token.",
                  },
                ]
              : []),
          ]}
        />
      ) : (
        <EmptyState
          title="No webhook"
          description={
            canOperate
              ? "Create one to redeploy from CI or a git push without signing in."
              : "An operator can create one to redeploy from CI."
          }
        />
      )}

      <ConfirmModal
        opened={confirmRotate}
        onClose={() => setConfirmRotate(false)}
        title="Rotate the webhook?"
        confirmLabel="Rotate"
        confirmColor="warning"
        loading={create.isPending}
        onConfirm={() => create.mutate()}
        message={`The current URL${isGit ? " and push secret stop" : " stops"} working at once. Update CI${isGit ? " and your git host" : ""} with the new value${isGit ? "s" : ""}.`}
      />
    </SectionCard>
  );
}
