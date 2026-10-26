import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge, Box, Select, Text, TextInput } from "@/ui/kit";
import { listContainers, listDeployments, listHosts } from "../api/client";
import type { Grant, GrantInput, GrantScope, PermissionLevel } from "../types";

const SCOPE_OPTIONS: { value: GrantScope; label: string; description: string }[] = [
  { value: "all_hosts", label: "All hosts", description: "Every host and everything on it, plus fleet-wide objects (workflows)." },
  { value: "host", label: "One host", description: "Everything on one host: containers, images, volumes, networks, deployments." },
  { value: "deployment", label: "Deployment", description: "One deployment and the containers, volumes and networks it runs." },
  { value: "stack", label: "Stack", description: "Containers, volumes and networks of one compose project on one host." },
  { value: "labels", label: "Label selector", description: "Containers, volumes and networks carrying a key=value label." },
];

const LEVEL_OPTIONS: { value: PermissionLevel; label: string; description: string }[] = [
  { value: "read", label: "Read", description: "See, inspect, logs, stats." },
  { value: "operate", label: "Operate", description: "Read + start, stop, restart, exec, deploy." },
  { value: "manage", label: "Manage", description: "Operate + create, update and delete." },
];

export const EMPTY_GRANT: GrantInput = { scope: "host", hostId: "", target: "", level: "read" };

function levelColor(level: string): "gray" | "blue" | "amber" {
  return level === "manage" ? "amber" : level === "operate" ? "blue" : "gray";
}

export function grantValid(g: GrantInput): string | null {
  if ((g.scope === "host" || g.scope === "stack") && !g.hostId) return "Pick a host";
  if (g.scope === "deployment" && !g.target) return "Pick a deployment";
  if (g.scope === "stack" && !g.target) return "Enter the compose project name";
  if (g.scope === "labels" && !/^[^=\s]+=.*$/.test(g.target ?? "")) return "Use key=value";
  return null;
}

/** Plain-language description of a grant's scope, e.g. "stack web on host-1". */
export function describeGrant(g: Pick<Grant, "scope" | "hostId" | "target">, hostName: (id: string) => string, depName: (id: string) => string): string {
  switch (g.scope) {
    case "all_hosts":
      return "all hosts";
    case "host":
      return `host ${hostName(g.hostId)}`;
    case "deployment":
      return `deployment ${depName(g.target)}`;
    case "stack":
      return `stack ${g.target} on ${hostName(g.hostId)}`;
    case "labels":
      return `label ${g.target}${g.hostId ? ` on ${hostName(g.hostId)}` : " on any host"}`;
    default:
      return g.scope;
  }
}

/** Lookup helpers shared by the Teams, Users and API key pages. */
export function useScopeNames() {
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const deps = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  return useMemo(() => {
    const h = new Map((hosts.data ?? []).map((x) => [x.id, x.name]));
    const d = new Map((deps.data ?? []).map((x) => [x.id, x.name]));
    return {
      hosts: hosts.data ?? [],
      deployments: deps.data ?? [],
      hostName: (id: string) => h.get(id) ?? id,
      depName: (id: string) => d.get(id) ?? id,
    };
  }, [hosts.data, deps.data]);
}

export function GrantBadge({ level }: { level: string }) {
  return (
    <Badge color={levelColor(level)} variant="outline">
      {level}
    </Badge>
  );
}

/** Scope + level picker for one grant. */
export default function GrantFields({ value, onChange }: { value: GrantInput; onChange: (g: GrantInput) => void }) {
  const names = useScopeNames();
  const needsHost = value.scope === "host" || value.scope === "stack";
  const showHost = needsHost || value.scope === "labels";
  // Suggest compose projects running on the chosen host.
  const containers = useQuery({
    queryKey: ["containers", value.hostId, "grant-suggest"],
    queryFn: () => listContainers(value.hostId ?? "", true),
    enabled: value.scope === "stack" && Boolean(value.hostId),
  });
  const projects = useMemo(
    () =>
      Array.from(new Set((containers.data ?? []).map((c) => c.labels?.["com.docker.compose.project"]).filter((p): p is string => Boolean(p)))).sort(),
    [containers.data],
  );
  const set = (patch: Partial<GrantInput>) => onChange({ ...value, ...patch });
  const scopeHelp = SCOPE_OPTIONS.find((s) => s.value === value.scope)?.description;

  return (
    <Box direction="column" gap={4}>
      <Select
        label="Scope"
        description={scopeHelp}
        options={SCOPE_OPTIONS.map((s) => ({ value: s.value, label: s.label }))}
        value={value.scope}
        onChange={(v) => set({ scope: v as GrantScope, target: "", hostId: v === "all_hosts" || v === "deployment" ? "" : value.hostId })}
      />
      {showHost && (
        <Select
          label={needsHost ? "Host" : "Host (optional)"}
          description={needsHost ? undefined : "Leave empty to match the label on every host."}
          options={names.hosts.map((h) => ({ value: h.id, label: h.name }))}
          value={value.hostId || null}
          clearable={!needsHost}
          searchable
          onChange={(v) => set({ hostId: v ?? "" })}
        />
      )}
      {value.scope === "deployment" && (
        <Select
          label="Deployment"
          options={names.deployments.map((d) => ({ value: d.id, label: d.name }))}
          value={value.target || null}
          searchable
          onChange={(v) => set({ target: v })}
        />
      )}
      {value.scope === "stack" && (
        <Box direction="column" gap={2}>
          <TextInput
            label="Compose project"
            placeholder="web"
            value={value.target ?? ""}
            onChange={(e) => set({ target: e.target.value.trim() })}
          />
          {projects.length > 0 && (
            <Box direction="row" gap={2} wrap>
              <Text size="xs" color="muted">On this host:</Text>
              {projects.map((p) => (
                <button key={p} type="button" className="font-mono text-[12px] text-primary underline-offset-2 hover:underline" onClick={() => set({ target: p })}>
                  {p}
                </button>
              ))}
            </Box>
          )}
        </Box>
      )}
      {value.scope === "labels" && (
        <TextInput
          label="Label selector"
          placeholder="team=payments"
          description="Exact match on a container, volume or network label."
          value={value.target ?? ""}
          onChange={(e) => set({ target: e.target.value.trim() })}
        />
      )}
      <Select
        label="Level"
        description={LEVEL_OPTIONS.find((l) => l.value === value.level)?.description}
        options={LEVEL_OPTIONS.map((l) => ({ value: l.value, label: l.label }))}
        value={value.level}
        onChange={(v) => set({ level: v as PermissionLevel })}
      />
    </Box>
  );
}
