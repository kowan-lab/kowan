import {
  Box,
  Button,
  Callout,
  Checkbox,
  Divider,
  InputToggle,
  NumberInput,
  RadioGroup,
  Radio,
  Select,
  Tabs,
  Text,
  TextInput,
} from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { connectionTypes, listConnections, listHosts } from "../api/client";
import type {
  ContainerSpec,
  Deployment,
  DeploymentStrategy,
  DeploymentType,
  GitSource,
} from "../types";
import {
  ContainerSpecForm,
  containerSpecFormFromSpec,
  containerSpecFormToSpec,
  emptyContainerSpecForm,
  type ContainerSpecFormValue,
} from "./ContainerSpecForm";
import { ComposeBuilder } from "./ComposeBuilder";
import type { ComposeDiagnostic } from "../types";
import { warningsSignature } from "./compose/useComposeValidation";
import FormSection from "./FormSection";
import EnvEditor, { envRowsFromApi, envRowsProblem, envRowsToApi, type EnvRow } from "./EnvEditor";

// Short, accurate explanations of each deployment type/strategy combination,
// shown beneath the type selector and updated with the current selection.
const TYPE_HINTS: Record<DeploymentType, string> = {
  container:
    "A single container, defined by an image + the spec below. The simplest option.",
  compose:
    "A multi-service stack from a docker-compose YAML, like “docker compose up” — multiple services/containers.",
};

const SWARM_HINT =
  "Swarm cluster: deploys to a Docker Swarm as a stack of services (“docker stack deploy”), orchestrated with replicas across the swarm. Point it at a single Swarm manager host.";

// ---------------------------------------------------------------------------
// DeploymentForm: the single, shared editor for a deployment's full
// configuration. It is used both by the full-screen create page (mode="create")
// and the detail page's Config tab (mode="edit"), so the two flows can never
// drift apart.
//
// The component is fully controlled via value/onChange. Use the helpers below
// to seed an empty value, hydrate from an existing Deployment, convert to the
// API DeploymentInput, and validate before submit.
// ---------------------------------------------------------------------------

/** Where a compose deployment's YAML comes from. */
export type ComposeSource = "inline" | "git";

/** Editable git source fields (webhookSecret is server-owned). */
export type GitFormValue = Omit<GitSource, "webhookSecret">;

export interface DeploymentFormValue {
  name: string;
  type: DeploymentType;
  strategy: DeploymentStrategy;
  targets: string[];
  replicas: number;
  spec: ContainerSpecFormValue;
  compose: string;
  source: ComposeSource;
  git: GitFormValue;
  /** Stack variables (compose): fill the compose's ${VAR}s on deploy. */
  variables: EnvRow[];
  /** Existing deployment id (edit flow): its own containers are not conflicts. */
  deploymentId?: string;
  /** Latest live-validation findings for the inline compose. */
  composeDiagnostics?: ComposeDiagnostic[];
  /** True while the current compose text has not been validated yet. */
  composePending?: boolean;
  /** Signature of the warning set the operator confirmed ("deploy anyway"). */
  composeAck?: string;
}

function emptyGitForm(): GitFormValue {
  return {
    repoUrl: "",
    ref: "main",
    path: "docker-compose.yml",
    credentialId: "",
    autoDeploy: true,
  };
}

/** The DeploymentInput shape the API expects (kept local to avoid a cycle). */
export interface DeploymentInputLike {
  name: string;
  type: DeploymentType;
  strategy: DeploymentStrategy;
  targets: string[];
  replicas: number;
  spec: ContainerSpec | null;
  compose: string;
  git: GitFormValue | null;
  variables: import("../types").KeyValue[];
}

/** A blank deployment form value, ready for the create flow. */
export function emptyDeploymentForm(): DeploymentFormValue {
  return {
    name: "",
    type: "container",
    strategy: "standalone",
    targets: [],
    replicas: 1,
    spec: emptyContainerSpecForm(),
    compose: "",
    source: "inline",
    git: emptyGitForm(),
    variables: [],
  };
}

/** Hydrate the form value from an existing deployment record (edit flow). */
export function deploymentFormFromDeployment(d: Deployment): DeploymentFormValue {
  return {
    name: d.name,
    type: d.type,
    strategy: d.strategy,
    targets: d.targets ?? [],
    replicas: d.replicas || 1,
    spec: containerSpecFormFromSpec(d.spec, d.name),
    compose: d.compose ?? "",
    source: d.git?.repoUrl ? "git" : "inline",
    variables: envRowsFromApi(d.variables),
    deploymentId: d.id,
    git: d.git
      ? {
          repoUrl: d.git.repoUrl,
          ref: d.git.ref || "main",
          path: d.git.path || "docker-compose.yml",
          credentialId: d.git.credentialId ?? "",
          autoDeploy: d.git.autoDeploy,
        }
      : emptyGitForm(),
  };
}

/** Convert the form value into the API DeploymentInput payload. */
export function deploymentFormToInput(f: DeploymentFormValue): DeploymentInputLike {
  let spec: ContainerSpec | null = null;
  if (f.type === "container") {
    spec = containerSpecFormToSpec(f.spec);
  }
  return {
    name: f.name.trim(),
    type: f.type,
    strategy: f.strategy,
    targets: f.targets,
    replicas: f.strategy === "swarm" ? f.replicas : 0,
    spec,
    compose: f.type === "compose" && f.source === "inline" ? f.compose : "",
    git:
      f.type === "compose" && f.source === "git"
        ? {
            repoUrl: f.git.repoUrl.trim(),
            ref: f.git.ref.trim() || "main",
            path: f.git.path.trim() || "docker-compose.yml",
            credentialId: f.git.credentialId,
            autoDeploy: f.git.autoDeploy,
          }
        : null,
    // Rows without a name are dropped, like the server does.
    variables: f.type === "compose" ? envRowsToApi(f.variables) : [],
  };
}

/** Return a human-readable validation error, or null when the form is valid. */
export function validateDeploymentForm(f: DeploymentFormValue): string | null {
  if (f.name.trim().length === 0) return "Name is required";
  const envProblem = f.type === "compose" ? envRowsProblem(f.variables, { strictNames: true }) : envRowsProblem(f.spec.env);
  if (envProblem) return envProblem;
  if (f.type === "container" && f.spec.image.trim().length === 0) {
    return "Image is required for container deployments";
  }
  if (f.type === "compose" && f.source === "git") {
    if (f.git.repoUrl.trim().length === 0) return "Repository URL is required";
    if (f.git.path.split("/").includes("..")) {
      return "Compose path must stay inside the repository";
    }
  } else if (f.type === "compose" && f.compose.trim().length === 0) {
    return "Compose YAML is required for compose deployments";
  } else if (f.type === "compose") {
    const diags = f.composeDiagnostics ?? [];
    const errors = diags.filter((d) => d.severity === "error").length;
    const warnings = diags.filter((d) => d.severity === "warning").length;
    if (f.composePending) return "Compose validation is still running, try again in a moment";
    if (errors > 0) {
      return `Fix the ${errors} compose error${errors === 1 ? "" : "s"} listed under the editor first`;
    }
    if (warnings > 0 && f.composeAck !== warningsSignature(diags)) {
      return `Review the ${warnings} compose warning${warnings === 1 ? "" : "s"} and confirm to continue`;
    }
  }
  if (f.strategy === "standalone" && f.targets.length !== 1) {
    return "Pick the host to deploy to";
  }
  if (f.strategy === "swarm" && f.targets.length !== 1) {
    return "Pick the Swarm manager host";
  }
  return null;
}

interface DeploymentFormProps {
  value: DeploymentFormValue;
  onChange: (value: DeploymentFormValue) => void;
  /**
   * "create" shows the name field (the record does not exist yet); "edit"
   * hides it because the detail page header already owns the name. Both modes
   * render the identical type/strategy/targets and per-type spec editor.
   */
  mode: "create" | "edit";
}

export function DeploymentForm({ value, onChange, mode }: DeploymentFormProps) {
  const set = <K extends keyof DeploymentFormValue>(
    key: K,
    v: DeploymentFormValue[K],
  ) => onChange({ ...value, [key]: v });

  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hostOptions = (hostsQuery.data ?? []).map((h) => ({
    value: h.id,
    label: h.name || h.id,
  }));

  return (
    <Box direction="column" gap={5}>
      <FormSection
        variant="plain"
        horizontal
        title="General"
        description="Name the deployment and choose what gets deployed and how it is orchestrated."
      >
        {mode === "create" && (
          <TextInput
            label="Name"
            placeholder="my-app"
            required
            data-autofocus
            value={value.name}
            onChange={(e) => set("name", e.target.value)}
          />
        )}

        <Box direction="row" wrap grow align="start" gap={4}>
          <Box grow direction="column" gap={2}>
            <Text size="sm" weight="medium">
              Type
            </Text>
            <RadioGroup
              orientation="horizontal"
              value={value.type}
              onChange={(v) => set("type", v as DeploymentType)}
            >
              <Radio value="container" label="Container" />
              <Radio value="compose" label="Compose" />
            </RadioGroup>
          </Box>
          <Box grow direction="column" gap={2}>
            <Text size="sm" weight="medium">
              Where it runs
            </Text>
            <RadioGroup
              orientation="horizontal"
              value={value.strategy}
              onChange={(v) => set("strategy", v as DeploymentStrategy)}
            >
              <Radio value="standalone" label="Single host" />
              <Radio value="swarm" label="Swarm cluster" />
            </RadioGroup>
          </Box>
        </Box>

        <Callout variant="light" color="blue">
          <Text size="sm">{TYPE_HINTS[value.type]}</Text>
          {value.strategy === "swarm" && (
            <Text size="sm" style={{ marginTop: 4 }}>
              {SWARM_HINT}
            </Text>
          )}
        </Callout>
      </FormSection>

      <Divider />

      <FormSection
        variant="plain"
        horizontal
        title="Host"
        description={
          value.strategy === "swarm"
            ? "The single Swarm manager that will run the stack."
            : "The host that will run this deployment. Its volumes and data live on that host."
        }
      >
        {value.strategy === "standalone" ? (
          <Select
            label="Host"
            description="The host to deploy to."
            placeholder="Select a host"
            options={hostOptions}
            value={value.targets[0] ?? null}
            onChange={(v) => set("targets", v ? [v] : [])}
            searchable
          />
        ) : (
          <Box direction="row" wrap grow align="start" gap={4}>
            <Select
              label="Manager host"
              description="The swarm manager to deploy the stack on."
              placeholder="Select a host"
              options={hostOptions}
              value={value.targets[0] ?? null}
              onChange={(v) => set("targets", v ? [v] : [])}
              searchable
            />
            <NumberInput
              label="Replicas"
              min={1}
              value={value.replicas}
              onChange={(v) =>
                set("replicas", typeof v === "number" ? v : Number(v) || 1)
              }
            />
          </Box>
        )}
      </FormSection>

      <Divider />

      {value.type === "container" ? (
        <ContainerSpecForm
          allowSecrets
          value={value.spec}
          onChange={(v) => set("spec", v)}
          hideName
        />
      ) : (
        <FormSection
          variant="plain"
          horizontal
          title="Compose"
          description="The docker-compose YAML defining the stack's services: written here, or read from a git repository on every deploy."
        >
          <Tabs
            variant="segmented"
            value={value.source}
            onValueChange={(v) => set("source", v as ComposeSource)}
          >
            <Tabs.List>
              <Tabs.Trigger value="inline">Inline YAML</Tabs.Trigger>
              <Tabs.Trigger value="git">Git repository</Tabs.Trigger>
            </Tabs.List>
            <Tabs.Panel value="inline">
              <ComposeBuilder
                value={value.compose}
                onChange={(yaml) => set("compose", yaml)}
                validation={{
                  hostId: value.targets[0],
                  deploymentId: value.deploymentId,
                  env: Object.fromEntries(value.variables.filter((v) => v.key.trim()).map((v) => [v.key.trim(), v.value])),
                  onDiagnostics: (d, pending) =>
                    onChange({ ...value, composeDiagnostics: d, composePending: pending }),
                }}
              />
              <ComposeWarningsAck value={value} onChange={onChange} />
            </Tabs.Panel>
            <Tabs.Panel value="git">
              <GitSourceFields
                value={value.git}
                onChange={(g) => set("git", g)}
              />
            </Tabs.Panel>
          </Tabs>
        </FormSection>
      )}

      {value.type === "compose" && (
        <>
          <Divider />
          <StackVariables value={value.variables} onChange={(v) => set("variables", v)} git={value.source === "git"} />
        </>
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// GitSourceFields: repo URL, ref, compose path, credential and auto-deploy for a
// git-sourced compose deployment. The credential picker lists stored connections
// that implement the gitFetch capability; listing them is admin-only, so for
// other roles (or on error) it falls back to a plain connection-id field.
// ---------------------------------------------------------------------------

const GIT_FETCH = "gitFetch";

function GitSourceFields({
  value,
  onChange,
}: {
  value: GitFormValue;
  onChange: (v: GitFormValue) => void;
}) {
  const set = <K extends keyof GitFormValue>(key: K, v: GitFormValue[K]) =>
    onChange({ ...value, [key]: v });

  const connsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
    retry: false,
  });
  const typesQuery = useQuery({
    queryKey: ["connection-types"],
    queryFn: connectionTypes,
    retry: false,
  });
  const gitTypes = new Set(
    (typesQuery.data ?? [])
      .filter((t) => t.capabilities.includes(GIT_FETCH))
      .map((t) => t.type),
  );
  const credentialOptions = [
    { value: "", label: "None (public repository)" },
    ...(connsQuery.data ?? [])
      .filter((c) => gitTypes.has(c.type) || c.id === value.credentialId)
      .map((c) => ({ value: c.id, label: `${c.name} · ${c.type}` })),
  ];
  const canPick = !connsQuery.isError && !typesQuery.isError;

  return (
    <Box direction="column" gap={4}>
      <TextInput
        label="Repository URL"
        placeholder="https://github.com/owner/repo.git"
        description="HTTPS (or SSH) clone URL. A shallow clone is made on the server for every deploy."
        required
        value={value.repoUrl}
        onChange={(e) => set("repoUrl", e.target.value)}
      />
      <Box direction="row" wrap grow align="start" gap={4}>
        <TextInput
          label="Branch or tag"
          placeholder="main"
          value={value.ref}
          onChange={(e) => set("ref", e.target.value)}
        />
        <TextInput
          label="Compose file path"
          placeholder="docker-compose.yml"
          description="Relative to the repository root. A .env next to it is applied."
          value={value.path}
          onChange={(e) => set("path", e.target.value)}
        />
      </Box>
      {canPick ? (
        <Select
          label="Credential"
          description="A stored git connection (GitHub, GitLab, Bitbucket, git-http) for private repositories."
          options={credentialOptions}
          value={value.credentialId}
          onChange={(v) => set("credentialId", v ?? "")}
          searchable
        />
      ) : (
        <TextInput
          label="Credential ID"
          description="Connection id of a stored git credential, for private repositories. Leave empty for a public repository."
          value={value.credentialId}
          onChange={(e) => set("credentialId", e.target.value.trim())}
        />
      )}
      <InputToggle
        label="Auto deploy on push"
        description="Redeploy when the repository's push webhook reports a commit on this ref that touches the compose file's folder. The webhook URL and secret appear on the deployment page."
        checked={value.autoDeploy}
        onChange={(e) => set("autoDeploy", e.currentTarget.checked)}
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// ComposeWarningsAck: warnings never block a deploy, but saving one asks for
// an explicit confirmation, scoped to that exact warning set (a new warning
// asks again). Errors block in validateDeploymentForm.
// ---------------------------------------------------------------------------

function ComposeWarningsAck({
  value,
  onChange,
}: {
  value: DeploymentFormValue;
  onChange: (v: DeploymentFormValue) => void;
}) {
  const diags = value.composeDiagnostics ?? [];
  const warnings = diags.filter((d) => d.severity === "warning").length;
  const errors = diags.filter((d) => d.severity === "error").length;
  if (warnings === 0 || errors > 0 || value.composePending) return null;
  const sig = warningsSignature(diags);
  return (
    <Box style={{ marginTop: 8 }}>
      <Checkbox
        label={`Deploy with ${warnings} warning${warnings === 1 ? "" : "s"}`}
        description="The warnings above don't stop the deploy; confirm you reviewed them."
        checked={value.composeAck === sig}
        onChange={(e) => onChange({ ...value, composeAck: e.currentTarget.checked ? sig : "" })}
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// StackVariables: the compose deployment's own variables (Portainer's stack
// env). They fill the compose's ${VAR}s when it deploys, over a git repo's .env.
// ---------------------------------------------------------------------------

function StackVariables({ value, onChange, git }: { value: EnvRow[]; onChange: (v: EnvRow[]) => void; git: boolean }) {
  return (
    <FormSection
      variant="plain"
      horizontal
      title="Stack variables"
      description={`Fill the compose's \${VAR}s when it deploys${git ? ", over the repository's .env" : ""}. Mark a value Secret to keep it encrypted, or reference a shared secret with [[name.field]].`}
      action={
        <Button size="sm" variant="light" iconLeft="add-rounded" onClick={() => onChange([...value, { key: "", value: "" }])}>
          Add variable
        </Button>
      }
    >
      <EnvEditor value={value} onChange={onChange} strictNames emptyText="No stack variables." />
    </FormSection>
  );
}
