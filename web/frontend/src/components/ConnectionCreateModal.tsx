import { useEffect, useMemo, useState } from "react";
import { Badge, Box, Button, Popup, Select, Text, TextInput } from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { connectionTypes, createConnection } from "../api/client";
import type { Connection, ConnectionType, PluginField } from "../types";
import { errorMessage } from "../utils";
import { notify } from "../ui/toast";
import { toKitColor } from "../ui/colors";

// Soft color per capability so the badges are visually distinguishable.
// Mirrors the palette used on the Connections page.
const CAPABILITY_COLORS: Record<string, string> = {
  dockerRegistryAuth: "blue",
  secretFetch: "teal",
  messageSend: "magenta",
  httpBearer: "orange",
  cloudIdentity: "purple",
  gitFetch: "green",
  gitApi: "lime",
};

/** Title-case a type/capability for display (e.g. "vault" → "Vault"). */
function typeLabel(type: string): string {
  return type.charAt(0).toUpperCase() + type.slice(1);
}

interface ConnectionFormState {
  name: string;
  type: string;
  /** Field key -> current input value. */
  config: Record<string, string>;
}

export interface ConnectionCreateModalProps {
  open: boolean;
  onClose: () => void;
  /** Called with the created connection. */
  onCreated: (conn: Connection) => void;
  /** When set, the Type dropdown offers ONLY these types. */
  restrictTypes?: string[];
  /** Modal title. Defaults to "New credential". */
  title?: string;
}

/**
 * Self-contained modal that creates a connection credential in place. Mirrors
 * the create form on the Connections page (field rendering, per-type config,
 * create mutation) but can be dropped anywhere and restricted to a subset of
 * connection types — so a picker only offers the types it can consume.
 */
export default function ConnectionCreateModal({
  open,
  onClose,
  onCreated,
  restrictTypes,
  title = "New credential",
}: ConnectionCreateModalProps) {
  const queryClient = useQueryClient();

  // Shared cache with the page and the picker.
  const typesQuery = useQuery({
    queryKey: ["connection-types"],
    queryFn: connectionTypes,
  });

  const typesByName = useMemo(() => {
    const map: Record<string, ConnectionType> = {};
    for (const t of typesQuery.data ?? []) map[t.type] = t;
    return map;
  }, [typesQuery.data]);

  // The types this modal is allowed to create. When restrictTypes is given it
  // wins; otherwise every known type is offered.
  // Callers typically pass restrictTypes as a fresh array each render; key the
  // memo (and the seeding effect below) on its contents, not its identity.
  const restrictKey = (restrictTypes ?? []).join(",");
  const availableTypes = useMemo(() => {
    const all = typesQuery.data ?? [];
    if (restrictKey) {
      const allowed = new Set(restrictKey.split(","));
      return all.filter((t) => allowed.has(t.type));
    }
    return all;
  }, [typesQuery.data, restrictKey]);
  const availableKey = availableTypes.map((t) => t.type).join(",");

  const singleType = availableTypes.length === 1;

  const [form, setForm] = useState<ConnectionFormState>({
    name: "",
    type: "",
    config: {},
  });

  // Seed the form when the modal opens (and reset any leftover state).
  useEffect(() => {
    if (open) {
      setForm({ name: "", type: availableTypes[0]?.type ?? "", config: {} });
    }
    // Keyed on the type list's contents: re-seeding on every parent re-render
    // (pages poll every few seconds) would wipe what the user is typing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, availableKey]);

  const currentFields: PluginField[] = typesByName[form.type]?.fields ?? [];

  const typeOptions = availableTypes.map((t) => ({
    value: t.type,
    label: typeLabel(t.type),
  }));

  // Switch type: reset config to the new type's fields, preserving any values
  // whose key still exists.
  const changeType = (type: string) =>
    setForm((f) => {
      const fields = typesByName[type]?.fields ?? [];
      const config: Record<string, string> = {};
      for (const field of fields) {
        config[field.key] = f.config[field.key] ?? "";
      }
      return { ...f, type, config };
    });

  const setConfigValue = (key: string, value: string) =>
    setForm((f) => ({ ...f, config: { ...f.config, [key]: value } }));

  const createMutation = useMutation({
    mutationFn: () => {
      const fields = typesByName[form.type]?.fields ?? [];
      const config: Record<string, string> = {};
      for (const field of fields) {
        config[field.key] = form.config[field.key] ?? "";
      }
      return createConnection({
        name: form.name.trim(),
        type: form.type,
        config,
      });
    },
    onSuccess: (created) => {
      notify({
        color: "green",
        title: "Credential created",
        message: `${created.name} was saved`,
      });
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
      onCreated(created);
      onClose();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not create credential",
        message: errorMessage(err, "Failed to create credential"),
      });
    },
  });

  const submitForm = (e: React.FormEvent) => {
    e.preventDefault();
    if (form.name.trim().length === 0) {
      notify({
        color: "red",
        title: "Name is required",
        message: "Give the credential a name.",
      });
      return;
    }
    if (form.type.length === 0) {
      notify({
        color: "red",
        title: "Type is required",
        message: "Pick a credential type.",
      });
      return;
    }
    const missing = currentFields.filter(
      (spec) =>
        spec.required && (form.config[spec.key] ?? "").trim().length === 0,
    );
    if (missing.length > 0) {
      notify({
        color: "red",
        title: "Missing required fields",
        message: `Required: ${missing.map((m) => m.label).join(", ")}.`,
      });
      return;
    }
    createMutation.mutate();
  };

  return (
    <Popup
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      size="lg"
    >
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{title}</Popup.Title>
        </Popup.Header>
        <form onSubmit={submitForm}>
          <Popup.Body>
            <Box direction="column" gap={4}>
              <TextInput
                label="Name"
                placeholder="my-credential"
                autoFocus
                value={form.name}
                onChange={(e) =>
                  setForm((f) => ({ ...f, name: e.target.value }))
                }
              />
              {singleType ? (
                <Select
                  label="Type"
                  description="The kind of credential and what it can be used for."
                  options={typeOptions}
                  value={form.type}
                  disabled
                  onChange={() => undefined}
                />
              ) : (
                <Select
                  label="Type"
                  description="The kind of credential and what it can be used for."
                  options={typeOptions}
                  value={form.type}
                  onChange={(v) => v && changeType(v as string)}
                />
              )}

              {typesByName[form.type]?.capabilities?.length ? (
                <Box direction="row" gap={2} wrap>
                  {typesByName[form.type].capabilities.map((cap) => (
                    <Badge
                      key={cap}
                      size="sm"
                      variant="light"
                      color={toKitColor(
                        CAPABILITY_COLORS[cap] ?? "gray",
                        "gray",
                      )}
                    >
                      {cap}
                    </Badge>
                  ))}
                </Box>
              ) : null}

              {currentFields.length > 0 ? (
                <Box direction="column" gap={2}>
                  {currentFields.map((field) => {
                    if (field.sensitive) {
                      return (
                        <TextInput
                          type="password"
                          key={field.key}
                          label={field.label}
                          placeholder={field.placeholder}
                          autoComplete="new-password"
                          required={field.required ?? false}
                          value={form.config[field.key] ?? ""}
                          onChange={(e) =>
                            setConfigValue(field.key, e.target.value)
                          }
                        />
                      );
                    }
                    return (
                      <TextInput
                        key={field.key}
                        label={field.label}
                        placeholder={field.placeholder}
                        autoComplete="off"
                        required={field.required ?? false}
                        value={form.config[field.key] ?? ""}
                        onChange={(e) =>
                          setConfigValue(field.key, e.target.value)
                        }
                      />
                    );
                  })}
                </Box>
              ) : (
                <Text size="xs" color="muted">
                  {typesQuery.isLoading
                    ? "Loading credential types…"
                    : "This credential type has no configurable fields."}
                </Text>
              )}
            </Box>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" justify="end" gap={2}>
              <Button
                variant="outline"
                color="neutral"
                onClick={onClose}
                disabled={createMutation.isPending}
              >
                Cancel
              </Button>
              <Button
                color="info"
                type="submit"
                loading={createMutation.isPending}
              >
                Create
              </Button>
            </Box>
          </Popup.Footer>
        </form>
      </Popup.Content>
    </Popup>
  );
}
