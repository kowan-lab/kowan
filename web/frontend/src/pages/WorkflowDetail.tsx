import { useEffect, useRef, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Code,
  Divider,
  Icon,
  Select,
  Skeleton,
  Sortable,
  Spinner,
  Table,
  Tabs,
  Text,
  TextArea,
  TextInput,
  InputToggle,
  Tooltip,
} from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import {
  createWorkflow,
  createWorkflowHook,
  deleteWorkflow,
  deleteWorkflowHook,
  getWorkflow,
  getWorkflowRun,
  listChannels,
  listContainers,
  listDeployments,
  listHosts,
  listRegistryRepositories,
  listRegistryTags,
  listWorkflowRuns,
  listWorkflowTriggerTypes,
  runWorkflow,
  setWorkflowProtection,
  updateWorkflow,
  type WorkflowInput,
} from "../api/client";
import { notify } from "../ui/toast";
import ConfirmDeleteModal from "../components/ConfirmDeleteModal";
import ConnectionField from "../components/ConnectionField";
import CreatableSelect from "../components/CreatableSelect";
import EmptyState from "../components/EmptyState";
import KeyValueEditor, {
  rowsToRecord,
  type KeyValueRow,
} from "../components/KeyValueEditor";
import PageHeader, { type Breadcrumb } from "../components/PageHeader";
import SecretReferenceButton from "../components/SecretReferenceButton";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import type {
  PluginField,
  TriggerSpec,
  Workflow,
  WorkflowRun,
  WorkflowStep,
  WorkflowStepType,
  WorkflowTrigger,
} from "../types";
import { errorMessage, formatDate } from "../utils";
import { useCanOperate } from "../components/deployment/shared";

/** Pull a human-friendly error message out of an axios error. */

const STEP_TYPES: {
  value: WorkflowStepType;
  label: string;
  iconLeft: string;
}[] = [
  { value: "script", label: "Run script", iconLeft: "terminal-rounded" },
  { value: "http", label: "HTTP request", iconLeft: "http-rounded" },
  { value: "deploy", label: "Deployment", iconLeft: "rocket-launch-outline" },
  { value: "container", label: "Container action", iconLeft: "dns-outline-rounded" },
  { value: "run-container", label: "Run container", iconLeft: "play-circle-outline-rounded" },
  { value: "prune", label: "Prune", iconLeft: "mop-outline-rounded" },
  { value: "notify", label: "Send notification", iconLeft: "notifications-outline-rounded" },
  { value: "backup", label: "Backup", iconLeft: "archive-outline-rounded" },
];

const DEPLOY_ACTIONS: { value: string; label: string }[] = [
  { value: "redeploy", label: "Redeploy" },
  { value: "deploy", label: "Deploy" },
  { value: "start", label: "Start" },
  { value: "stop", label: "Stop" },
  { value: "destroy", label: "Destroy" },
];

const PRUNE_TARGETS: { value: string; label: string }[] = [
  { value: "containers", label: "Stopped containers" },
  { value: "images", label: "Dangling images" },
  { value: "images-all", label: "All unused images" },
  { value: "volumes", label: "Unused volumes" },
  { value: "networks", label: "Unused networks" },
  { value: "all", label: "Everything" },
];

const HTTP_METHODS: { value: string; label: string }[] = [
  { value: "GET", label: "GET" },
  { value: "POST", label: "POST" },
  { value: "PUT", label: "PUT" },
  { value: "PATCH", label: "PATCH" },
  { value: "DELETE", label: "DELETE" },
];

const CONTAINER_ACTIONS: { value: string; label: string }[] = [
  { value: "start", label: "Start" },
  { value: "stop", label: "Stop" },
  { value: "restart", label: "Restart" },
];

const NOTIFY_LEVELS: { value: string; label: string }[] = [
  { value: "info", label: "Info" },
  { value: "warning", label: "Warning" },
  { value: "critical", label: "Critical" },
];

// ---------------------------------------------------------------------------
// Editable form value. We keep it close to the Workflow shape but always carry
// an array of steps the builder can mutate freely.
// ---------------------------------------------------------------------------

// A form step is a workflow step plus a client-only stable key. The key gives
// dnd-kit (via Sortable) an identity that survives reordering; it is never sent
// to the backend (see formToInput, which strips it).
type FormStep = WorkflowStep & { _key: number };

// Monotonic source of client-only step keys. Module-level so keys stay unique
// across every step created in the session, regardless of which form made them.
let nextStepKey = 1;
const mintStepKey = () => nextStepKey++;

interface WorkflowFormValue {
  name: string;
  description: string;
  enabled: boolean;
  variables: KeyValueRow[];
  steps: FormStep[];
  triggers: WorkflowTrigger[];
}

function emptyForm(): WorkflowFormValue {
  return {
    name: "",
    description: "",
    enabled: true,
    variables: [],
    steps: [],
    triggers: [],
  };
}

function formFromWorkflow(w: Workflow): WorkflowFormValue {
  return {
    name: w.name,
    description: w.description ?? "",
    enabled: w.enabled,
    variables: Object.entries(w.variables ?? {}).map(([key, value]) => ({
      key,
      value,
    })),
    steps: (w.steps ?? []).map((s) => ({
      _key: mintStepKey(),
      name: s.name ?? "",
      type: s.type,
      config: { ...(s.config ?? {}) },
      continueOnError: !!s.continueOnError,
    })),
    triggers: (w.triggers ?? []).map((t) => ({
      id: t.id,
      type: t.type,
      enabled: t.enabled,
      config: { ...(t.config ?? {}) },
      state: t.state,
      lastFired: t.lastFired,
      lastError: t.lastError,
    })),
  };
}

function formToInput(f: WorkflowFormValue): WorkflowInput {
  return {
    name: f.name.trim(),
    description: f.description.trim(),
    enabled: f.enabled,
    variables: rowsToRecord(f.variables),
    // _key is client-only: drop it so the payload shape matches the backend.
    steps: f.steps.map((s) => ({
      name: s.name.trim(),
      type: s.type,
      config: s.config,
      continueOnError: s.continueOnError,
    })),
    triggers: f.triggers.map((t) => ({
      id: t.id,
      type: t.type,
      enabled: t.enabled,
      config: t.config,
    })),
  };
}

function validateForm(f: WorkflowFormValue): string | null {
  if (f.name.trim().length === 0) return "Name is required";
  for (let i = 0; i < f.steps.length; i++) {
    const s = f.steps[i];
    const label = s.name.trim() || `step ${i + 1}`;
    if (s.type === "script" && !(s.config.script ?? "").trim()) {
      return `Script is required for ${label}`;
    }
    if (s.type === "http" && !(s.config.url ?? "").trim()) {
      return `A URL is required for ${label}`;
    }
    if (s.type === "deploy" && !(s.config.deployment ?? "").trim()) {
      return `A deployment is required for ${label}`;
    }
    if (s.type === "container") {
      if (!(s.config.host ?? "").trim())
        return `A host is required for ${label}`;
      if (!(s.config.container ?? "").trim())
        return `A container is required for ${label}`;
    }
    if (s.type === "run-container") {
      if (!(s.config.host ?? "").trim())
        return `A host is required for ${label}`;
      if (!(s.config.image ?? "").trim())
        return `An image is required for ${label}`;
    }
    if (s.type === "prune" && !(s.config.host ?? "").trim()) {
      return `A host is required for ${label}`;
    }
    if (s.type === "notify" && !(s.config.channel ?? "").trim()) {
      return `A channel is required for ${label}`;
    }
  }
  return null;
}

function newStep(): FormStep {
  return {
    _key: mintStepKey(),
    name: "",
    type: "script",
    config: {},
    continueOnError: false,
  };
}

function newTrigger(type: string): WorkflowTrigger {
  // id is left blank: the server mints a stable id (and a webhook token) on save.
  return { id: "", type, enabled: true, config: {} };
}

// ---------------------------------------------------------------------------
// WorkflowDetail: full-screen editor at /workflows/:id and /workflows/new.
// Tabs: Config (name/description/enabled/cron + steps builder), Runs (history
// with per-step output, polled while running), Automation (webhook run URL).
// ---------------------------------------------------------------------------

export default function WorkflowDetail() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const isNew = id === "" || id === "new";

  const workflowQuery = useQuery({
    queryKey: ["workflow", id],
    queryFn: () => getWorkflow(id),
    enabled: !isNew,
  });

  if (!isNew && workflowQuery.isLoading) {
    return (
      <Box direction="column" gap={6}>
        <Skeleton height={42} width={320} />
        <Skeleton height={1} />
        <Skeleton height={36} width="60%" />
        <Skeleton height={300} />
      </Box>
    );
  }

  if (!isNew && (workflowQuery.isError || !workflowQuery.data)) {
    return (
      <Box direction="column" gap={4}>
        <Button
          variant="clear"
          onClick={() => navigate("/workflows")}
          style={{ alignSelf: "flex-start" }}
        >
          Back to workflows
        </Button>
        <Callout color="danger" title="Workflow not found">
          {errorMessage(
            workflowQuery.error,
            "This workflow could not be loaded.",
          )}
        </Callout>
      </Box>
    );
  }

  const workflow = workflowQuery.data ?? null;

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["workflows"] });
    if (!isNew) {
      queryClient.invalidateQueries({ queryKey: ["workflow", id] });
    }
  };

  // ----- New: render only the editor (no run/automation tabs yet) -----------
  if (isNew || !workflow) {
    return (
      <Box direction="column" gap={6}>
        <PageHeader
          title="New workflow"
          description="Build an ordered list of steps. Save to create it, then trigger it manually, on a schedule, or via webhook."
          icon={<Icon name="route-outline" size="lg" />}
          breadcrumbs={[
            { label: "Workflows", to: "/workflows" },
            { label: "New workflow" },
          ]}
        />
        <ConfigTab
          isNew
          initial={emptyForm()}
          onCreated={(w) => navigate(`/workflows/${w.id}`)}
          onSaved={invalidate}
        />
      </Box>
    );
  }

  const breadcrumbs: Breadcrumb[] = [
    { label: "Workflows", to: "/workflows" },
    { label: workflow.name },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title={workflow.name}
        icon={<Icon name="route-outline" size="lg" />}
        breadcrumbs={breadcrumbs}
        actions={
          <Box direction="row" gap={3} wrap="nowrap">
            <RunNowButton workflowId={workflow.id} onRun={invalidate} />
            <WorkflowDangerActions workflow={workflow} onChanged={invalidate} />
          </Box>
        }
      />

      <Box direction="row" wrap gap={3} align="center">
        <StatusBadge status={workflow.enabled ? "enabled" : "disabled"} />
        {workflow.protected && (
          <Badge color="yellow" variant="light" iconLeft="lock-outline">
            Delete protected
          </Badge>
        )}
      </Box>

      <Tabs defaultValue="config" variant="line">
        <Tabs.List>
          <Tabs.Trigger value="config">Config</Tabs.Trigger>
          <Tabs.Trigger value="runs">Runs</Tabs.Trigger>
          <Tabs.Trigger value="automation">Automation</Tabs.Trigger>
        </Tabs.List>

        {/* forceMount keeps unsaved config edits across tab switches. */}
        <Tabs.Panel value="config" forceMount>
          <ConfigTab
            isNew={false}
            workflowId={workflow.id}
            initial={formFromWorkflow(workflow)}
            onSaved={invalidate}
          />
        </Tabs.Panel>

        <Tabs.Panel value="runs">
          <RunsTab workflowId={workflow.id} />
        </Tabs.Panel>

        <Tabs.Panel value="automation">
          <AutomationTab workflow={workflow} onChanged={invalidate} />
        </Tabs.Panel>
      </Tabs>
    </Box>
  );
}

function RunNowButton({
  workflowId,
  onRun,
}: {
  workflowId: string;
  onRun: () => void;
}) {
  const runMutation = useMutation({
    mutationFn: () => runWorkflow(workflowId),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Workflow",
        message: "Run started — see the Runs tab for progress.",
      });
      onRun();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Run failed",
        message: errorMessage(err, "Could not start the workflow"),
      });
    },
  });

  return (
    <Button
      iconLeft="play-arrow-rounded"
      color="green"
      loading={runMutation.isPending}
      onClick={() => runMutation.mutate()}
    >
      Run now
    </Button>
  );
}

// WorkflowDangerActions renders the deletion-protection toggle and the
// type-to-confirm delete for an existing workflow.
function WorkflowDangerActions({
  workflow,
  onChanged,
}: {
  workflow: Workflow;
  onChanged: () => void;
}) {
  const navigate = useNavigate();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const protectionMutation = useMutation({
    mutationFn: (on: boolean) => setWorkflowProtection(workflow.id, on),
    onSuccess: (_d, on) => {
      notify({
        color: on ? "yellow" : "gray",
        title: on ? "Deletion protection on" : "Deletion protection off",
        message: `${workflow.name} is ${on ? "now protected" : "no longer protected"}.`,
      });
      onChanged();
    },
    onError: (err) =>
      notify({
        color: "red",
        title: "Update failed",
        message: errorMessage(err, "Could not change protection"),
      }),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteWorkflow(workflow.id),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Workflow deleted",
        message: `${workflow.name} was removed.`,
      });
      navigate("/workflows");
    },
    onError: (err) =>
      notify({
        color: "red",
        title: "Delete failed",
        message: errorMessage(err, "Could not delete the workflow"),
      }),
  });

  return (
    <>
      <Tooltip content="Deletion protection">
        <InputToggle
          variant="inline"
          size="sm"
          checked={workflow.protected}
          onChange={(e) => protectionMutation.mutate(e.target.checked)}
          aria-label="Deletion protection"
        />
      </Tooltip>
      <Button
        variant="light"
        color="danger"
        iconLeft="delete-outline-rounded"
        onClick={() => setConfirmOpen(true)}
      >
        Delete
      </Button>
      <ConfirmDeleteModal
        opened={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        itemName={workflow.name}
        itemKind="workflow"
        protectedOn={workflow.protected}
        loading={deleteMutation.isPending}
        onConfirm={() => deleteMutation.mutate()}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Config tab: the editable form (name/description/enabled/cron) + steps builder.
// Used for both create and edit; the parent decides which mutation runs.
// ---------------------------------------------------------------------------

interface ConfigTabProps {
  isNew: boolean;
  workflowId?: string;
  initial: WorkflowFormValue;
  onSaved: () => void;
  onCreated?: (w: Workflow) => void;
}

function ConfigTab({
  isNew,
  workflowId,
  initial,
  onSaved,
  onCreated,
}: ConfigTabProps) {
  const [form, setForm] = useState<WorkflowFormValue>(initial);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form when switching to a different workflow record. We do not
  // resync on every refetch so in-progress edits are not clobbered.
  const loadedId = useRef(workflowId);
  useEffect(() => {
    if (loadedId.current !== workflowId) {
      loadedId.current = workflowId;
      setForm(initial);
      setError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId]);

  const set = <K extends keyof WorkflowFormValue>(
    key: K,
    v: WorkflowFormValue[K],
  ) => setForm((f) => ({ ...f, [key]: v }));

  // --- Variables (run env) editor ------------------------------------------
  const addVariable = () =>
    setForm((f) => ({ ...f, variables: [...f.variables, { key: "", value: "" }] }));

  const updateVariable = (index: number, patch: Partial<KeyValueRow>) =>
    setForm((f) => ({
      ...f,
      variables: f.variables.map((row, i) =>
        i === index ? { ...row, ...patch } : row,
      ),
    }));

  const removeVariable = (index: number) =>
    setForm((f) => ({
      ...f,
      variables: f.variables.filter((_, i) => i !== index),
    }));

  const saveMutation = useMutation({
    mutationFn: (input: WorkflowInput) =>
      isNew
        ? createWorkflow(input)
        : updateWorkflow(workflowId as string, input),
    onSuccess: (w) => {
      notify({
        color: "green",
        title: isNew ? "Workflow created" : "Workflow saved",
        message: `"${w.name}" was saved`,
      });
      onSaved();
      if (isNew) onCreated?.(w);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Save failed",
        message: errorMessage(err, "Could not save the workflow"),
      });
    },
  });

  const submit = () => {
    const msg = validateForm(form);
    if (msg) {
      setError(msg);
      return;
    }
    setError(null);
    saveMutation.mutate(formToInput(form));
  };

  // --- Step builder mutations ----------------------------------------------
  const updateStep = (index: number, patch: Partial<WorkflowStep>) =>
    setForm((f) => ({
      ...f,
      steps: f.steps.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    }));

  const updateStepConfig = (index: number, key: string, value: string) =>
    setForm((f) => ({
      ...f,
      steps: f.steps.map((s, i) =>
        i === index ? { ...s, config: { ...s.config, [key]: value } } : s,
      ),
    }));

  const addStep = () =>
    setForm((f) => ({ ...f, steps: [...f.steps, newStep()] }));

  const removeStep = (index: number) =>
    setForm((f) => ({ ...f, steps: f.steps.filter((_, i) => i !== index) }));

  // Sortable hands us the already-reordered array; just store it.
  const reorderSteps = (steps: FormStep[]) =>
    setForm((f) => ({ ...f, steps }));

  // --- Trigger builder mutations -------------------------------------------
  const updateTrigger = (index: number, patch: Partial<WorkflowTrigger>) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((t, i) =>
        i === index ? { ...t, ...patch } : t,
      ),
    }));

  const updateTriggerConfig = (index: number, key: string, value: string) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.map((t, i) =>
        i === index ? { ...t, config: { ...t.config, [key]: value } } : t,
      ),
    }));

  const addTrigger = (type: string) =>
    setForm((f) => ({ ...f, triggers: [...f.triggers, newTrigger(type)] }));

  const removeTrigger = (index: number) =>
    setForm((f) => ({
      ...f,
      triggers: f.triggers.filter((_, i) => i !== index),
    }));

  return (
    <Box direction="column" gap={6} maxWidth={860}>
      <Box direction="column" gap={4} className="border bg-card p-6">
        <TextInput
          label="Name"
          placeholder="nightly-maintenance"
          required
          data-autofocus
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
        />
        <TextArea
          label="Description"
          placeholder="What this workflow does"
          rows={2}
          value={form.description}
          onChange={(e) => set("description", e.target.value)}
        />
        <Box direction="column" grow>
          <Text size="sm" weight="medium" style={{ marginBottom: 4 }}>
            Enabled
          </Text>
          <InputToggle
            variant="inline"
            style={{ marginTop: 6 }}
            label="Allow scheduled, webhook and polling runs"
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
        </Box>
        <Text size="xs" color="muted">
          To schedule this workflow, add a Cron trigger in the Triggers section
          below.
        </Text>

        <KeyValueEditor
          label="Variables"
          addLabel="Add variable"
          emptyText="No variables. Add key/value pairs to seed the run environment."
          keyPlaceholder="NAME"
          removeAriaLabel="Remove variable"
          rows={form.variables}
          onAdd={addVariable}
          onUpdate={updateVariable}
          onRemove={removeVariable}
        />
        <Text size="xs" color="muted">
          Reference a variable as <Code>$NAME</Code> in any step. Values may
          contain <Code>[[secret]]</Code> references. A script step can export
          values by writing <Code>KEY=value</Code> lines to the file at{" "}
          <Code>$KOWAN_OUTPUT</Code>.
        </Text>
      </Box>

      <Box direction="column" gap={4} className="border bg-card p-6">
        <Divider label="Triggers" />

        <TriggersBuilder
          triggers={form.triggers}
          onUpdateTrigger={updateTrigger}
          onUpdateConfig={updateTriggerConfig}
          onAdd={addTrigger}
          onRemove={removeTrigger}
        />
      </Box>

      <Box direction="column" gap={4} className="border bg-card p-6">
        <Divider label="Steps" />

        <StepsBuilder
          steps={form.steps}
          onUpdateStep={updateStep}
          onUpdateConfig={updateStepConfig}
          onRemove={removeStep}
          onReorder={reorderSteps}
        />
      </Box>

      <Box direction="row" wrap gap={3}>
        <Button
          variant="light"
          iconLeft="add-rounded"
          onClick={addStep}
        >
          Add step
        </Button>
      </Box>

      {error && (
        <Text color="danger" size="sm">
          {error}
        </Text>
      )}

      <Box direction="row" wrap gap={3} justify="end">
        {!isNew && (
          <Button
            variant="outline"
            onClick={() => {
              setForm(initial);
              setError(null);
            }}
            disabled={saveMutation.isPending}
          >
            Reset
          </Button>
        )}
        <Button color="info" onClick={submit} loading={saveMutation.isPending}>
          {isNew ? "Create" : "Save"}
        </Button>
      </Box>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Steps builder: an ordered list of step cards, each with a type Select and the
// type-specific config fields.
// ---------------------------------------------------------------------------

interface StepsBuilderProps {
  steps: FormStep[];
  onUpdateStep: (index: number, patch: Partial<WorkflowStep>) => void;
  onUpdateConfig: (index: number, key: string, value: string) => void;
  onRemove: (index: number) => void;
  onReorder: (steps: FormStep[]) => void;
}

function StepsBuilder({
  steps,
  onUpdateStep,
  onUpdateConfig,
  onRemove,
  onReorder,
}: StepsBuilderProps) {
  // Option sources shared by every step card.
  const deploymentsQuery = useQuery({
    queryKey: ["deployments"],
    queryFn: listDeployments,
  });
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const channelsQuery = useQuery({
    queryKey: ["channels"],
    queryFn: listChannels,
  });

  const deploymentOptions = (deploymentsQuery.data ?? []).map((d) => ({
    value: d.id,
    label: d.name || d.id,
  }));
  const hostOptions = (hostsQuery.data ?? []).map((h) => ({
    value: h.id,
    label: h.name || h.id,
  }));
  const channelOptions = (channelsQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.name || c.id,
  }));

  if (steps.length === 0) {
    return (
      <Text color="muted" size="sm">
        No steps yet. Add a step to build the workflow.
      </Text>
    );
  }

  return (
    // Sortable owns the reorder gesture: it renders a drag handle per row
    // (withHandle) so only the handle starts a drag and the card body stays
    // fully interactive for editing. getKey uses the client-only _key so
    // dnd-kit keeps a stable identity across reorders.
    <Sortable
      items={steps}
      getKey={(s) => s._key}
      onChange={onReorder}
      bordered={false}
      renderItem={(step, ctx) => (
        <Card variant="raised" style={{ flex: 1 }}>
          <Card.Body>
            <Box direction="column" gap={3}>
              <Box direction="row" wrap="nowrap" justify="between" align="start" gap={3}>
                <Box direction="row" wrap="nowrap" gap={3} style={{ flex: 1 }}>
                  <Badge variant="light" color="blue" size="lg">
                    {ctx.index + 1}
                  </Badge>
                  <TextInput
                    placeholder={`Step ${ctx.index + 1} name (optional)`}
                    value={step.name}
                    onChange={(e) =>
                      onUpdateStep(ctx.index, { name: e.target.value })
                    }
                    style={{ flex: 1 }}
                  />
                  <Select
                    width={180}
                    options={STEP_TYPES}
                    value={step.type}
                    onChange={(v) =>
                      v &&
                      onUpdateStep(ctx.index, {
                        type: v as WorkflowStepType,
                        // Reset config when the type changes so stale keys don't
                        // leak into the new step shape.
                        config: {},
                      })
                    }
                  />
                </Box>
                <Box direction="row" wrap="nowrap" gap={1}>
                  <Tooltip content="Remove step">
                    <Button
                      variant="clear"
                      color="danger"
                      aria-label="Remove step"
                      onClick={() => onRemove(ctx.index)}
                      iconLeft="delete-outline-rounded"
                    />
                  </Tooltip>
                </Box>
              </Box>

              <StepConfig
                step={step}
                index={ctx.index}
                onUpdateConfig={onUpdateConfig}
                deploymentOptions={deploymentOptions}
                hostOptions={hostOptions}
                channelOptions={channelOptions}
              />

              <InputToggle
                variant="inline"
                label="Continue on error"
                description="If this step fails, keep running the remaining steps."
                checked={step.continueOnError}
                onChange={(e) =>
                  onUpdateStep(ctx.index, {
                    continueOnError: e.target.checked,
                  })
                }
              />
            </Box>
          </Card.Body>
        </Card>
      )}
    />
  );
}

interface StepConfigProps {
  step: WorkflowStep;
  index: number;
  onUpdateConfig: (index: number, key: string, value: string) => void;
  deploymentOptions: { value: string; label: string }[];
  hostOptions: { value: string; label: string }[];
  channelOptions: { value: string; label: string }[];
}

function StepConfig({
  step,
  index,
  onUpdateConfig,
  deploymentOptions,
  hostOptions,
  channelOptions,
}: StepConfigProps) {
  const cfg = (key: string) => step.config[key] ?? "";
  const setCfg = (key: string, value: string) =>
    onUpdateConfig(index, key, value);

  switch (step.type) {
    case "script":
      return (
        <Box direction="column" gap={2}>
          <Box direction="row" wrap gap={2} justify="end">
            <SecretReferenceButton
              onInsert={(tok) => setCfg("script", cfg("script") + tok)}
            />
          </Box>
          <TextArea
            label="Script"
            description="Bash script run on the control plane. Reference secrets with [[secret-name.field]]."
            placeholder={"#!/bin/bash\necho hello [[my-secret.token]]"}
            rows={4}
            style={{ fontFamily: "monospace", fontSize: 13 }}
            value={cfg("script")}
            onChange={(e) => setCfg("script", e.target.value)}
          />
        </Box>
      );

    case "http":
      return (
        <Box direction="column" gap={3}>
          <Box direction="row" gap={4} align="end">
            <Select
              label="Method"
              options={HTTP_METHODS}
              value={cfg("method") || "POST"}
              onChange={(v) => setCfg("method", v ?? "POST")}
              width={140}
            />
            <TextInput
              label="URL"
              placeholder="https://api.example.com/hook"
              fullWidth
              value={cfg("url")}
              onChange={(e) => setCfg("url", e.target.value)}
            />
          </Box>
          <Box direction="column" gap={2}>
            <Box direction="row" wrap gap={2} justify="end">
              <SecretReferenceButton
                onInsert={(tok) => setCfg("headers", cfg("headers") + tok)}
              />
            </Box>
            <TextArea
              label="Headers"
              description="One per line, e.g. Content-Type: application/json"
              placeholder={"Content-Type: application/json"}
              rows={2}
              style={{ fontFamily: "monospace", fontSize: 13 }}
              value={cfg("headers")}
              onChange={(e) => setCfg("headers", e.target.value)}
            />
          </Box>
          <ConnectionField
            capability="httpBearer"
            optional
            isEdit
            label="Credential"
            description="Optional. Adds an Authorization header from the selected credential; leave as None for public endpoints."
            value={cfg("connectionRef")}
            onChange={(v) => setCfg("connectionRef", v)}
          />
          <TextArea
            label="Body"
            rows={3}
            style={{ fontFamily: "monospace", fontSize: 13 }}
            placeholder={'{"text": "{{body}}"}'}
            value={cfg("body")}
            onChange={(e) => setCfg("body", e.target.value)}
          />
          <TextInput
            label="Expected status"
            description="e.g. 200 or 2xx. Leave empty to accept any 2xx."
            placeholder="2xx"
            value={cfg("expectStatus")}
            onChange={(e) => setCfg("expectStatus", e.target.value)}
            maxWidth={220}
          />
          <Callout color="blue" variant="light">
            Secrets resolve with <code>[[name]]</code>. When this workflow is run
            by its webhook, template the incoming request with{" "}
            <code>{"{{body}}"}</code>, <code>{"{{method}}"}</code>,{" "}
            <code>{"{{header.NAME}}"}</code> and <code>{"{{query.NAME}}"}</code>.
          </Callout>
        </Box>
      );

    case "deploy":
      return (
        <Box direction="row" gap={4} align="start">
          <Select
            label="Deployment"
            description="The deployment to act on."
            placeholder="Select a deployment"
            options={deploymentOptions}
            value={cfg("deployment") || undefined}
            onChange={(v) => setCfg("deployment", v ?? "")}
            searchable
            fullWidth
          />
          <Select
            label="Action"
            description="What to do with the deployment."
            options={DEPLOY_ACTIONS}
            value={cfg("action") || "redeploy"}
            onChange={(v) => setCfg("action", v ?? "redeploy")}
            fullWidth
          />
        </Box>
      );

    case "container":
      return (
        <ContainerStepFields
          host={cfg("host")}
          container={cfg("container")}
          action={cfg("action") || "restart"}
          hostOptions={hostOptions}
          onSetHost={(v) => setCfg("host", v)}
          onSetContainer={(v) => setCfg("container", v)}
          onSetAction={(v) => setCfg("action", v)}
        />
      );

    case "run-container":
      return (
        <Box direction="column" gap={3}>
          <Box direction="row" gap={4} align="start">
            <Select
              label="Host"
              placeholder="Select a host"
              options={hostOptions}
              value={cfg("host") || undefined}
              onChange={(v) => setCfg("host", v ?? "")}
              searchable
              fullWidth
            />
            <TextInput
              label="Image"
              description="Image ref to run"
              placeholder="alpine:latest"
              fullWidth
              value={cfg("image")}
              onChange={(e) => setCfg("image", e.target.value)}
            />
          </Box>
          <TextInput
            label="Command"
            description="Optional. Shell-split, e.g. sh -c 'echo hi'."
            placeholder="echo hello"
            value={cfg("command")}
            onChange={(e) => setCfg("command", e.target.value)}
          />
          <TextArea
            label="Environment"
            description="Optional. One KEY=VALUE per line."
            placeholder={"FOO=bar\nDEBUG=1"}
            rows={2}
            style={{ fontFamily: "monospace", fontSize: 13 }}
            value={cfg("env")}
            onChange={(e) => setCfg("env", e.target.value)}
          />
          <TextInput
            label="Output variable"
            description="Optional. The container's stdout is exported under this name so later steps read it as $NAME (and $NAME_EXIT for the exit code)."
            placeholder="RESULT"
            value={cfg("outputVar")}
            onChange={(e) => setCfg("outputVar", e.target.value)}
          />
          <InputToggle
            variant="inline"
            label="Auto-remove"
            description="Remove the container after it exits."
            checked={!!cfg("remove")}
            onChange={(e) => setCfg("remove", e.target.checked ? "true" : "")}
          />
        </Box>
      );

    case "prune":
      return (
        <Box direction="row" gap={4} align="start">
          <Select
            label="Host"
            placeholder="Select a host"
            options={hostOptions}
            value={cfg("host") || undefined}
            onChange={(v) => setCfg("host", v ?? "")}
            searchable
            fullWidth
          />
          <Select
            label="Target"
            description="What to prune on the host."
            options={PRUNE_TARGETS}
            value={cfg("target") || "containers"}
            onChange={(v) => setCfg("target", v ?? "containers")}
            fullWidth
          />
        </Box>
      );

    case "notify":
      return (
        <Box direction="column" gap={3}>
          <Box direction="row" gap={4} align="start">
            <Select
              label="Channel"
              placeholder="Select a channel"
              options={channelOptions}
              value={cfg("channel") || undefined}
              onChange={(v) => setCfg("channel", v ?? "")}
              searchable
              fullWidth
            />
            <Select
              label="Level"
              options={NOTIFY_LEVELS}
              value={cfg("level") || "info"}
              onChange={(v) => setCfg("level", v ?? "info")}
              fullWidth
            />
          </Box>
          <TextInput
            label="Title"
            placeholder="Deploy finished"
            value={cfg("title")}
            onChange={(e) => setCfg("title", e.target.value)}
          />
          <TextArea
            label="Body"
            placeholder="Message body"
            rows={2}
            value={cfg("body")}
            onChange={(e) => setCfg("body", e.target.value)}
          />
        </Box>
      );

    case "backup":
      return (
        <Box direction="column" gap={3}>
          <TextInput
            label="Path"
            description="Server-side directory; defaults to <data>/backups. A timestamped kowan-backup-*.tar.gz archive is written here."
            placeholder="<data>/backups"
            value={cfg("path")}
            onChange={(e) => setCfg("path", e.target.value)}
          />
          <Callout color="blue" variant="light">
            Pair this step with a Cron trigger to get automated, scheduled
            backups.
          </Callout>
        </Box>
      );

    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// container step: pick a host, then the container to act on. The container is a
// CreatableSelect fed by the containers running on the chosen host — but
// free-typing is always allowed so a container that isn't currently running (or
// isn't in the list) can still be targeted. Its own component so the containers
// query can use a hook without violating the rules-of-hooks in StepConfig's
// switch.
// ---------------------------------------------------------------------------

function ContainerStepFields({
  host,
  container,
  action,
  hostOptions,
  onSetHost,
  onSetContainer,
  onSetAction,
}: {
  host: string;
  container: string;
  action: string;
  hostOptions: { value: string; label: string }[];
  onSetHost: (value: string) => void;
  onSetContainer: (value: string) => void;
  onSetAction: (value: string) => void;
}) {
  // Containers on the selected host. Only fetched once a host is chosen; the
  // list feeds the picker's options while free-typing covers everything else.
  const containersQuery = useQuery({
    queryKey: ["containers", host],
    queryFn: () => listContainers(host),
    enabled: !!host,
  });

  const containerOptions = (containersQuery.data ?? []).map((c) => {
    // Docker container names carry a leading slash; strip it for a stable,
    // human id that matches what the engine accepts as a target.
    const name = (c.names[0] ?? "").replace(/^\//, "") || c.id;
    const hint = c.state || (c.image ? c.image.split("@")[0] : "");
    return { value: name, label: hint ? `${name} (${hint})` : name };
  });

  return (
    <Box direction="row" gap={4} align="start">
      <Select
        label="Host"
        placeholder="Select a host"
        options={hostOptions}
        value={host || undefined}
        onChange={(v) => onSetHost(v ?? "")}
        searchable
        fullWidth
      />
      <Box style={{ flex: 1 }}>
        <CreatableSelect
          label="Container"
          placeholder="my-container"
          description={
            host
              ? "Pick a container on the host, or type a name/id."
              : "Select a host to list its containers, or type a name/id."
          }
          value={container}
          onChange={onSetContainer}
          options={containerOptions}
          loading={containersQuery.isFetching}
        />
      </Box>
      <Select
        label="Action"
        options={CONTAINER_ACTIONS}
        value={action}
        onChange={(v) => onSetAction(v ?? "restart")}
        fullWidth
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Triggers builder: an OR'd list of trigger cards. Each has a type Select (from
// the pluggable catalog), an enabled switch, the type-specific config fields,
// and — for a saved webhook trigger — its public run URL. The trigger catalog
// (incl. each type's field schema) is fetched from /workflow-trigger-types.
// ---------------------------------------------------------------------------

interface TriggersBuilderProps {
  triggers: WorkflowTrigger[];
  onUpdateTrigger: (index: number, patch: Partial<WorkflowTrigger>) => void;
  onUpdateConfig: (index: number, key: string, value: string) => void;
  onAdd: (type: string) => void;
  onRemove: (index: number) => void;
}

function TriggersBuilder({
  triggers,
  onUpdateTrigger,
  onUpdateConfig,
  onAdd,
  onRemove,
}: TriggersBuilderProps) {
  const specsQuery = useQuery({
    queryKey: ["workflow-trigger-types"],
    queryFn: listWorkflowTriggerTypes,
  });
  const specs = specsQuery.data ?? [];
  const specByType = (type: string): TriggerSpec | undefined =>
    specs.find((s) => s.type === type);
  const typeOptions = specs.map((s) => ({ value: s.type, label: s.label }));

  return (
    <Box direction="column" gap={4}>
      {triggers.length === 0 ? (
        <Text color="muted" size="sm">
          No triggers. Add one to start this workflow automatically — any enabled
          trigger fires it (they are OR'd together).
        </Text>
      ) : (
        triggers.map((trigger, index) => (
          <TriggerCard
            key={index}
            trigger={trigger}
            index={index}
            spec={specByType(trigger.type)}
            typeOptions={typeOptions}
            onUpdateTrigger={onUpdateTrigger}
            onUpdateConfig={onUpdateConfig}
            onRemove={onRemove}
          />
        ))
      )}

      <Box direction="row" wrap gap={3}>
        <Button
          variant="light"
          iconLeft="add-rounded"
          disabled={specs.length === 0}
          onClick={() => onAdd(specs[0]?.type ?? "cron")}
        >
          Add trigger
        </Button>
      </Box>
    </Box>
  );
}

interface TriggerCardProps {
  trigger: WorkflowTrigger;
  index: number;
  spec: TriggerSpec | undefined;
  typeOptions: { value: string; label: string }[];
  onUpdateTrigger: (index: number, patch: Partial<WorkflowTrigger>) => void;
  onUpdateConfig: (index: number, key: string, value: string) => void;
  onRemove: (index: number) => void;
}

function TriggerCard({
  trigger,
  index,
  spec,
  typeOptions,
  onUpdateTrigger,
  onUpdateConfig,
  onRemove,
}: TriggerCardProps) {
  const fields: PluginField[] = spec?.fields ?? [];
  const isWebhook = spec?.kind === "webhook" || trigger.type === "webhook";
  const token = trigger.config.token ?? "";
  const hookUrl = token
    ? `${window.location.origin}/hooks/workflow/${token}`
    : "";

  return (
    <Card variant="raised">
      <Card.Body>
        <Box direction="column" gap={3}>
          <Box direction="row" wrap="nowrap" justify="between" align="start" gap={3}>
            <Box direction="row" wrap="nowrap" gap={3} align="center" style={{ flex: 1 }}>
              <Select
                width={210}
                options={typeOptions}
                value={trigger.type}
                onChange={(v) =>
                  v &&
                  onUpdateTrigger(index, {
                    type: v,
                    // Reset config when the type changes so stale keys (and any
                    // webhook token) don't leak into the new trigger shape.
                    config: {},
                  })
                }
              />
              {spec?.kind && (
                <Badge variant="light" color="magenta" size="sm">
                  {spec.kind}
                </Badge>
              )}
              {trigger.lastError && (
                <Tooltip content={trigger.lastError}>
                  <Badge variant="light" color="red" size="sm">
                    error
                  </Badge>
                </Tooltip>
              )}
              {trigger.lastFired && !trigger.lastError && (
                <Text size="xs" color="muted">
                  last fired {formatDate(trigger.lastFired)}
                </Text>
              )}
            </Box>
            <Box direction="row" wrap="nowrap" gap={1} align="center">
              <Tooltip content={trigger.enabled ? "Enabled" : "Disabled"}>
                <InputToggle
                  variant="inline"
                  size="sm"
                  checked={trigger.enabled}
                  onChange={(e) =>
                    onUpdateTrigger(index, { enabled: e.target.checked })
                  }
                  aria-label="Trigger enabled"
                />
              </Tooltip>
              <Tooltip content="Remove trigger">
                <Button
                  variant="clear"
                  color="danger"
                  aria-label="Remove trigger"
                  onClick={() => onRemove(index)}
                  iconLeft="delete-outline-rounded"
                />
              </Tooltip>
            </Box>
          </Box>

          {spec?.description && (
            <Text size="xs" color="muted">
              {spec.description}
            </Text>
          )}

          {trigger.type === "docker-image" ? (
            <DockerImageTriggerFields
              index={index}
              config={trigger.config}
              onUpdateConfig={onUpdateConfig}
            />
          ) : (
            fields.length > 0 && (
            <Box direction="column" gap={2}>
              {fields.map((f) =>
                f.connection ? (
                  <ConnectionField
                    key={f.key}
                    capability={f.connection.capability as any}
                    types={f.connection.types}
                    optional={f.connection.optional}
                    isEdit
                    label={f.label}
                    description="Auth for private sources comes from the selected credential. Leave as None for public."
                    value={trigger.config[f.key] ?? ""}
                    onChange={(v) => onUpdateConfig(index, f.key, v)}
                  />
                ) : f.sensitive ? (
                  <Box key={f.key} direction="column" gap={2}>
                    <TextInput
                      type="password"
                      label={f.label}
                      placeholder={f.placeholder}
                      required={f.required}
                      autoComplete="new-password"
                      value={trigger.config[f.key] ?? ""}
                      onChange={(e) =>
                        onUpdateConfig(index, f.key, e.target.value)
                      }
                    />
                    <Box direction="row" wrap gap={2}>
                      <SecretReferenceButton
                        label="Reference a secret"
                        onInsert={(tok) =>
                          onUpdateConfig(
                            index,
                            f.key,
                            (trigger.config[f.key] ?? "") + tok,
                          )
                        }
                      />
                    </Box>
                  </Box>
                ) : f.options && f.options.length > 0 ? (
                  <Select
                    key={f.key}
                    label={f.label}
                    placeholder={f.placeholder}
                    options={f.options.map((o) => ({ value: o, label: o }))}
                    value={trigger.config[f.key] ?? ""}
                    onChange={(v) => onUpdateConfig(index, f.key, v ?? "")}
                  />
                ) : (
                  <TextInput
                    key={f.key}
                    label={f.label}
                    placeholder={f.placeholder}
                    required={f.required}
                    value={trigger.config[f.key] ?? ""}
                    onChange={(e) =>
                      onUpdateConfig(index, f.key, e.target.value)
                    }
                  />
                ),
              )}
            </Box>
            )
          )}

          {isWebhook &&
            (hookUrl ? (
              <div>
                <Text size="xs" weight="medium" style={{ marginBottom: 4 }}>
                  Run URL (POST here to trigger)
                </Text>
                <Box direction="row" wrap="nowrap" gap={2} align="center">
                  <Code
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {hookUrl}
                  </Code>
                  <CopyUrlButton value={hookUrl} label="Copy run URL" />
                </Box>
              </div>
            ) : (
              <Callout color="blue" variant="light">
                Save the workflow to generate this webhook's secret run URL.
              </Callout>
            ))}
        </Box>
      </Card.Body>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// docker-image trigger: a bespoke editor (like the http step) instead of the
// generic field loop. Credentials AND the registry host come from a registry
// connection (dockerRegistryAuth capability); the image is chosen with live
// repository/tag pickers backed by the registry-browse API. Leaving the
// connection as None targets public Docker Hub images. The repository/tag
// pickers always allow free-typing (registries that don't support catalog, or a
// tag not yet pushed).
// ---------------------------------------------------------------------------

function DockerImageTriggerFields({
  index,
  config,
  onUpdateConfig,
}: {
  index: number;
  config: Record<string, string>;
  onUpdateConfig: (index: number, key: string, value: string) => void;
}) {
  const connectionRef = config.connectionRef ?? "";
  const repository = config.repository ?? "";
  const tag = config.tag ?? "";

  // Options load from the connection's registry. Only enabled with a connection
  // (public Docker Hub has no browsable catalog); free-typing covers that case.
  const reposQuery = useQuery({
    queryKey: ["registry-repositories", connectionRef],
    queryFn: () => listRegistryRepositories(connectionRef),
    enabled: !!connectionRef,
  });
  const tagsQuery = useQuery({
    queryKey: ["registry-tags", connectionRef, repository],
    queryFn: () => listRegistryTags(connectionRef, repository),
    enabled: !!connectionRef && !!repository,
  });

  const repoOptions = (reposQuery.data ?? []).map((r) => ({
    value: r,
    label: r,
  }));
  const tagOptions = (tagsQuery.data ?? []).map((t) => ({ value: t, label: t }));

  return (
    <Box direction="column" gap={3}>
      <ConnectionField
        capability="dockerRegistryAuth"
        optional
        isEdit
        label="Registry credential"
        description="Provides both the auth and the registry host. Leave as None for public Docker Hub images."
        value={connectionRef}
        onChange={(v) => onUpdateConfig(index, "connectionRef", v)}
      />
      <CreatableSelect
        label="Repository"
        placeholder="library/nginx"
        description={
          connectionRef
            ? "Pick a repository from the registry, or type one."
            : "Type the repository, e.g. library/nginx (Docker Hub)."
        }
        value={repository}
        onChange={(v) => onUpdateConfig(index, "repository", v)}
        options={repoOptions}
        loading={reposQuery.isFetching}
      />
      <CreatableSelect
        label="Tag"
        placeholder="latest"
        description={
          connectionRef && repository
            ? "Pick a tag from the repository, or type one."
            : "Type the tag to watch, e.g. latest."
        }
        value={tag}
        onChange={(v) => onUpdateConfig(index, "tag", v)}
        options={tagOptions}
        loading={tagsQuery.isFetching}
      />
    </Box>
  );
}

// Icon-only copy-to-clipboard button.
function CopyUrlButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <Tooltip content={copied ? "Copied" : label}>
      <Button
        variant="clear"
        color={copied ? "green" : "neutral"}
        aria-label={label}
        onClick={copy}
        iconLeft={
          copied
            ? "check-rounded"
            : "content-copy-outline-rounded"
        }
      />
    </Tooltip>
  );
}

// ---------------------------------------------------------------------------
// Runs tab: run history; selecting a run shows per-step status + output, polled
// while the run is still running.
// ---------------------------------------------------------------------------

function RunsTab({ workflowId }: { workflowId: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const canOperate = useCanOperate();

  const runsQuery = useQuery({
    queryKey: ["workflow-runs", workflowId],
    queryFn: () => listWorkflowRuns(workflowId),
    enabled: canOperate,
    refetchInterval: (query) => {
      const runs = query.state.data as WorkflowRun[] | undefined;
      return runs?.some((r) => r.status === "running") ? 2000 : 5000;
    },
  });

  const runs = runsQuery.data ?? [];

  if (!canOperate) {
    return (
      <SectionCard title="Run history">
        <EmptyState
          title="Visible to operators"
          description="Runs can include script output and responses with secrets, so only operators and admins can read them."
        />
      </SectionCard>
    );
  }

  return (
    <Box direction="row" wrap="nowrap" align="start" gap={6}>
      <Box style={{ flex: "0 0 340px", minWidth: 0 }}>
        <SectionCard
          title="Run history"
          actions={
            !runsQuery.isLoading &&
            !runsQuery.isError && (
              <Badge variant="light" color="gray" size="lg">
                {runs.length}
              </Badge>
            )
          }
          noBodyPadding
        >
          {runsQuery.isLoading ? (
            <Box direction="column" gap={3} style={{ padding: 16 }}>
              {Array.from({ length: 4 }).map((_, i) => (
                <Skeleton key={i} height={40} />
              ))}
            </Box>
          ) : runsQuery.isError ? (
            <Text color="danger" size="sm" style={{ padding: 16 }}>
              {errorMessage(runsQuery.error, "Unable to load runs")}
            </Text>
          ) : runs.length === 0 ? (
            <EmptyState
              title="No runs yet"
              description='Use "Run now" to start the first run of this workflow.'
            />
          ) : (
            <Table hoverable>
              <Table.Header>
                <Table.Row>
                  <Table.Head>Status</Table.Head>
                  <Table.Head>Trigger</Table.Head>
                  <Table.Head>Started</Table.Head>
                </Table.Row>
              </Table.Header>
              <Table.Body>
                {runs.map((run) => (
                  <Table.Row
                    key={run.id}
                    selected={selectedId === run.id}
                    color={selectedId === run.id ? "blue" : undefined}
                    style={{ cursor: "pointer" }}
                    onClick={() => setSelectedId(run.id)}
                  >
                    <Table.Cell>
                      <StatusBadge status={run.status} />
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="sm">{run.trigger}</Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="xs" color="muted">
                        {formatDate(run.startedAt)}
                      </Text>
                    </Table.Cell>
                  </Table.Row>
                ))}
              </Table.Body>
            </Table>
          )}
        </SectionCard>
      </Box>

      <Box style={{ flex: 1, minWidth: 0 }}>
        {selectedId ? (
          <RunDetail workflowId={workflowId} runId={selectedId} />
        ) : (
          <SectionCard title="Run output">
            <EmptyState
              title="No run selected"
              description="Select a run from the history to see its per-step output."
            />
          </SectionCard>
        )}
      </Box>
    </Box>
  );
}

function RunDetail({
  workflowId,
  runId,
}: {
  workflowId: string;
  runId: string;
}) {
  const runQuery = useQuery({
    queryKey: ["workflow-run", workflowId, runId],
    queryFn: () => getWorkflowRun(workflowId, runId),
    refetchInterval: (query) =>
      (query.state.data as WorkflowRun | undefined)?.status === "running"
        ? 2000
        : false,
  });

  if (runQuery.isLoading) {
    return (
      <SectionCard title="Run output">
        <Box direction="column" gap={3}>
          <Skeleton height={32} width="50%" />
          <Skeleton height={80} />
          <Skeleton height={80} />
        </Box>
      </SectionCard>
    );
  }

  if (runQuery.isError || !runQuery.data) {
    return (
      <SectionCard title="Run output">
        <Text color="danger" size="sm">
          {errorMessage(runQuery.error, "Unable to load this run")}
        </Text>
      </SectionCard>
    );
  }

  const run = runQuery.data;

  return (
    <SectionCard
      title="Run output"
      actions={<StatusBadge status={run.status} size="md" />}
    >
      <Box direction="row" wrap gap={3} align="center">
        {run.status === "running" && <Spinner size="xs" />}
        <Text size="sm" color="muted">
          {run.trigger} · started {formatDate(run.startedAt)}
          {run.finishedAt && ` · finished ${formatDate(run.finishedAt)}`}
        </Text>
      </Box>

      {(run.steps ?? []).length === 0 ? (
        <EmptyState
          title="No step output yet"
          description="Steps will appear here as the run progresses."
        />
      ) : (
        <Box direction="column" gap={3}>
          {run.steps.map((s, i) => (
            <Card key={i} variant="raised">
              <Card.Body>
                <Box direction="column" gap={2}>
                  <Box direction="row" wrap gap={3} align="center">
                    <StatusBadge status={s.status} />
                    <Text weight="medium" size="sm">
                      {s.name || `Step ${i + 1}`}
                    </Text>
                    <Badge variant="outline" color="gray" size="sm">
                      {s.type}
                    </Badge>
                    {s.durationMs > 0 && (
                      <Text size="xs" color="muted">
                        {s.durationMs} ms
                      </Text>
                    )}
                  </Box>
                  {s.output && (
                    <Code block style={{ whiteSpace: "pre-wrap" }}>
                      {s.output}
                    </Code>
                  )}
                  {s.error && (
                    <Code block color="red" style={{ whiteSpace: "pre-wrap" }}>
                      {s.error}
                    </Code>
                  )}
                </Box>
              </Card.Body>
            </Card>
          ))}
        </Box>
      )}
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Automation tab: the public workflow run webhook URL (generate / rotate /
// remove + copy).
// ---------------------------------------------------------------------------

function AutomationTab({
  workflow,
  onChanged,
}: {
  workflow: Workflow;
  onChanged: () => void;
}) {
  const hasHook = !!workflow.runToken;
  const hookUrl = hasHook
    ? `${window.location.origin}/hooks/workflow/${workflow.runToken}`
    : "";

  const createMutation = useMutation({
    mutationFn: () => createWorkflowHook(workflow.id),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Webhook ready",
        message: hasHook
          ? "The run webhook was rotated"
          : "A run webhook was generated",
      });
      onChanged();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Webhook failed",
        message: errorMessage(err, "Could not generate the webhook"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteWorkflowHook(workflow.id),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Webhook removed",
        message: "The run webhook was cleared",
      });
      onChanged();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Remove failed",
        message: errorMessage(err, "Could not remove the webhook"),
      });
    },
  });

  const busy = createMutation.isPending || deleteMutation.isPending;

  return (
    <SectionCard
      title="Run webhook"
      description="A public, unauthenticated URL that triggers a run of this workflow when it receives a POST. Treat the URL as a secret — anyone who has it can trigger a run."
    >
      <Box direction="column" gap={4} maxWidth={760}>
        <Text size="sm" color="muted">
          Wire it into your CI to run the workflow automatically.
        </Text>

        {hasHook ? (
        <>
          <Box direction="row" wrap="nowrap" gap={2} align="center">
            <Code
              style={{
                flex: 1,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {hookUrl}
            </Code>
            <CopyUrlButton value={hookUrl} label="Copy webhook URL" />
          </Box>
          <Text size="xs" color="muted">
            Example: <Code>{`curl -X POST ${hookUrl || "<url>"}`}</Code>
          </Text>
          <Box direction="row" wrap gap={2}>
            <Button
              variant="light"
              iconLeft="refresh-rounded"
              loading={createMutation.isPending}
              disabled={busy}
              onClick={() => createMutation.mutate()}
            >
              Rotate
            </Button>
            <Button
              variant="light"
              color="danger"
              iconLeft="delete-outline-rounded"
              loading={deleteMutation.isPending}
              disabled={busy}
              onClick={() => deleteMutation.mutate()}
            >
              Remove
            </Button>
          </Box>
        </>
        ) : (
          <Box direction="row" wrap gap={3}>
            <Button
              color="info"
              iconLeft="refresh-rounded"
              loading={createMutation.isPending}
              disabled={busy}
              onClick={() => createMutation.mutate()}
            >
              Generate webhook
            </Button>
          </Box>
        )}
      </Box>
    </SectionCard>
  );
}
