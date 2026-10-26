import {
  Box,
  Button,
  Checkbox,
  Divider,
  Icon,
  InputToggle,
  NumberInput,
  Select,
  Tabs,
  TagsInput,
  Text,
  TextArea,
  TextInput,
} from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { listRegistries } from "../api/client";
import FormSection from "./FormSection";
import ImageInput from "./ImageInput";
import { useAuth } from "../auth/AuthContext";
import { joinWords, splitWords } from "../lib/shellwords";
import type {
  ContainerSpec,
  KeyValue,
  PortMapping,
  PortProtocol,
  RestartPolicy,
  VolumeMapping,
} from "../types";

// ---------------------------------------------------------------------------
// Editable form state for a container spec. Kept separate from the API model
// so free-text fields (command, entrypoint, extraArgs) stay editable as raw
// strings while the user types. Use the helpers below to convert to/from the
// ContainerSpec the API expects.
// ---------------------------------------------------------------------------

export interface ContainerSpecFormValue {
  name: string;
  image: string;
  env: KeyValue[];
  ports: PortMapping[];
  volumes: VolumeMapping[];
  networks: string[];
  restartPolicy: RestartPolicy;
  command: string;
  // Advanced
  entrypoint: string;
  workingDir: string;
  user: string;
  cpuLimit: number;
  memoryLimitMB: number;
  memoryReservationMB: number;
  privileged: boolean;
  gpus: string;
  tty: boolean;
  openStdin: boolean;
  pullImage: boolean;
  extraArgs: string;
  /**
   * Optional stored registry credential to authenticate a private pull. This is
   * a UI-only field: it is sent alongside the spec on the create request rather
   * than embedded in the ContainerSpec itself.
   */
  registryId: string;
}

const RESTART_POLICIES: { value: RestartPolicy; label: string }[] = [
  { value: "", label: "(default)" },
  { value: "no", label: "no" },
  { value: "on-failure", label: "on-failure" },
  { value: "always", label: "always" },
  { value: "unless-stopped", label: "unless-stopped" },
];

/** A blank container spec form. */
export function emptyContainerSpecForm(): ContainerSpecFormValue {
  return {
    name: "",
    image: "",
    env: [],
    ports: [],
    volumes: [],
    networks: [],
    restartPolicy: "",
    command: "",
    entrypoint: "",
    workingDir: "",
    user: "",
    cpuLimit: 0,
    memoryLimitMB: 0,
    memoryReservationMB: 0,
    privileged: false,
    gpus: "",
    tty: false,
    openStdin: false,
    pullImage: false,
    extraArgs: "",
    registryId: "",
  };
}

/** Build form state from an existing ContainerSpec (and optional name). */
export function containerSpecFormFromSpec(
  spec: ContainerSpec | null | undefined,
  name = "",
): ContainerSpecFormValue {
  const base = emptyContainerSpecForm();
  if (!spec) return { ...base, name };
  return {
    ...base,
    name,
    image: spec.image ?? "",
    env: spec.env ?? [],
    ports: spec.ports ?? [],
    volumes: spec.volumes ?? [],
    networks: spec.networks ?? [],
    restartPolicy: spec.restartPolicy ?? "",
    command: joinWords(spec.command ?? []),
    entrypoint: joinWords(spec.entrypoint ?? []),
    workingDir: spec.workingDir ?? "",
    user: spec.user ?? "",
    cpuLimit: spec.cpuLimit ?? 0,
    memoryLimitMB: spec.memoryLimitMB ?? 0,
    memoryReservationMB: spec.memoryReservationMB ?? 0,
    privileged: spec.privileged ?? false,
    gpus: spec.gpus ?? "",
    tty: spec.tty ?? false,
    openStdin: spec.openStdin ?? false,
    pullImage: spec.pullImage ?? false,
    extraArgs: (spec.extraArgs ?? []).join("\n"),
  };
}

/**
 * Validate a container spec form before submitting it to the API. Returns a
 * human-friendly error message for the first problem found, or null when the
 * form is valid. This prevents submitting port rows with containerPort=0 (the
 * NumberInput default), which Docker rejects with an opaque "invalid port
 * specification" error surfaced to the user as a 502.
 */
export function validateContainerSpecForm(f: ContainerSpecFormValue): string | null {
  if (f.image.trim().length === 0) {
    return "Image is required";
  }
  for (const p of f.ports) {
    if (!p.containerPort || p.containerPort < 1 || p.containerPort > 65535) {
      return "Each port mapping needs a container port between 1 and 65535";
    }
    if (p.hostPort < 0 || p.hostPort > 65535) {
      return "Host port must be between 0 and 65535 (use 0 for a random host port)";
    }
  }
  return null;
}


/** Convert editable form state into the ContainerSpec the API expects. */
export function containerSpecFormToSpec(f: ContainerSpecFormValue): ContainerSpec {
  const entrypoint = splitWords(f.entrypoint);
  const extraArgs = f.extraArgs
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  return {
    image: f.image.trim(),
    env: f.env,
    ports: f.ports,
    volumes: f.volumes,
    networks: f.networks,
    restartPolicy: f.restartPolicy,
    command: splitWords(f.command),
    labels: {},
    entrypoint: entrypoint.length > 0 ? entrypoint : undefined,
    workingDir: f.workingDir.trim() || undefined,
    user: f.user.trim() || undefined,
    cpuLimit: f.cpuLimit > 0 ? f.cpuLimit : undefined,
    memoryLimitMB: f.memoryLimitMB > 0 ? f.memoryLimitMB : undefined,
    memoryReservationMB:
      f.memoryReservationMB > 0 ? f.memoryReservationMB : undefined,
    privileged: f.privileged || undefined,
    gpus: f.gpus.trim() || undefined,
    tty: f.tty || undefined,
    openStdin: f.openStdin || undefined,
    pullImage: f.pullImage || undefined,
    extraArgs: extraArgs.length > 0 ? extraArgs : undefined,
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ContainerSpecFormProps {
  value: ContainerSpecFormValue;
  onChange: (value: ContainerSpecFormValue) => void;
  /** Hide the name field (e.g. when the parent owns naming). Defaults false. */
  hideName?: boolean;
}

export function ContainerSpecForm({
  value,
  onChange,
  hideName = false,
}: ContainerSpecFormProps) {
  const set = <K extends keyof ContainerSpecFormValue>(
    key: K,
    v: ContainerSpecFormValue[K],
  ) => onChange({ ...value, [key]: v });

  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  // Registries are admin-only; non-admins never see the Select.
  const registriesQuery = useQuery({
    queryKey: ["registries"],
    queryFn: listRegistries,
    enabled: isAdmin,
  });

  const registryOptions = (registriesQuery.data ?? []).map((registry) => ({
    value: registry.id,
    label: `${registry.name} (${registry.url})`,
  }));

  // Env ----------------------------------------------------------------------
  const addEnv = () => set("env", [...value.env, { key: "", value: "" }]);
  const updateEnv = (i: number, patch: Partial<KeyValue>) =>
    set(
      "env",
      value.env.map((row, idx) => (idx === i ? { ...row, ...patch } : row)),
    );
  const removeEnv = (i: number) =>
    set(
      "env",
      value.env.filter((_, idx) => idx !== i),
    );

  // Ports --------------------------------------------------------------------
  const addPort = () =>
    set("ports", [
      ...value.ports,
      { hostPort: 0, containerPort: 0, protocol: "tcp" },
    ]);
  const updatePort = (i: number, patch: Partial<PortMapping>) =>
    set(
      "ports",
      value.ports.map((row, idx) => (idx === i ? { ...row, ...patch } : row)),
    );
  const removePort = (i: number) =>
    set(
      "ports",
      value.ports.filter((_, idx) => idx !== i),
    );

  // Volumes ------------------------------------------------------------------
  const addVolume = () =>
    set("volumes", [
      ...value.volumes,
      { source: "", target: "", readOnly: false },
    ]);
  const updateVolume = (i: number, patch: Partial<VolumeMapping>) =>
    set(
      "volumes",
      value.volumes.map((row, idx) => (idx === i ? { ...row, ...patch } : row)),
    );
  const removeVolume = (i: number) =>
    set(
      "volumes",
      value.volumes.filter((_, idx) => idx !== i),
    );

  const portsList = (
    <Box direction="column" gap={2}>
      {value.ports.length === 0 && (
        <Text size="xs" color="muted">
          No port mappings.
        </Text>
      )}
      {value.ports.map((row, i) => (
        <Box key={i} direction="row" wrap="nowrap" gap={2} align="end">
          <NumberInput
            placeholder="host (0 = random)"
            min={0}
            max={65535}
            value={row.hostPort}
            onChange={(v) =>
              updatePort(i, {
                hostPort: typeof v === "number" ? v : Number(v) || 0,
              })
            }
            style={{ flex: 1 }}
          />
          <NumberInput
            placeholder="container"
            min={1}
            max={65535}
            errorText={
              !row.containerPort || row.containerPort < 1 ? "required" : undefined
            }
            value={row.containerPort || ""}
            onChange={(v) =>
              updatePort(i, {
                containerPort: typeof v === "number" ? v : Number(v) || 0,
              })
            }
            style={{ flex: 1 }}
          />
          <Select
            aria-label="Protocol"
            width={90}
            options={[
              { value: "tcp", label: "tcp" },
              { value: "udp", label: "udp" },
            ]}
            value={row.protocol}
            onChange={(v) =>
              updatePort(i, { protocol: (v as PortProtocol) ?? "tcp" })
            }
          />
          <Button
            variant="clear"
            color="danger"
            aria-label="Remove port"
            iconLeft="delete-outline-rounded"
            onClick={() => removePort(i)}
          />
        </Box>
      ))}
    </Box>
  );

  // A small right-aligned "Add" toolbar for the list editors inside the tabs.
  const addBar = (label: string, onAdd: () => void) => (
    <Box direction="row" justify="end">
      <Button
        size="sm"
        variant="light"
        iconLeft="add-rounded"
        onClick={onAdd}
      >
        {label}
      </Button>
    </Box>
  );

  return (
    <Box direction="column" gap={5}>
      {/* Primary fields — the essentials, always visible up front. */}
      <FormSection
        variant="plain"
        horizontal
        title="Image"
        description="The image to run, and an optional stored credential for private registries."
      >
        {!hideName && (
          <TextInput
            label="Name"
            description="Optional. Docker assigns a random name when empty."
            placeholder="my-container"
            value={value.name}
            onChange={(e) => set("name", e.target.value)}
          />
        )}
        <ImageInput value={value.image} onChange={(v) => set("image", v)} />
        {isAdmin && registryOptions.length > 0 && (
          <Select
            label="Registry"
            description="Optional. Use a stored credential for a private pull."
            placeholder="Anonymous / public"
            options={registryOptions}
            clearable
            value={value.registryId || undefined}
            onChange={(v) => set("registryId", v ?? "")}
          />
        )}
      </FormSection>

      <Divider />

      <FormSection
        variant="plain"
        horizontal
        title="Network ports"
        description="Host port 0 publishes on a random host port; the container port is required."
        action={
          <Button
            size="sm"
            variant="light"
            iconLeft="add-rounded"
            onClick={addPort}
          >
            Map additional port
          </Button>
        }
      >
        {portsList}
      </FormSection>

      <Divider />

      {/* Advanced container settings — everything else, grouped into tabs. */}
      <Box direction="column" gap={3}>
        <Box direction="row" align="center" gap={2}>
          <Icon name="settings-outline-rounded" size="sm" />
          <Text size="sm" weight="semibold">
            Advanced container settings
          </Text>
        </Box>

        <Tabs defaultValue="runtime">
          <Tabs.List>
            <Tabs.Trigger value="runtime">Command &amp; runtime</Tabs.Trigger>
            <Tabs.Trigger value="volumes">Volumes</Tabs.Trigger>
            <Tabs.Trigger value="env">Environment</Tabs.Trigger>
            <Tabs.Trigger value="networks">Networks</Tabs.Trigger>
            <Tabs.Trigger value="resources">Resources</Tabs.Trigger>
            <Tabs.Trigger value="extra">Extra args</Tabs.Trigger>
          </Tabs.List>

          <Tabs.Panel value="runtime">
            <Box direction="column" gap={3}>
              <Select
                label="Restart policy"
                options={RESTART_POLICIES}
                value={value.restartPolicy}
                onChange={(v) => set("restartPolicy", (v as RestartPolicy) ?? "")}
              />
              <TextInput
                label="Command"
                description="Optional override, space-separated."
                placeholder="nginx -g 'daemon off;'"
                value={value.command}
                onChange={(e) => set("command", e.target.value)}
              />
              <TextInput
                label="Entrypoint"
                description="Override the image entrypoint, space-separated."
                placeholder="/bin/sh -c"
                value={value.entrypoint}
                onChange={(e) => set("entrypoint", e.target.value)}
              />
              <Box direction="row" wrap align="start" gap={2}>
                <TextInput
                  label="Working directory"
                  placeholder="/app"
                  value={value.workingDir}
                  onChange={(e) => set("workingDir", e.target.value)}
                  style={{ flex: 1 }}
                />
                <TextInput
                  label="User"
                  placeholder="1000:1000 or appuser"
                  value={value.user}
                  onChange={(e) => set("user", e.target.value)}
                  style={{ flex: 1 }}
                />
              </Box>
              <Box direction="row" wrap gap={2}>
                <InputToggle
                  label="Privileged"
                  checked={value.privileged}
                  onChange={(e) => set("privileged", e.target.checked)}
                />
                <InputToggle
                  label="TTY"
                  checked={value.tty}
                  onChange={(e) => set("tty", e.target.checked)}
                />
                <InputToggle
                  label="Open stdin"
                  checked={value.openStdin}
                  onChange={(e) => set("openStdin", e.target.checked)}
                />
                <InputToggle
                  label="Pull image before create"
                  checked={value.pullImage}
                  onChange={(e) => set("pullImage", e.target.checked)}
                />
              </Box>
            </Box>
          </Tabs.Panel>

          <Tabs.Panel value="volumes">
            <Box direction="column" gap={2}>
              {addBar("Add volume", addVolume)}
              {value.volumes.length === 0 && (
                <Text size="xs" color="muted">
                  No volume mappings.
                </Text>
              )}
              {value.volumes.map((row, i) => (
                <Box key={i} direction="row" wrap="nowrap" gap={2} align="end">
                  <TextInput
                    placeholder="source / named volume"
                    value={row.source}
                    onChange={(e) => updateVolume(i, { source: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <TextInput
                    placeholder="/container/path"
                    value={row.target}
                    onChange={(e) => updateVolume(i, { target: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <Checkbox
                    label="ro"
                    checked={row.readOnly}
                    onChange={(e) =>
                      updateVolume(i, { readOnly: e.target.checked })
                    }
                  />
                  <Button
                    variant="clear"
                    color="danger"
                    aria-label="Remove volume"
                    iconLeft="delete-outline-rounded"
                    onClick={() => removeVolume(i)}
                  />
                </Box>
              ))}
            </Box>
          </Tabs.Panel>

          <Tabs.Panel value="env">
            <Box direction="column" gap={2}>
              {addBar("Add variable", addEnv)}
              <Text size="xs" color="muted">
                Reference a secret with [[name]] or [[name.field]].
              </Text>
              {value.env.length === 0 && (
                <Text size="xs" color="muted">
                  No environment variables.
                </Text>
              )}
              {value.env.map((row, i) => (
                <Box key={i} direction="row" wrap="nowrap" gap={2} align="end">
                  <TextInput
                    placeholder="KEY"
                    value={row.key}
                    onChange={(e) => updateEnv(i, { key: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <TextInput
                    placeholder="value"
                    value={row.value}
                    onChange={(e) => updateEnv(i, { value: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <Button
                    variant="clear"
                    color="danger"
                    aria-label="Remove env"
                    iconLeft="delete-outline-rounded"
                    onClick={() => removeEnv(i)}
                  />
                </Box>
              ))}
            </Box>
          </Tabs.Panel>

          <Tabs.Panel value="networks">
            <TagsInput
              label="Networks"
              description="Extra networks to attach. Press Enter to add."
              placeholder="network name"
              value={value.networks}
              onChange={(v) => set("networks", v)}
            />
          </Tabs.Panel>

          <Tabs.Panel value="resources">
            <Box direction="column" gap={3}>
              <Box direction="row" wrap align="start" gap={2}>
                <NumberInput
                  label="CPU limit (cores)"
                  description="0 = unlimited"
                  min={0}
                  step={0.5}
                  precision={2}
                  value={value.cpuLimit}
                  onChange={(v) =>
                    set("cpuLimit", typeof v === "number" ? v : Number(v) || 0)
                  }
                  style={{ flex: 1 }}
                />
                <NumberInput
                  label="Memory limit (MB)"
                  description="0 = unlimited"
                  min={0}
                  value={value.memoryLimitMB}
                  onChange={(v) =>
                    set(
                      "memoryLimitMB",
                      typeof v === "number" ? v : Number(v) || 0,
                    )
                  }
                  style={{ flex: 1 }}
                />
                <NumberInput
                  label="Memory reservation (MB)"
                  description="0 = none"
                  min={0}
                  value={value.memoryReservationMB}
                  onChange={(v) =>
                    set(
                      "memoryReservationMB",
                      typeof v === "number" ? v : Number(v) || 0,
                    )
                  }
                  style={{ flex: 1 }}
                />
              </Box>
              <TextInput
                label="GPUs"
                description={`"all", a count like "2", or empty for none.`}
                placeholder="all"
                value={value.gpus}
                onChange={(e) => set("gpus", e.target.value)}
              />
            </Box>
          </Tabs.Panel>

          <Tabs.Panel value="extra">
            <TextArea
              label="Extra docker run arguments"
              description="Escape hatch: one argument per line. When set, the container is created via docker run with the core flags translated."
              placeholder={"--cap-add=NET_ADMIN\n--sysctl=net.ipv4.ip_forward=1"}
              rows={3}
              style={{ fontFamily: "monospace" }}
              value={value.extraArgs}
              onChange={(e) => set("extraArgs", e.target.value)}
            />
          </Tabs.Panel>
        </Tabs>
      </Box>
    </Box>
  );
}
