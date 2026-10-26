import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import {
  Box,
  Button,
  Callout,
  Card,
  Icon,
  Select,
  TagsInput,
  Tabs,
  Text,
  TextInput,
  Skeleton,
} from "@/ui/kit";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

import ImageInput from "./ImageInput";

import type { EditorJump } from "./YamlEditor";
import type { ComposeDiagnostic } from "../types";
import { ComposeDiagnostics } from "./compose/ComposeDiagnostics";
import { applyComposePatch } from "./compose/applyPatch";
import { joinWords, splitWords } from "../lib/shellwords";
import { useComposeValidation, type ComposeValidationTarget } from "./compose/useComposeValidation";

const YamlEditor = lazy(() => import("./YamlEditor"));

// ---------------------------------------------------------------------------
// ComposeBuilder: an "Easy" per-service compose builder that can be toggled to
// a raw YAML editor ("YAML", advanced). The component is fully controlled via
// value/onChange — value is the compose YAML string.
//
// Easy mode is intentionally limited to the handful of per-service fields it
// can faithfully round-trip. If the YAML uses anything the easy model cannot
// represent, easy editing is disabled and a warning points the user to the
// YAML editor — we never clobber YAML we cannot fully understand.
// ---------------------------------------------------------------------------

type Mode = "easy" | "yaml";

interface PortRow {
  host: string;
  container: string;
  proto: string; // "tcp" | "udp"
}

interface EnvRow {
  key: string;
  value: string;
}

interface VolumeRow {
  source: string;
  target: string;
  readOnly: boolean;
}

interface ServiceModel {
  name: string;
  image: string;
  ports: PortRow[];
  env: EnvRow[];
  volumes: VolumeRow[];
  restart: string;
  command: string;
  dependsOn: string[];
}

const RESTART_OPTIONS = [
  { value: "", label: "(default)" },
  { value: "no", label: "no" },
  { value: "on-failure", label: "on-failure" },
  { value: "always", label: "always" },
  { value: "unless-stopped", label: "unless-stopped" },
];

// The only per-service keys the easy editor understands. Any other key means
// the service uses features the easy model cannot represent.
const KNOWN_SERVICE_KEYS = new Set([
  "image",
  "ports",
  "environment",
  "volumes",
  "restart",
  "command",
  "depends_on",
]);

function emptyService(name: string): ServiceModel {
  return {
    name,
    image: "",
    ports: [],
    env: [],
    volumes: [],
    restart: "",
    command: "",
    dependsOn: [],
  };
}

interface ParseResult {
  services: ServiceModel[];
  /** True when the YAML uses features the easy editor cannot represent. */
  unrepresentable: boolean;
  /**
   * Tolerated top-level `version` key, preserved verbatim so the first easy
   * edit doesn't silently drop it. `undefined` when the source had no version.
   */
  version?: unknown;
}

// Prefix used to serialise a service whose name is (temporarily) empty. Compose
// map keys must be unique and non-empty, so in-progress renames are emitted
// under a stable placeholder key and stripped back to an empty name on parse.
const UNNAMED_PREFIX = "__port_unnamed_";

function unnamedKey(index: number): string {
  return `${UNNAMED_PREFIX}${index}`;
}

function isUnnamedKey(key: string): boolean {
  return key.startsWith(UNNAMED_PREFIX);
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Parse a single compose port entry into a row, or null if unrepresentable. */
function parsePort(entry: unknown): PortRow | null {
  // Long-form mapping (e.g. { target, published, protocol }) is not supported.
  if (typeof entry === "number") {
    return { host: "", container: String(entry), proto: "tcp" };
  }
  if (typeof entry !== "string") return null;
  let proto = "tcp";
  let spec = entry;
  const slash = spec.lastIndexOf("/");
  if (slash !== -1) {
    proto = spec.slice(slash + 1) || "tcp";
    spec = spec.slice(0, slash);
  }
  const parts = spec.split(":");
  // Host-IP forms (ip:host:container) cannot be represented.
  if (parts.length === 1) {
    return { host: "", container: parts[0], proto };
  }
  if (parts.length === 2) {
    return { host: parts[0], container: parts[1], proto };
  }
  return null;
}

/** Parse a single compose volume entry into a row, or null if unrepresentable. */
function parseVolume(entry: unknown): VolumeRow | null {
  // Long-form mapping volumes are not supported by the easy editor.
  if (typeof entry !== "string") return null;
  const parts = entry.split(":");
  if (parts.length === 1) {
    // Anonymous volume (target only).
    return { source: "", target: parts[0], readOnly: false };
  }
  if (parts.length === 2) {
    return { source: parts[0], target: parts[1], readOnly: false };
  }
  if (parts.length === 3) {
    if (parts[2] !== "ro" && parts[2] !== "rw") return null;
    return { source: parts[0], target: parts[1], readOnly: parts[2] === "ro" };
  }
  return null;
}

/** Parse the `environment` field (map or list of KEY=VALUE) into rows. */
function parseEnv(env: unknown): EnvRow[] | null {
  if (isPlainObject(env)) {
    return Object.entries(env).map(([key, value]) => ({
      key,
      value: value === null || value === undefined ? "" : String(value),
    }));
  }
  if (Array.isArray(env)) {
    const rows: EnvRow[] = [];
    for (const item of env) {
      if (typeof item !== "string") return null;
      const eq = item.indexOf("=");
      if (eq === -1) {
        rows.push({ key: item, value: "" });
      } else {
        rows.push({ key: item.slice(0, eq), value: item.slice(eq + 1) });
      }
    }
    return rows;
  }
  return null;
}

/** Parse a `command` value (string or string list) into a single string. */
function parseCommand(cmd: unknown): string | null {
  if (typeof cmd === "string") return cmd;
  if (Array.isArray(cmd)) {
    if (cmd.some((c) => typeof c !== "string")) return null;
    // Quote tokens so splitWords gives the same list back: joining
    // ["nginx", "-g", "daemon off;"] bare would come back as four words on
    // the next easy-mode edit and break the command.
    return joinWords(cmd as string[]);
  }
  return null;
}

/** Parse a `depends_on` value (string list or condition map) into names. */
function parseDependsOn(dep: unknown): string[] | null {
  if (Array.isArray(dep)) {
    if (dep.some((d) => typeof d !== "string")) return null;
    return dep as string[];
  }
  // The long-form condition map (service: { condition }) is not representable.
  return null;
}

/**
 * Best-effort parse of compose YAML into the simple per-service model. If the
 * YAML cannot be fully represented, `unrepresentable` is true and `services`
 * is empty so the caller can warn instead of editing.
 */
function parseCompose(value: string): ParseResult {
  const empty: ParseResult = { services: [], unrepresentable: false };
  const text = value.trim();
  if (text.length === 0) return empty;

  let doc: unknown;
  try {
    doc = parseYaml(text);
  } catch {
    return { services: [], unrepresentable: true };
  }

  if (doc === null || doc === undefined) return empty;
  if (!isPlainObject(doc)) return { services: [], unrepresentable: true };

  const version = doc.version;

  // Only a bare `services` map is representable. A `version` key is benign and
  // tolerated; anything else (networks, volumes, configs, secrets, ...) is not.
  for (const key of Object.keys(doc)) {
    if (key !== "services" && key !== "version") {
      return { services: [], unrepresentable: true };
    }
  }

  const servicesRaw = doc.services;
  if (servicesRaw === undefined) return { ...empty, version };
  if (!isPlainObject(servicesRaw)) return { services: [], unrepresentable: true };

  const services: ServiceModel[] = [];
  for (const [name, raw] of Object.entries(servicesRaw)) {
    if (!isPlainObject(raw)) return { services: [], unrepresentable: true };

    for (const key of Object.keys(raw)) {
      if (!KNOWN_SERVICE_KEYS.has(key)) {
        return { services: [], unrepresentable: true };
      }
    }

    // A placeholder key represents a service mid-rename with an empty name.
    const svc = emptyService(isUnnamedKey(name) ? "" : name);

    if (raw.image !== undefined) {
      if (typeof raw.image !== "string") {
        return { services: [], unrepresentable: true };
      }
      svc.image = raw.image;
    }

    if (raw.ports !== undefined) {
      if (!Array.isArray(raw.ports)) {
        return { services: [], unrepresentable: true };
      }
      const rows: PortRow[] = [];
      for (const p of raw.ports) {
        const row = parsePort(p);
        if (row === null) return { services: [], unrepresentable: true };
        rows.push(row);
      }
      svc.ports = rows;
    }

    if (raw.environment !== undefined) {
      const rows = parseEnv(raw.environment);
      if (rows === null) return { services: [], unrepresentable: true };
      svc.env = rows;
    }

    if (raw.volumes !== undefined) {
      if (!Array.isArray(raw.volumes)) {
        return { services: [], unrepresentable: true };
      }
      const rows: VolumeRow[] = [];
      for (const v of raw.volumes) {
        const row = parseVolume(v);
        if (row === null) return { services: [], unrepresentable: true };
        rows.push(row);
      }
      svc.volumes = rows;
    }

    if (raw.restart !== undefined) {
      if (typeof raw.restart !== "string") {
        return { services: [], unrepresentable: true };
      }
      svc.restart = raw.restart;
    }

    if (raw.command !== undefined) {
      const cmd = parseCommand(raw.command);
      if (cmd === null) return { services: [], unrepresentable: true };
      svc.command = cmd;
    }

    if (raw.depends_on !== undefined) {
      const dep = parseDependsOn(raw.depends_on);
      if (dep === null) return { services: [], unrepresentable: true };
      svc.dependsOn = dep;
    }

    services.push(svc);
  }

  return { services, unrepresentable: false, version };
}

/** Serialise one port row back into compose short syntax. */
function portToString(p: PortRow): string {
  const proto = p.proto && p.proto !== "tcp" ? `/${p.proto}` : "";
  const base = p.host.trim()
    ? `${p.host.trim()}:${p.container.trim()}`
    : p.container.trim();
  return `${base}${proto}`;
}

/** Serialise one volume row back into compose short syntax. */
function volumeToString(v: VolumeRow): string {
  const source = v.source.trim();
  const target = v.target.trim();
  const base = source ? `${source}:${target}` : target;
  return v.readOnly ? `${base}:ro` : base;
}

/** Rebuild a compose YAML string from the simple per-service model. */
function buildCompose(services: ServiceModel[], version?: unknown): string {
  const out: Record<string, Record<string, unknown>> = {};
  services.forEach((svc, index) => {
    const trimmed = svc.name.trim();
    // A service mid-rename (empty name) must not be dropped — only the explicit
    // remove button removes services. Serialise it under a stable placeholder
    // key that parseCompose maps back to an empty name.
    const key = trimmed.length === 0 ? unnamedKey(index) : trimmed;
    const body: Record<string, unknown> = {};
    if (svc.image.trim()) body.image = svc.image.trim();
    const ports = svc.ports
      .filter((p) => p.container.trim().length > 0)
      .map(portToString);
    if (ports.length > 0) body.ports = ports;
    const env = svc.env.filter((e) => e.key.trim().length > 0);
    if (env.length > 0) {
      const map: Record<string, string> = {};
      for (const e of env) map[e.key.trim()] = e.value;
      body.environment = map;
    }
    const volumes = svc.volumes
      .filter((v) => v.target.trim().length > 0)
      .map(volumeToString);
    if (volumes.length > 0) body.volumes = volumes;
    if (svc.restart.trim()) body.restart = svc.restart.trim();
    const command = splitWords(svc.command);
    if (command.length > 0) body.command = command;
    const dependsOn = svc.dependsOn.filter((d) => d.trim().length > 0);
    if (dependsOn.length > 0) body.depends_on = dependsOn;
    out[key] = body;
  });
  // Re-emit a tolerated top-level `version` key so it round-trips through an
  // easy-mode edit instead of being silently dropped.
  const doc: Record<string, unknown> = {};
  if (version !== undefined) doc.version = version;
  doc.services = out;
  return stringifyYaml(doc);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ComposeBuilderProps {
  value: string;
  onChange: (yaml: string) => void;
  /**
   * Live validation (POST /compose/validate) for this target: a diagnostics
   * list under the editor, inline marks in YAML mode, jump-to-line and
   * "Apply fix". Omit to disable.
   */
  validation?: ComposeValidationTarget & {
    onDiagnostics?: (diagnostics: ComposeDiagnostic[], pending: boolean) => void;
  };
}

export function ComposeBuilder({ value, onChange, validation }: ComposeBuilderProps) {
  const [mode, setMode] = useState<Mode>("easy");
  const check = useComposeValidation(value, validation ?? {}, { enabled: validation !== undefined });
  const [jump, setJump] = useState<EditorJump | null>(null);
  const onDiagnostics = validation?.onDiagnostics;
  const pending = check.loading || check.validatedText !== value;
  useEffect(() => {
    onDiagnostics?.(check.diagnostics, pending);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [check.diagnostics, pending]);
  const jumpTo = (d: ComposeDiagnostic) => {
    setMode("yaml");
    setJump({ line: d.line, column: d.column, nonce: Date.now() });
  };
  const applyFix = (d: ComposeDiagnostic) => {
    if (!d.fix) return;
    const next = applyComposePatch(value, d.fix.patch);
    if (next !== null) onChange(next);
  };

  // Parse the incoming YAML to learn whether it's representable + its version.
  const parsed = useMemo(() => parseCompose(value), [value]);

  // The editable per-service model is held in LOCAL state, not derived directly
  // from the YAML. This is what lets an in-progress, still-blank row survive: a
  // freshly "Add variable"/"Add port"/"Add volume" row has an empty key/target,
  // and buildCompose deliberately drops incomplete rows from the emitted YAML —
  // so re-deriving the editor from that YAML would make the new row vanish the
  // instant it's added. We seed from the YAML and only re-sync when the value
  // changes from the OUTSIDE (a YAML-mode edit, a different deployment, a Reset)
  // — never from the echo of our own commits.
  const [services, setServices] = useState<ServiceModel[]>(
    () => parseCompose(value).services,
  );
  const lastEmitted = useRef<string>(value);

  useEffect(() => {
    if (value === lastEmitted.current) return; // ignore our own echo
    lastEmitted.current = value;
    setServices(parseCompose(value).services);
  }, [value]);

  // If the current YAML is not representable, force the user into YAML mode so
  // they never see a misleadingly-empty easy editor over real content.
  useEffect(() => {
    if (mode === "easy" && parsed.unrepresentable) {
      setMode("yaml");
    }
  }, [mode, parsed.unrepresentable]);

  const commit = (next: ServiceModel[]) => {
    setServices(next);
    const yaml = buildCompose(next, parsed.version);
    lastEmitted.current = yaml;
    onChange(yaml);
  };

  const updateService = (index: number, patch: Partial<ServiceModel>) =>
    commit(services.map((s, i) => (i === index ? { ...s, ...patch } : s)));

  const removeService = (index: number) =>
    commit(services.filter((_, i) => i !== index));

  const addService = () => {
    const used = new Set(services.map((s) => s.name));
    let name = "service";
    let n = 1;
    while (used.has(name)) {
      n += 1;
      name = `service${n}`;
    }
    commit([...services, emptyService(name)]);
  };

  return (
    <Box direction="column" gap={4}>
      <Box direction="row" wrap justify="between" align="center">
        <Text size="sm" weight="medium">
          Compose
        </Text>
        <Tabs
          variant="segmented"
          value={mode}
          onValueChange={(v) => setMode(v as Mode)}
        >
          <Tabs.List>
            <Tabs.Trigger value="easy">Easy</Tabs.Trigger>
            <Tabs.Trigger value="yaml">YAML</Tabs.Trigger>
          </Tabs.List>
        </Tabs>
      </Box>

      {mode === "yaml" ? (
        <Suspense fallback={<Skeleton height={360} />}>
          <YamlEditor
            value={value}
            onChange={onChange}
            diagnostics={validation ? check.diagnostics : undefined}
            jumpTo={jump}
          />
        </Suspense>
      ) : parsed.unrepresentable ? (
        <Callout
          color="warning"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Easy editor unavailable"
        >
          This compose uses features the easy editor can&apos;t represent — edit
          in YAML mode.
        </Callout>
      ) : (
        <Box direction="column" gap={4}>
          {services.length === 0 && (
            <Text size="sm" color="muted">
              No services yet. Add one to get started.
            </Text>
          )}

          {services.map((svc, index) => (
            <ServiceCard
              key={index}
              service={svc}
              onChange={(patch) => updateService(index, patch)}
              onRemove={() => removeService(index)}
            />
          ))}

          <Box direction="row" wrap>
            <Button
              variant="light"
              iconLeft="add-rounded"
              onClick={addService}
            >
              Add service
            </Button>
          </Box>
        </Box>
      )}

      {validation && value.trim() !== "" && (
        <ComposeDiagnostics
          diagnostics={check.diagnostics}
          result={check.result}
          loading={check.loading}
          error={check.error}
          onJump={jumpTo}
          onFix={applyFix}
        />
      )}
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Per-service editor card
// ---------------------------------------------------------------------------

interface ServiceCardProps {
  service: ServiceModel;
  onChange: (patch: Partial<ServiceModel>) => void;
  onRemove: () => void;
}

function ServiceCard({ service, onChange, onRemove }: ServiceCardProps) {
  // Ports -------------------------------------------------------------------
  const addPort = () =>
    onChange({
      ports: [...service.ports, { host: "", container: "", proto: "tcp" }],
    });
  const updatePort = (i: number, patch: Partial<PortRow>) =>
    onChange({
      ports: service.ports.map((p, idx) => (idx === i ? { ...p, ...patch } : p)),
    });
  const removePort = (i: number) =>
    onChange({ ports: service.ports.filter((_, idx) => idx !== i) });

  // Environment -------------------------------------------------------------
  const addEnv = () => onChange({ env: [...service.env, { key: "", value: "" }] });
  const updateEnv = (i: number, patch: Partial<EnvRow>) =>
    onChange({
      env: service.env.map((e, idx) => (idx === i ? { ...e, ...patch } : e)),
    });
  const removeEnv = (i: number) =>
    onChange({ env: service.env.filter((_, idx) => idx !== i) });

  // Volumes -----------------------------------------------------------------
  const addVolume = () =>
    onChange({
      volumes: [
        ...service.volumes,
        { source: "", target: "", readOnly: false },
      ],
    });
  const updateVolume = (i: number, patch: Partial<VolumeRow>) =>
    onChange({
      volumes: service.volumes.map((v, idx) =>
        idx === i ? { ...v, ...patch } : v,
      ),
    });
  const removeVolume = (i: number) =>
    onChange({ volumes: service.volumes.filter((_, idx) => idx !== i) });

  return (
    <Card variant="raised">
      <Card.Body>
        <Box direction="column" gap={4}>
          <Box direction="row" justify="between" align="end" wrap="nowrap">
            <TextInput
              label="Service name"
              placeholder="web"
              value={service.name}
              onChange={(e) => onChange({ name: e.target.value })}
              style={{ flex: 1 }}
            />
            <Button
              variant="clear"
              color="danger"
              aria-label="Remove service"
              iconLeft="delete-outline-rounded"
              onClick={onRemove}
            />
          </Box>

          <ImageInput
            value={service.image}
            onChange={(v) => onChange({ image: v })}
          />

          {/* Ports */}
          <Box direction="column">
            <Box direction="row" wrap justify="between" style={{ marginBottom: 4 }}>
              <Text size="sm" weight="medium">
                Ports
              </Text>
              <Button
                size="sm"
                variant="light"
                iconLeft="add-rounded"
                onClick={addPort}
              >
                Add
              </Button>
            </Box>
            <Box direction="column" gap={2}>
              {service.ports.length === 0 && (
                <Text size="xs" color="muted">
                  No published ports.
                </Text>
              )}
              {service.ports.map((row, i) => (
                <Box key={i} direction="row" gap={2} wrap="nowrap" align="end">
                  <TextInput
                    placeholder="host"
                    value={row.host}
                    onChange={(e) => updatePort(i, { host: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <TextInput
                    placeholder="container"
                    value={row.container}
                    onChange={(e) =>
                      updatePort(i, { container: e.target.value })
                    }
                    style={{ flex: 1 }}
                  />
                  <Select
                    style={{ width: 90 }}
                    options={[
                      { value: "tcp", label: "tcp" },
                      { value: "udp", label: "udp" },
                    ]}
                    value={row.proto}
                    onChange={(v) => updatePort(i, { proto: v ?? "tcp" })}
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
          </Box>

          {/* Environment */}
          <Box direction="column">
            <Box direction="row" wrap justify="between" style={{ marginBottom: 4 }}>
              <Text size="sm" weight="medium">
                Environment
              </Text>
              <Button
                size="sm"
                variant="light"
                iconLeft="add-rounded"
                onClick={addEnv}
              >
                Add
              </Button>
            </Box>
            <Box direction="column" gap={2}>
              {service.env.length === 0 && (
                <Text size="xs" color="muted">
                  No environment variables.
                </Text>
              )}
              {service.env.map((row, i) => (
                <Box key={i} direction="row" gap={2} wrap="nowrap" align="end">
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
          </Box>

          {/* Volumes */}
          <Box direction="column">
            <Box direction="row" wrap justify="between" style={{ marginBottom: 4 }}>
              <Text size="sm" weight="medium">
                Volumes
              </Text>
              <Button
                size="sm"
                variant="light"
                iconLeft="add-rounded"
                onClick={addVolume}
              >
                Add
              </Button>
            </Box>
            <Box direction="column" gap={2}>
              {service.volumes.length === 0 && (
                <Text size="xs" color="muted">
                  No volume mappings.
                </Text>
              )}
              {service.volumes.map((row, i) => (
                <Box key={i} direction="row" gap={2} wrap="nowrap" align="end">
                  <TextInput
                    placeholder="source / named volume"
                    value={row.source}
                    onChange={(e) =>
                      updateVolume(i, { source: e.target.value })
                    }
                    style={{ flex: 1 }}
                  />
                  <TextInput
                    placeholder="/container/path"
                    value={row.target}
                    onChange={(e) =>
                      updateVolume(i, { target: e.target.value })
                    }
                    style={{ flex: 1 }}
                  />
                  <Select
                    style={{ width: 80 }}
                    options={[
                      { value: "rw", label: "rw" },
                      { value: "ro", label: "ro" },
                    ]}
                    value={row.readOnly ? "ro" : "rw"}
                    onChange={(v) => updateVolume(i, { readOnly: v === "ro" })}
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
          </Box>

          <Box display="grid" fullWidth style={{ gridTemplateColumns: "1fr" }} align="start">
            <Select
              label="Restart policy"
              options={RESTART_OPTIONS}
              value={service.restart}
              onChange={(v) => onChange({ restart: v ?? "" })}
            />
          </Box>

          <TextInput
            label="Command"
            description="Optional override, space-separated."
            placeholder="nginx -g 'daemon off;'"
            value={service.command}
            onChange={(e) => onChange({ command: e.target.value })}
          />

          <TagsInput
            label="Depends on"
            description="Other service names this service depends on. Press Enter to add."
            placeholder="service name"
            value={service.dependsOn}
            onChange={(v) => onChange({ dependsOn: v })}
          />
        </Box>
      </Card.Body>
    </Card>
  );
}

