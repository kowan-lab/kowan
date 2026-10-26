import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Box, Button, Callout, Icon, Select, StatePill, Text, TextInput, Tooltip } from "@/ui/kit";
import {
  deleteContainerUpdatePolicy,
  deleteDeploymentUpdatePolicy,
  getContainerUpdatePolicy,
  getDeploymentUpdatePolicy,
  putContainerUpdatePolicy,
  putDeploymentUpdatePolicy,
  runAutoUpdateCheck,
  type UpdatePolicyBody,
} from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { notify } from "../../ui/toast";
import { errorMessage, timeAgo } from "../../utils";
import SectionCard from "../SectionCard";
import type { ObjectUpdatePolicy, PolicySource, UpdateMode } from "../../types";
import HistoryTable from "./HistoryTable";
import { MODE_OPTIONS, SCAN_OPTIONS, outcomeColor, outcomeText } from "./labels";

export const POLICY_KEY = "autoupdate-policy";

type Target = { kind: "container"; hostId: string; containerId: string } | { kind: "deployment"; deploymentId: string };

const LABEL_OF = {
  mode: "kowan.update",
  window: "kowan.update.window",
  minAge: "kowan.update.min-age",
  scanBlock: "kowan.scan.block",
} as const;

function FromLabel({ source, label }: { source?: PolicySource; label: string }) {
  if (source !== "label") return null;
  return (
    <Tooltip content={`Set by the container label ${label}; edit it in your compose file.`}>
      <span tabIndex={0} className="inline-flex"><Badge size="sm" variant="outline" color="blue">label</Badge></span>
    </Tooltip>
  );
}

/**
 * Auto-update policy editor for one container or deployment: mode,
 * maintenance window, minimum image age and scan gate. Fields set by a
 * container label are shown read-only ("labels win"). Below: current state
 * and the object's recent history.
 */
export default function PolicyEditor(props: Target) {
  const { user } = useAuth();
  const canEdit = user?.role === "admin" || user?.role === "operator";
  const queryClient = useQueryClient();
  const key = props.kind === "container" ? [POLICY_KEY, props.hostId, props.containerId] : [POLICY_KEY, "dep", props.deploymentId];

  const q = useQuery({
    queryKey: key,
    queryFn: () =>
      props.kind === "container"
        ? getContainerUpdatePolicy(props.hostId, props.containerId)
        : getDeploymentUpdatePolicy(props.deploymentId),
    staleTime: 15_000,
    retry: false,
  });

  const [form, setForm] = useState<UpdatePolicyBody>({ mode: "off", window: "", minAge: "", scanBlock: "" });
  useEffect(() => {
    const d = q.data;
    if (!d) return;
    setForm({
      mode: (d.stored?.mode ?? d.effective.mode) as UpdateMode,
      window: d.stored?.window ?? (d.effective.sources.window === "label" ? "" : d.effective.window),
      minAge: d.stored?.minAge ?? (d.effective.sources.minAge === "label" ? "" : d.effective.minAge),
      scanBlock: d.stored?.scanBlock ?? "",
    });
  }, [q.data]);

  const onSaved = (d: ObjectUpdatePolicy) => {
    queryClient.setQueryData(key, d);
    queryClient.invalidateQueries({ queryKey: ["autoupdate-overview"] });
  };
  const save = useMutation({
    mutationFn: () =>
      props.kind === "container"
        ? putContainerUpdatePolicy(props.hostId, props.containerId, form)
        : putDeploymentUpdatePolicy(props.deploymentId, form),
    onSuccess: (d) => {
      onSaved(d);
      notify({ color: "green", title: "Auto-update", message: "Policy saved" });
    },
    onError: (err) => notify({ color: "red", title: "Save failed", message: errorMessage(err, "Could not save the policy") }),
  });
  const reset = useMutation({
    mutationFn: () =>
      props.kind === "container"
        ? deleteContainerUpdatePolicy(props.hostId, props.containerId)
        : deleteDeploymentUpdatePolicy(props.deploymentId),
    onSuccess: (d) => {
      onSaved(d);
      notify({ color: "green", title: "Auto-update", message: "Stored policy removed" });
    },
    onError: (err) => notify({ color: "red", title: "Reset failed", message: errorMessage(err, "Could not reset the policy") }),
  });
  const check = useMutation({
    mutationFn: () => {
      const d = q.data!;
      return runAutoUpdateCheck({ kind: d.kind, hostId: d.kind === "container" ? d.hostId : undefined, target: d.target });
    },
    onSuccess: () => {
      notify({ color: "blue", title: "Auto-update", message: "Check started; results appear here shortly" });
      window.setTimeout(() => queryClient.invalidateQueries({ queryKey: key }), 4000);
    },
    onError: (err) => notify({ color: "red", title: "Check failed", message: errorMessage(err, "Could not start the check") }),
  });

  const d = q.data;
  if (q.isError) {
    return (
      <SectionCard title="Auto-update">
        <Text size="sm" color="danger">{errorMessage(q.error, "Could not load the auto-update policy.")}</Text>
      </SectionCard>
    );
  }
  if (!d) return null;
  const eff = d.effective;
  const src = eff.sources;
  const ro = (f: keyof typeof LABEL_OF) => !canEdit || src[f] === "label";
  const st = d.state ?? {};
  const windowNote = d.window.valid
    ? d.window.always
      ? "No window: updates may run at any time."
      : d.window.open
        ? `Open now (${d.window.describe}).`
        : `Next opens ${d.window.nextOpen ? new Date(d.window.nextOpen).toLocaleString() : "—"} (${d.window.describe}).`
    : d.window.error;

  return (
    <SectionCard
      title="Auto-update"
      description={
        d.kind === "container"
          ? "Policy for this container (stored by name). Labels in compose override these fields."
          : "Policy for this deployment: pull and redeploy changed services, verify, roll back on failure."
      }
      actions={
        <Box direction="row" align="center" gap={2}>
          <StatePill status={eff.mode} label={`mode ${eff.mode}`} color={eff.mode === "off" ? "gray" : eff.mode === "apply" ? "green" : "blue"} size="sm" />
          {canEdit && eff.mode !== "off" && (
            <Button variant="outline" size="sm" iconLeft="refresh-rounded" loading={check.isPending} onClick={() => check.mutate()}>
              Check now
            </Button>
          )}
        </Box>
      }
    >
      {d.managedByDeployment && (
        <Callout color="info" iconLeft={<Icon name="info-outline-rounded" />} title="Covered by its deployment">
          The deployment's auto-update policy handles this container unless a <code>kowan.update</code> label is set.
        </Callout>
      )}
      {d.swarm && (
        <Callout color="warning" iconLeft={<Icon name="info-outline-rounded" />} title="Swarm deployments are not auto-updated">
          The background updater only handles standalone containers and compose deployments.
        </Callout>
      )}
      {eff.errors.length > 0 && (
        <Callout color="warning" iconLeft={<Icon name="warning-outline-rounded" />} title="Ignored values">
          {eff.errors.join("; ")}
        </Callout>
      )}
      <div className="grid gap-5 md:grid-cols-4">
        <div className="flex flex-col gap-2">
          <Box direction="row" align="center" gap={2}><Text size="xs" weight="medium">Mode</Text><FromLabel source={src.mode} label={LABEL_OF.mode} /></Box>
          <Select
            aria-label="Mode"
            options={MODE_OPTIONS}
            value={src.mode === "label" ? eff.mode : form.mode}
            disabled={ro("mode")}
            onChange={(v) => setForm((f) => ({ ...f, mode: v as UpdateMode }))}
            size="sm"
          />
        </div>
        <div className="flex flex-col gap-2">
          <Box direction="row" align="center" gap={2}><Text size="xs" weight="medium">Maintenance window</Text><FromLabel source={src.window} label={LABEL_OF.window} /></Box>
          <TextInput
            size="sm"
            aria-label="Maintenance window"
            placeholder="any time · e.g. 02:00-05:00 sat,sun"
            value={src.window === "label" ? eff.window : form.window ?? ""}
            disabled={ro("window")}
            onChange={(e) => setForm((f) => ({ ...f, window: e.target.value }))}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Box direction="row" align="center" gap={2}><Text size="xs" weight="medium">Minimum image age</Text><FromLabel source={src.minAge} label={LABEL_OF.minAge} /></Box>
          <TextInput
            size="sm"
            aria-label="Minimum image age"
            placeholder="none · e.g. 2d"
            value={src.minAge === "label" ? eff.minAge : form.minAge ?? ""}
            disabled={ro("minAge")}
            onChange={(e) => setForm((f) => ({ ...f, minAge: e.target.value }))}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Box direction="row" align="center" gap={2}><Text size="xs" weight="medium">Scan gate</Text><FromLabel source={src.scanBlock} label={LABEL_OF.scanBlock} /></Box>
          <Select
            aria-label="Scan gate"
            options={[{ value: "", label: `Default (${SCAN_OPTIONS.find((o) => o.value === eff.scanBlock && src.scanBlock === "default")?.label ?? "global"})` }, ...SCAN_OPTIONS]}
            value={src.scanBlock === "label" ? eff.scanBlock : form.scanBlock ?? ""}
            disabled={ro("scanBlock")}
            onChange={(v) => setForm((f) => ({ ...f, scanBlock: v }))}
            size="sm"
          />
        </div>
      </div>
      <Box direction="row" align="center" justify="between" gap={3} wrap>
        <Text size="xs" color="muted">{windowNote}</Text>
        {canEdit && (
          <Box direction="row" gap={2}>
            {d.stored && (
              <Button variant="clear" size="sm" loading={reset.isPending} onClick={() => reset.mutate()}>
                Reset to defaults
              </Button>
            )}
            <Button size="sm" loading={save.isPending} onClick={() => save.mutate()}>
              Save policy
            </Button>
          </Box>
        )}
      </Box>
      {st.lastResult && (
        <Box direction="row" align="center" gap={2} wrap>
          <Text size="xs" color="muted">Last check {timeAgo(st.lastCheckAt)}</Text>
          <StatePill status={st.lastResult} label={outcomeText(st.lastResult)} color={outcomeColor(st.lastResult)} size="sm" />
          <Text size="xs">{st.lastMessage}</Text>
          {st.trackedRef && st.rollbackRef && (
            <Text size="xs" color="muted">Pinned to {st.rollbackRef} after a rollback; still following {st.trackedRef}.</Text>
          )}
        </Box>
      )}
      {d.history.length > 0 && <HistoryTable items={d.history} />}
    </SectionCard>
  );
}
