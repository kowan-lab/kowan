import { useEffect, useMemo, useState } from "react";
import { Box, Button, Callout, Icon, Select } from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { connectionTypes, listConnections } from "../api/client";
import ConnectionCreateModal from "./ConnectionCreateModal";

/**
 * The connection capabilities a credential form can require. A connection is
 * eligible to back a form when its type's `capabilities` include this string.
 * Mirrors the backend capability names surfaced by /credentials/types.
 */
type ConnectionCapability =
  | "dockerRegistryAuth"
  | "secretFetch"
  | "messageSend"
  | "gitFetch"
  | "httpBearer"
  | "cloudIdentity";

const CAPABILITY_LABELS: Record<ConnectionCapability, string> = {
  dockerRegistryAuth: "registry",
  secretFetch: "secret-backend",
  messageSend: "notification",
  gitFetch: "git",
  httpBearer: "HTTP-auth",
  cloudIdentity: "cloud",
};

export interface ConnectionFieldProps {
  /** Only connections whose type implements this capability are offered. */
  capability: ConnectionCapability;
  /** Selected connection id. `''` means nothing chosen yet. */
  value: string;
  /** Called with the chosen connection id. */
  onChange: (connectionRef: string) => void;
  /**
   * True on edit forms: the passed `value` is preserved as-is and the
   * default first-connection preselect is never applied.
   */
  isEdit?: boolean;
  label?: string;
  description?: string;
  /**
   * When provided, eligible connections are filtered by their `type` being in
   * this list INSTEAD of the capability-based filter. Used by consumers that
   * need a specific connection type (e.g. volumes: s3fs→["aws"],
   * cifs/sshfs→["basic-auth"]) rather than a capability.
   */
  types?: string[];
  /**
   * Overrides the empty-state title noun ("No {emptyLabel} credentials yet").
   * Defaults to the capability-derived label.
   */
  emptyLabel?: string;
  /**
   * When true, the picker becomes OPTIONAL: it offers a leading "None" choice,
   * never auto-preselects a connection, and never shows the blocking empty
   * state — so it is always usable (choose None or create one). Used for
   * surfaces where auth is not required (public git repos / public HTTP
   * endpoints). Defaults to false (required semantics, unchanged).
   */
  optional?: boolean;
}

/**
 * The single credential picker shared by every consuming form (registry,
 * notification channel, external secret, …).
 *
 * Credentials live ONLY in connections. A consuming form never accepts inline
 * credentials — it just selects a connection whose type implements the required
 * `capability`. When no eligible connection exists the list is empty and the
 * form cannot be completed until one is created (there is no manual fallback).
 *
 * The component owns NO submit state — the parent keeps its own form state and
 * decides what to send; this only surfaces the (required) picker.
 */
export default function ConnectionField({
  capability,
  value,
  onChange,
  isEdit = false,
  label = "Credential",
  description = "Auth comes from the selected credential.",
  types,
  emptyLabel,
  optional = false,
}: ConnectionFieldProps) {
  const [createOpen, setCreateOpen] = useState(false);

  // Same query keys the pages already use, so the react-query cache is shared.
  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
  });
  const connectionTypesQuery = useQuery({
    queryKey: ["connection-types"],
    queryFn: connectionTypes,
  });

  // Connection type names to offer. When an explicit `types` list is given it
  // wins (filter by connection type); otherwise fall back to the capability
  // filter (types whose capabilities include the required capability).
  // Callers pass `types` as an inline array literal; key on its contents so the
  // memo (and everything derived from it) is stable across re-renders.
  const typesKey = (types ?? []).join(",");
  const eligibleTypes = useMemo(() => {
    if (typesKey) return new Set(typesKey.split(","));
    const set = new Set<string>();
    for (const t of connectionTypesQuery.data ?? []) {
      if (t.capabilities.includes(capability)) set.add(t.type);
    }
    return set;
  }, [connectionTypesQuery.data, capability, typesKey]);

  // Connections whose type implements the capability — the only ones offered.
  const eligibleConnections = useMemo(
    () =>
      (connectionsQuery.data ?? []).filter((c) => eligibleTypes.has(c.type)),
    [connectionsQuery.data, eligibleTypes],
  );

  // Preselect the first eligible connection on a NEW form so the common case is
  // one click. On edit, preserve the stored value. If the current value is no
  // longer eligible (none exist), it is left as-is and the parent blocks submit.
  // Optional pickers never auto-preselect: "None" is a legitimate choice.
  useEffect(() => {
    if (!optional && !isEdit && value === "" && eligibleConnections.length >= 1) {
      onChange(eligibleConnections[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isEdit, eligibleConnections]);

  const createModal = (
    <ConnectionCreateModal
      open={createOpen}
      onClose={() => setCreateOpen(false)}
      restrictTypes={Array.from(eligibleTypes)}
      onCreated={(conn) => {
        setCreateOpen(false);
        onChange(conn.id);
      }}
    />
  );

  const newCredentialButton = (color?: "info") => (
    <Button
      variant="clear"
      color={color}
      size="sm"
      iconLeft="add-rounded"
      onClick={() => setCreateOpen(true)}
    >
      New credential
    </Button>
  );

  // No eligible credential exists: it must be defined first — there is no inline
  // entry. Show an empty state whose primary action creates one in place.
  // Optional pickers skip this: they are always usable (choose None or create).
  if (!optional && eligibleConnections.length === 0) {
    return (
      <Box direction="column" gap={2}>
        <Callout
          variant="light"
          color="yellow"
          iconLeft={<Icon name="info-outline-rounded" />}
          title={`No ${emptyLabel ?? CAPABILITY_LABELS[capability]} credentials yet`}
        >
          A credential is defined once. Create a credential that provides this
          capability, then select it here.
        </Callout>
        <Box direction="row" justify="start">
          {newCredentialButton("info")}
        </Box>
        {createModal}
      </Box>
    );
  }

  return (
    <Box direction="column" gap={1}>
      <Select
        label={label}
        description={description}
        options={
          optional
            ? [
                { value: "", label: "— None (no credential) —" },
                ...eligibleConnections.map((c) => ({
                  value: c.id,
                  label: c.name,
                })),
              ]
            : eligibleConnections.map((c) => ({
                value: c.id,
                label: c.name,
              }))
        }
        value={value}
        onChange={(v) => onChange((v as string) ?? "")}
      />
      <Box direction="row" justify="start">
        {newCredentialButton()}
      </Box>
      {createModal}
    </Box>
  );
}
