// Header actions of the deployment page. One primary action (Deploy, or
// Redeploy once it has run), the reversible Start/Stop that matches the current
// state, Pull for a git source, Expose, and a "More" menu holding the
// switches and the destructive actions, each behind a confirmation.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Button, Menu, Text, Tooltip } from "@/ui/kit";
import {
  deleteDeployment,
  setDeploymentEnabled,
  setDeploymentProtection,
  syncDeployment,
} from "../../api/client";
import { notify } from "../../ui/toast";
import ConfirmDeleteModal from "../ConfirmDeleteModal";
import ConfirmModal from "../ConfirmModal";
import ExposeButton from "../exposure/ExposeDialog";
import { composeServices } from "../exposure/compose";
import type { Deployment } from "../../types";
import { errorMessage } from "../../utils";
import { deployInFlight, neverDeployed, useLifecycle } from "./shared";

export default function DeploymentActions({ deployment: d, compact = false }: { deployment: Deployment; compact?: boolean }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const life = useLifecycle(d);
  const [confirm, setConfirm] = useState<"destroy" | "delete" | null>(null);
  const inFlight = deployInFlight(d);
  const isGit = d.type === "compose" && !!d.git?.repoUrl;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["deployment", d.id] });
    qc.invalidateQueries({ queryKey: ["deployments"] });
  };

  const sync = useMutation({
    mutationFn: () => syncDeployment(d.id),
    onSuccess: (r) => {
      notify({
        color: r.changed ? "blue" : "gray",
        title: r.changed ? `Deploying ${r.commit.slice(0, 7)}` : "Already up to date",
        message: r.changed
          ? `A new commit was found on ${d.git?.ref || "main"}; the redeploy runs in the background.`
          : `${d.name} already runs ${r.commit.slice(0, 7)}.`,
      });
      refresh();
    },
    onError: (err) =>
      notify({ color: "red", title: "Pull failed", message: errorMessage(err, "Could not check the repository") }),
  });

  const enabled = useMutation({
    mutationFn: (on: boolean) => setDeploymentEnabled(d.id, on),
    onSuccess: (_r, on) => {
      notify({
        color: on ? "green" : "gray",
        title: on ? "Auto-heal on" : "Auto-heal paused",
        message: on
          ? `Kowan redeploys ${d.name} if its containers go missing or stop.`
          : `${d.name} is left as it is; nothing is redeployed automatically.`,
      });
      refresh();
    },
    onError: (err) =>
      notify({ color: "red", title: "Update failed", message: errorMessage(err, "Could not change auto-heal") }),
  });

  const protection = useMutation({
    mutationFn: (on: boolean) => setDeploymentProtection(d.id, on),
    onSuccess: (_r, on) => {
      notify({
        color: on ? "yellow" : "gray",
        title: on ? "Deletion protection on" : "Deletion protection off",
        message: on ? `${d.name} cannot be deleted until protection is turned off.` : `${d.name} can be deleted.`,
      });
      refresh();
    },
    onError: (err) =>
      notify({ color: "red", title: "Update failed", message: errorMessage(err, "Could not change protection") }),
  });

  const remove = useMutation({
    mutationFn: () => deleteDeployment(d.id),
    onSuccess: () => {
      notify({ color: "green", title: "Deployment deleted", message: `${d.name} and its containers were removed.` });
      qc.invalidateQueries({ queryKey: ["deployments"] });
      navigate("/workloads");
    },
    onError: (err) =>
      notify({ color: "red", title: "Delete failed", message: errorMessage(err, "Could not delete the deployment") }),
  });

  const first = neverDeployed(d);
  const canStop = d.status === "deployed";
  const canStart = d.status === "stopped";
  const host = d.targets?.[0];

  return (
    <>
      {!compact && canStop && (
        <Button variant="outline" color="neutral" iconLeft="stop-rounded" loading={life.acting("stop")} disabled={life.busy} onClick={() => life.run("stop")}>
          Stop
        </Button>
      )}
      {!compact && canStart && (
        <Button variant="outline" color="neutral" iconLeft="play-arrow-rounded" loading={life.acting("start")} disabled={life.busy} onClick={() => life.run("start")}>
          Start
        </Button>
      )}
      {!compact && host && d.strategy !== "swarm" && !isGit && (
        <ExposeButton
          hostId={host}
          name={d.name}
          target={{ kind: "deployment", id: d.id }}
          defaultPort={d.spec?.ports?.[0]?.containerPort}
          services={d.type === "compose" ? composeServices(d.compose ?? "") : undefined}
        />
      )}
      <Menu placement="bottom-end">
        <Menu.Trigger>
          <Button variant="outline" color="neutral" iconRight="arrow-drop-down-rounded">
            {compact ? "Actions" : "More"}
          </Button>
        </Menu.Trigger>
        <Menu.Content ariaLabel="More deployment actions">
          {compact && <Menu.Item iconLeft="rocket-launch-outline-rounded" disabled={inFlight || life.busy || (isGit && sync.isPending)} onSelect={()=>life.run("deploy")}>{inFlight ? "Deploying…" : first ? "Deploy" : "Redeploy"}</Menu.Item>}
          {compact && canStop && <Menu.Item iconLeft="stop-rounded" disabled={life.busy} onSelect={()=>life.run("stop")}>Stop</Menu.Item>}
          {compact && canStart && <Menu.Item iconLeft="play-arrow-rounded" disabled={life.busy} onSelect={()=>life.run("start")}>Start</Menu.Item>}
          {isGit && (
            <Menu.Item iconLeft="download-rounded" disabled={inFlight || sync.isPending} onSelect={() => sync.mutate()}>
              Pull latest commit
            </Menu.Item>
          )}
          {d.status === "error" && (
            <Menu.Item iconLeft="play-arrow-rounded" disabled={life.busy} onSelect={() => life.run("start")}>
              Start existing containers
            </Menu.Item>
          )}
          <Menu.Item
            iconLeft={d.enabled ? "pause-circle-outline" : "play-circle-outline-rounded"}
            trailing={d.enabled ? "on" : "off"}
            onSelect={() => enabled.mutate(!d.enabled)}
          >
            Auto-heal
          </Menu.Item>
          <Menu.Item iconLeft="lock-outline" trailing={d.protected ? "on" : "off"} onSelect={() => protection.mutate(!d.protected)}>
            Deletion protection
          </Menu.Item>
          <Menu.Separator />
          <Menu.Item iconLeft="cancel-outline-rounded" disabled={first || inFlight} onSelect={() => setConfirm("destroy")}>
            Remove containers…
          </Menu.Item>
          <Menu.Item iconLeft="delete-outline-rounded" color="danger" onSelect={() => setConfirm("delete")}>
            Delete deployment…
          </Menu.Item>
        </Menu.Content>
      </Menu>
      {!compact && <Tooltip content={inFlight ? "A deploy is already running" : first ? "Create the containers on the host" : "Recreate the containers from the saved configuration"}>
        <Button
          iconLeft="rocket-launch-outline-rounded"
          loading={life.acting("deploy") || (isGit && sync.isPending)}
          disabled={inFlight || life.busy}
          onClick={() => life.run("deploy")}
        >
          {inFlight ? "Deploying…" : first ? "Deploy" : "Redeploy"}
        </Button>
      </Tooltip>}

      <ConfirmModal
        opened={confirm === "destroy"}
        onClose={() => setConfirm(null)}
        title={`Remove ${d.name}'s containers?`}
        confirmLabel="Remove containers"
        loading={life.acting("destroy")}
        onConfirm={() => {
          void life.runAsync("destroy").finally(() => setConfirm(null)).catch(() => undefined);
        }}
      >
        <Text size="sm" color="muted">
          The containers{d.type === "compose" ? ", networks and the compose project" : ""} are removed from the host. The
          configuration, webhook and history stay, and Deploy brings it back. Named volumes are kept.
        </Text>
      </ConfirmModal>

      <ConfirmDeleteModal
        opened={confirm === "delete"}
        onClose={() => setConfirm(null)}
        itemName={d.name}
        itemKind="deployment"
        protectedOn={d.protected}
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <Text size="sm" color="muted">
          Its containers are torn down first, then the deployment record and its webhook are removed. Volumes are kept.
        </Text>
      </ConfirmDeleteModal>
    </>
  );
}
