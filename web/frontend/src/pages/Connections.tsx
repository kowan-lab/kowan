import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Icon,
  Popup,
  Select,
  Table,
  Text,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  connectionTypes,
  createConnection,
  deleteConnection,
  listConnections,
  updateConnection,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { SLOW_REFETCH_MS } from "../lib/live";
import type { Connection, ConnectionType, PluginField } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";
import { toKitColor } from "../ui/colors";

const TYPE_COLORS: Record<string, string> = {
  aws: "orange",
  azure: "blue",
  gcp: "red",
  generic: "gray",
  ghcr: "gray",
  vault: "blue",
  infisical: "purple",
  phase: "rose",
  smtp: "teal",
  slack: "magenta",
  webhook: "cyan",
  telegram: "blue",
  discord: "blue",
  ntfy: "green",
  github: "gray",
  gitlab: "orange",
  bitbucket: "blue",
  "git-http": "gray",
  "api-token": "teal",
  "basic-auth": "magenta",
};

// Soft color per capability so the badges are visually distinguishable.
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

function emptyForm(defaultType: string): ConnectionFormState {
  return { name: "", type: defaultType, config: {} };
}

/**
 * Seed the form for editing. Sensitive fields render empty (= keep the stored
 * value); non-sensitive fields are prefilled from the masked config the API
 * returns.
 */
function formFromConnection(
  connection: Connection,
  fields: PluginField[],
): ConnectionFormState {
  const config: Record<string, string> = {};
  for (const field of fields) {
    if (field.sensitive) {
      config[field.key] = "";
    } else {
      config[field.key] = connection.config?.[field.key] ?? "";
    }
  }
  return {
    name: connection.name,
    type: connection.type,
    config,
  };
}

export default function Connections({ embedded }: { embedded?: boolean } = {}) {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();

  const isAdmin = currentUser?.role === "admin";

  const [formOpen, setFormOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Connection | null>(null);
  const [form, setForm] = useState<ConnectionFormState>(emptyForm(""));
  const [deleteTarget, setDeleteTarget] = useState<Connection | null>(null);

  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
    enabled: isAdmin,
    // Credentials change rarely — poll on the slow tier.
    refetchInterval: SLOW_REFETCH_MS,
  });

  const typesQuery = useQuery({
    queryKey: ["connection-types"],
    queryFn: connectionTypes,
    enabled: isAdmin,
  });

  const typesByName = useMemo(() => {
    const map: Record<string, ConnectionType> = {};
    for (const t of typesQuery.data ?? []) map[t.type] = t;
    return map;
  }, [typesQuery.data]);

  // Type facet options for the table's select filter — derived from the data
  // (data-mode needs explicit options, unlike the old DataTable which
  // auto-faceted). Declared before the early return to respect rules of hooks.
  const typeFilterOptions = useMemo(
    () =>
      Array.from(
        new Set((connectionsQuery.data ?? []).map((c) => c.type).filter(Boolean)),
      )
        .sort()
        .map((v) => ({ value: v, label: typeLabel(v) })),
    [connectionsQuery.data],
  );

  const types = typesQuery.data ?? [];
  const typeOptions = types.map((t) => ({
    value: t.type,
    label: typeLabel(t.type),
  }));
  const currentFields: PluginField[] = typesByName[form.type]?.fields ?? [];

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["connections"] });

  const saveMutation = useMutation({
    mutationFn: () => {
      const fields = typesByName[form.type]?.fields ?? [];
      // Build the config map. On edit, sensitive fields left blank are omitted
      // so the stored value is kept.
      const config: Record<string, string> = {};
      for (const field of fields) {
        const value = form.config[field.key] ?? "";
        if (editTarget && field.sensitive && value.length === 0) continue;
        config[field.key] = value;
      }
      if (editTarget) {
        return updateConnection(editTarget.id, {
          name: form.name.trim(),
          config,
        });
      }
      return createConnection({
        name: form.name.trim(),
        type: form.type,
        config,
      });
    },
    onSuccess: (saved) => {
      notify({
        color: "green",
        title: editTarget ? "Credential updated" : "Credential created",
        message: `${saved.name} was saved`,
      });
      setFormOpen(false);
      setEditTarget(null);
      setForm(emptyForm(types[0]?.type ?? ""));
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not save credential",
        message: errorMessage(err, "Failed to save credential"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteConnection(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.name ?? id;
      notify({
        color: "green",
        title: "Credential deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete credential",
        message: errorMessage(err, "Failed to delete credential"),
      });
    },
  });

  if (!isAdmin) {
    return (
      <Box direction="column" gap={5}>
        {!embedded && (
          <PageHeader
            title="Credentials"
            description="Define a credential once, then reference it from registries, secrets and channels."
            icon={<Icon name="key-outline-rounded" size="lg" />}
          />
        )}
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage credentials.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    setEditTarget(null);
    setForm(emptyForm(types[0]?.type ?? ""));
    setFormOpen(true);
  };

  const openEdit = (connection: Connection) => {
    const fields = typesByName[connection.type]?.fields ?? [];
    setEditTarget(connection);
    setForm(formFromConnection(connection, fields));
    setFormOpen(true);
  };

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
    // On create, required fields must be provided. On edit a blank sensitive
    // value keeps the stored one, so it is not required.
    if (!editTarget) {
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
    }
    saveMutation.mutate();
  };

  const connections = connectionsQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  const columns: Column<Connection>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      accessor: (c) => c.name,
      cell: (c) => <Text weight="medium">{c.name}</Text>,
    },
    {
      key: "type",
      header: "Type",
      sortable: true,
      accessor: (c) => c.type,
      filter: { kind: "select", options: typeFilterOptions },
      filterFn: (c, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(c.type),
      cell: (c) => (
        <Badge
          variant="light"
          color={toKitColor(TYPE_COLORS[c.type] ?? "gray", "gray")}
        >
          {typeLabel(c.type)}
        </Badge>
      ),
    },
    {
      key: "capabilities",
      header: "Capabilities",
      cell: (c) => {
        const capabilities = typesByName[c.type]?.capabilities ?? [];
        return capabilities.length > 0 ? (
          <Box direction="row" gap={2} wrap>
            {capabilities.map((cap) => (
              <Badge
                key={cap}
                size="sm"
                variant="light"
                color={toKitColor(CAPABILITY_COLORS[cap] ?? "gray", "gray")}
              >
                {cap}
              </Badge>
            ))}
          </Box>
        ) : (
          <Text size="xs" color="muted">
            —
          </Text>
        );
      },
    },
    {
      key: "updated",
      header: "Updated",
      sortable: true,
      accessor: (c) => c.updatedAt,
      cell: (c) => formatDate(c.updatedAt),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (c) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(c)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (connection: Connection) => (
    <Box direction="row" gap={2} justify="end" wrap={false}>
      <Tooltip content="Edit credential">
        <Button
          variant="clear"
          color="blue"
          iconLeft="edit-outline-rounded"
          onClick={() => openEdit(connection)}
          aria-label={`Edit ${connection.name}`}
        />
      </Tooltip>
      <Tooltip content={`Delete ${connection.name}`}>
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(connection)}
          aria-label={`Delete ${connection.name}`}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={5}>
      {embedded ? (
        <Box direction="row" justify="end" style={{ marginBottom: 16 }}>
          <Button
            color="info"
            iconLeft="add-rounded"
            onClick={openCreate}
          >
            New credential
          </Button>
        </Box>
      ) : (
        <PageHeader
          title="Credentials"
          description="Define a credential once, then reference it from registries, secrets and channels."
          icon={<Icon name="key-outline-rounded" size="lg" />}
          actions={
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={openCreate}
            >
              New credential
            </Button>
          }
        />
      )}

      <Callout variant="light" color="blue" title="Shared credentials">
        A credential is defined once and other subsystems reference it by id —
        e.g. a registry can delegate its auth to a credential with the{" "}
        <Text as="span" weight="medium">
          dockerRegistryAuth
        </Text>{" "}
        capability instead of storing its own username and password.
      </Callout>

      <SectionCard
        plain
        title="Credentials"
        icon={<Icon name="key-outline-rounded" />}
        actions={
          !connectionsQuery.isLoading && !connectionsQuery.isError ? (
            <Text size="sm" color="muted">
              {connections.length}{" "}
              {connections.length === 1 ? "credential" : "credentials"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {connectionsQuery.isError ? (
          <Box padding={4}>
            <Callout
              color="danger"
              iconLeft={<Icon name="warning-outline-rounded" />}
              title="Failed to load credentials"
            >
              {errorMessage(
                connectionsQuery.error,
                "Could not load the credential list.",
              )}
            </Callout>
          </Box>
        ) : (
          <Table<Connection>
            data={connections}
            columns={columns}
            rowKey={(c) => c.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={connectionsQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={
                  <Icon name="key-outline-rounded" size="xl" />
                }
                title="No credentials yet"
                description="Create a reusable credential that registries, secrets and channels can share."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                  >
                    New credential
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      {/* Create / edit modal */}
      <Popup
        open={formOpen}
        onOpenChange={(open) => {
          if (!open) setFormOpen(false);
        }}
        size="lg"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {editTarget ? `Edit ${editTarget.name}` : "New credential"}
            </Popup.Title>
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
                <Select
                  label="Type"
                  description="The kind of credential and what it can be used for."
                  options={typeOptions}
                  value={form.type}
                  disabled={!!editTarget}
                  onChange={(v) => v && changeType(v as string)}
                />

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

                {editTarget ? (
                  <Text size="xs" color="muted">
                    Leave a sensitive field blank to keep the currently stored
                    value.
                  </Text>
                ) : null}

                {currentFields.length > 0 ? (
                  <Box direction="column" gap={2}>
                    {currentFields.map((field) => {
                      const editHasValue =
                        !!editTarget &&
                        (editTarget.configKeys ?? []).includes(field.key);
                      if (field.sensitive) {
                        return (
                          <TextInput
                            type="password"
                            key={field.key}
                            label={field.label}
                            placeholder={
                              editHasValue ? "(unchanged)" : field.placeholder
                            }
                            autoComplete="new-password"
                            required={(field.required ?? false) && !editTarget}
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
                  onClick={() => setFormOpen(false)}
                  disabled={saveMutation.isPending}
                >
                  Cancel
                </Button>
                <Button
                  color="info"
                  type="submit"
                  loading={saveMutation.isPending}
                >
                  {editTarget ? "Save" : "Create"}
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete credential"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? Anything referencing this credential will stop working.
          </>
        }
      />
    </Box>
  );
}
