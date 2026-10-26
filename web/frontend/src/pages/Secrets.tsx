import { useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
  Icon,
  Popup,
  Select,
  Spinner,
  Table,
  Text,
  TextArea,
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
  createSecret,
  deleteSecret,
  listConnections,
  listSecretTypes,
  resolveReferences,
  revealSecret,
  updateSecret,
  listDeployments,
  listSecrets,
} from "../api/client";
import { Link } from "react-router";
import { appHref } from "../lib/apps";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import ConnectionField from "../components/ConnectionField";
import KeyValueEditor from "../components/KeyValueEditor";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { SLOW_REFETCH_MS } from "../lib/live";
import type { PluginField, ReferencePreview, Secret } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";
import { toKitColor } from "../ui/colors";
import { copyText } from "../lib/clipboard";


// Static secret types hold their value directly (free-form key/value fields).
// External backends (vault, infisical, phase, …) are NOT listed here: they are
// discovered at runtime from /secrets/types and render their own connection
// fields from the schema the backend reports.
const STATIC_SECRET_TYPES: { value: string; label: string }[] = [
  { value: "generic", label: "Generic" },
  { value: "registry", label: "Registry" },
  { value: "git", label: "Git" },
  { value: "env", label: "Environment" },
  { value: "token", label: "Token" },
];

const TYPE_COLORS: Record<string, string> = {
  generic: "gray",
  registry: "blue",
  git: "orange",
  env: "teal",
  token: "magenta",
  vault: "blue",
  infisical: "purple",
  phase: "rose",
};

const REF_STATUS_COLOR: Record<ReferencePreview["status"], string> = {
  resolved: "teal",
  unresolved: "gray",
  error: "red",
};

/** Title-case a provider type for display (e.g. "infisical" → "Infisical"). */
function typeLabel(type: string): string {
  return type.charAt(0).toUpperCase() + type.slice(1);
}

/** One editable field row in the create/edit modal. */
interface FieldRow {
  key: string;
  value: string;
}

/** Where a secret gets its value: typed in place, or fetched from a connection. */
type SecretSource = "static" | "connection";

interface SecretFormState {
  /** Static value vs backed by a shared connection. */
  source: SecretSource;
  name: string;
  /** Only meaningful for the static branch (generic/token/env/git/registry). */
  type: string;
  description: string;
  rows: FieldRow[];
  /**
   * Per-secret NON-SENSITIVE selector fields for the connection branch (e.g.
   * vault mount/path/key), keyed by field key. Credentials are never here —
   * they live in the connection.
   */
  external: Record<string, string>;
  /**
   * The shared connection (by id) whose type implements `secretFetch`. Required
   * in the connection branch; the backend's sensitive fields (address/token)
   * come from it, so they are never entered here.
   */
  connectionRef: string;
}

function emptyForm(): SecretFormState {
  return {
    source: "static",
    name: "",
    type: "generic",
    description: "",
    rows: [{ key: "", value: "" }],
    external: {},
    connectionRef: "",
  };
}

/** Seed the form for editing: existing field NAMES with blank (masked) values. */
function formFromSecret(secret: Secret): SecretFormState {
  const rows: FieldRow[] =
    secret.fields.length > 0
      ? secret.fields.map((k) => ({ key: k, value: "" }))
      : [{ key: "", value: "" }];
  // A secret backed by a connection has connectionRef set; everything else is
  // a static, in-place value.
  const source: SecretSource = secret.connectionRef ? "connection" : "static";
  return {
    source,
    name: secret.name,
    type: secret.type ?? "generic",
    description: secret.description ?? "",
    rows,
    // Reference values are never returned by the API; start blank so an
    // unchanged value is kept (sensitive) and clear-text ones can be re-entered.
    external: {},
    connectionRef: secret.connectionRef ?? "",
  };
}

export default function Secrets({ embedded }: { embedded?: boolean } = {}) {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();

  const isAdmin = currentUser?.role === "admin";

  const [formOpen, setFormOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Secret | null>(null);
  const [form, setForm] = useState<SecretFormState>(emptyForm());
  const [deleteTarget, setDeleteTarget] = useState<Secret | null>(null);
  const [revealTarget, setRevealTarget] = useState<Secret | null>(null);
  const [revealed, setRevealed] = useState<Record<string, string> | null>(null);
  const revealTargetId = useRef<string | null>(null);

  // Names of the workloads that own a secret (their secret variables).
  const deploymentsQuery = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const depNames = new Map((deploymentsQuery.data ?? []).map((d) => [d.id, d.name]));
  const secretsQuery = useQuery({
    queryKey: ["secrets"],
    queryFn: listSecrets,
    enabled: isAdmin,
    // Secrets are configuration that changes rarely — poll on the slow tier.
    refetchInterval: SLOW_REFETCH_MS,
  });

  // External secret backends (vault, infisical, phase, …) and their field
  // schema, discovered at runtime so a new backend needs no UI change. Used only
  // to know which NON-SENSITIVE selector fields (mount/path/key) a connection's
  // type exposes — credentials themselves live in the connection.
  const secretTypesQuery = useQuery({
    queryKey: ["secret-types"],
    queryFn: listSecretTypes,
    enabled: isAdmin,
  });
  const externalTypes = secretTypesQuery.data ?? [];
  const externalFields = (type: string): PluginField[] =>
    externalTypes.find((t) => t.type === type)?.fields ?? [];

  // Shared connections cache (same key ConnectionField uses). Used to derive the
  // type of the SELECTED connection so we can render its selector fields and
  // submit the right secret `type`.
  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
    enabled: isAdmin,
  });
  const selectedConnection = useMemo(
    () => (connectionsQuery.data ?? []).find((c) => c.id === form.connectionRef),
    [connectionsQuery.data, form.connectionRef],
  );
  const connectionType = selectedConnection?.type ?? "";
  // The connection type's NON-SENSITIVE selector fields (per-secret, not creds),
  // minus the ones the connection already stores (e.g. the Vault address): the
  // backend merges the connection's values under the secret's own.
  const selectorFields = useMemo(() => {
    const provided = new Set(selectedConnection?.configKeys ?? []);
    return externalFields(connectionType).filter(
      (spec) => !spec.sensitive && !provided.has(spec.key),
    );
    // externalFields closes over externalTypes; recompute when either changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectionType, selectedConnection, externalTypes]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["secrets"] });

  const saveMutation = useMutation({
    mutationFn: () => {
      // Connection branch: the secret is backed by a shared connection. Its type
      // is the connection's type; the fields are only the NON-SENSITIVE selector
      // values (mount/path/key). Credentials come from the connection.
      if (form.source === "connection") {
        const fields: Record<string, string> = {};
        for (const spec of selectorFields) {
          const value = form.external[spec.key] ?? "";
          if (editTarget && value.length === 0) continue;
          fields[spec.key] = value;
        }
        const body = {
          name: form.name.trim(),
          type: connectionType,
          description: form.description.trim(),
          fields,
          connectionRef: form.connectionRef,
        };
        return editTarget
          ? updateSecret(editTarget.id, body)
          : createSecret(body);
      }

      // Static branch: free-form rows held in place. On edit, values left blank
      // are omitted so the stored value is kept.
      const fields: Record<string, string> = {};
      for (const row of form.rows) {
        const key = row.key.trim();
        if (key.length === 0) continue;
        if (editTarget && row.value.length === 0) continue;
        fields[key] = row.value;
      }
      const body = {
        name: form.name.trim(),
        type: form.type,
        description: form.description.trim(),
        fields,
        // On edit, send "" to clear a previously delegated connection (revert to
        // an inline static value).
        ...(editTarget ? { connectionRef: "" } : {}),
      };
      return editTarget
        ? updateSecret(editTarget.id, body)
        : createSecret(body);
    },
    onSuccess: (saved) => {
      notify({
        color: "green",
        title: editTarget ? "Secret updated" : "Secret created",
        message: `${saved.name} was saved`,
      });
      setFormOpen(false);
      setEditTarget(null);
      setForm(emptyForm());
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not save secret",
        message: errorMessage(err, "Failed to save secret"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteSecret(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.name ?? id;
      notify({
        color: "green",
        title: "Secret deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete secret",
        message: errorMessage(err, "Failed to delete secret"),
      });
    },
  });

  const revealMutation = useMutation({
    mutationFn: (secret: Secret) => revealSecret(secret.id),
    onSuccess: (data, secret) => {
      // A slow response for a secret the user already closed (or switched away
      // from) must not land under another secret's title.
      if (revealTargetId.current !== secret.id) return;
      setRevealed(data.fields ?? {});
    },
    onError: (err) => {
      setRevealTarget(null);
      notify({
        color: "red",
        title: "Could not reveal secret",
        message: errorMessage(err, "Failed to reveal secret"),
      });
    },
  });

  // --- Live reference preview ---------------------------------------------
  // Type/paste any text with [[...]] tokens and (debounced) ask the server to
  // resolve them — masked, with a live backend fetch for external secrets.
  const [refText, setRefText] = useState("");
  const [debouncedRef, setDebouncedRef] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setDebouncedRef(refText), 400);
    return () => clearTimeout(id);
  }, [refText]);
  const hasTokens = debouncedRef.includes("[[");
  const previewQuery = useQuery({
    queryKey: ["resolve-refs", debouncedRef],
    queryFn: () => resolveReferences(debouncedRef),
    enabled: isAdmin && hasTokens,
  });

  // Select-filter options for the Type facet, derived from the data (data-mode
  // needs explicit options, unlike the old DataTable which auto-faceted).
  const typeOptions = useMemo(
    () =>
      Array.from(
        new Set((secretsQuery.data ?? []).map((s) => s.type).filter(Boolean)),
      )
        .sort()
        .map((t) => ({ value: t, label: t })),
    [secretsQuery.data],
  );

  if (!isAdmin) {
    return (
      <Box direction="column" gap={5}>
        {!embedded && (
          <PageHeader
            title="Secrets"
            description="Store encrypted credentials referenced across the platform."
            icon={<Icon name="key-outline-rounded" size="lg" />}
          />
        )}
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage secrets.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    setEditTarget(null);
    setForm(emptyForm());
    setFormOpen(true);
  };

  const openEdit = (secret: Secret) => {
    setEditTarget(secret);
    setForm(formFromSecret(secret));
    setFormOpen(true);
  };

  const openReveal = (secret: Secret) => {
    revealTargetId.current = secret.id;
    setRevealed(null);
    setRevealTarget(secret);
    revealMutation.mutate(secret);
  };

  const closeReveal = () => {
    revealTargetId.current = null;
    setRevealTarget(null);
    setRevealed(null);
  };

  const addRow = () =>
    setForm((f) => ({ ...f, rows: [...f.rows, { key: "", value: "" }] }));

  const updateRow = (i: number, patch: Partial<FieldRow>) =>
    setForm((f) => ({
      ...f,
      rows: f.rows.map((row, idx) => (idx === i ? { ...row, ...patch } : row)),
    }));

  const removeRow = (i: number) =>
    setForm((f) => ({ ...f, rows: f.rows.filter((_, idx) => idx !== i) }));

  const updateExternal = (key: string, value: string) =>
    setForm((f) => ({ ...f, external: { ...f.external, [key]: value } }));

  // Switch between a static value and a connection-backed secret. Reset the
  // branch-specific state so a stale connection/type never leaks across.
  const changeSource = (source: SecretSource) =>
    setForm((f) => ({
      ...f,
      source,
      type: source === "static" ? (f.type === "" ? "generic" : f.type) : f.type,
      connectionRef: source === "connection" ? f.connectionRef : "",
    }));

  const submitForm = (e: React.FormEvent) => {
    e.preventDefault();
    if (form.name.trim().length === 0) {
      notify({
        color: "red",
        title: "Name is required",
        message: "Give the secret a name.",
      });
      return;
    }
    if (form.source === "connection") {
      // The secret must reference a connection — credentials come only from it.
      if (form.connectionRef.length === 0) {
        notify({
          color: "red",
          title: "Select a credential",
          message: "Create a secret-backend credential first, then select it.",
        });
        return;
      }
      // On create, required NON-SENSITIVE selector fields must be provided; on
      // edit a blank value keeps the stored one.
      if (!editTarget) {
        const missing = selectorFields.filter(
          (spec) =>
            spec.required &&
            (form.external[spec.key] ?? "").trim().length === 0,
        );
        if (missing.length > 0) {
          notify({
            color: "red",
            title: `Missing ${typeLabel(connectionType)} fields`,
            message: `Required: ${missing.map((m) => m.label).join(", ")}.`,
          });
          return;
        }
      }
    }
    saveMutation.mutate();
  };

  const secrets = secretsQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  const columns: Column<Secret>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      accessor: (s) => s.name,
      cell: (s) => (
        <>
          <Text weight="medium">{s.name}</Text>
          {s.deploymentId ? (
            <Text size="xs" color="muted">
              Secret variables of{" "}
              <Link to={appHref(`dep~${s.deploymentId}`, "config")} className="cell-link" onClick={(e) => e.stopPropagation()}>
                {depNames.get(s.deploymentId) ?? "a workload"}
              </Link>
              ; deleted with it
            </Text>
          ) : s.description ? (
            <Text size="xs" color="muted">
              {s.description}
            </Text>
          ) : null}
        </>
      ),
    },
    {
      key: "type",
      header: "Type",
      sortable: true,
      accessor: (s) => s.type,
      // The secret's type doubles as its provider (generic/token/vault/…).
      filter: { kind: "select", options: typeOptions },
      filterFn: (s, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(s.type),
      cell: (s) => (
        <Badge
          variant="light"
          color={toKitColor(TYPE_COLORS[s.type] ?? "gray")}
        >
          {s.type}
        </Badge>
      ),
    },
    {
      key: "fields",
      header: "Fields",
      sortable: true,
      accessor: (s) => s.fields.length,
      cell: (s) => <Text size="sm">{s.fields.length}</Text>,
    },
    {
      key: "updated",
      header: "Updated",
      sortable: true,
      accessor: (s) => s.updatedAt,
      cell: (s) => formatDate(s.updatedAt),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (s) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(s)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (secret: Secret) => (
    <Box direction="row" gap={2} justify="end" wrap={false}>
      <Tooltip content="Reveal values">
        <Button
          variant="clear"
          color="yellow"
          iconLeft="visibility-outline-rounded"
          onClick={() => openReveal(secret)}
          aria-label={`Reveal ${secret.name}`}
        />
      </Tooltip>
      <Tooltip content="Edit secret">
        <Button
          variant="clear"
          color="blue"
          iconLeft="edit-outline-rounded"
          onClick={() => openEdit(secret)}
          aria-label={`Edit ${secret.name}`}
        />
      </Tooltip>
      <Tooltip content={`Delete ${secret.name}`}>
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(secret)}
          aria-label={`Delete ${secret.name}`}
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
            New secret
          </Button>
        </Box>
      ) : (
        <PageHeader
          title="Secrets"
          description="Store encrypted credentials referenced across the platform."
          icon={<Icon name="key-outline-rounded" size="lg" />}
          actions={
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={openCreate}
            >
              New secret
            </Button>
          }
        />
      )}

      <Callout variant="light" color="blue" title="Referencing secrets">
        Reference a secret anywhere a value is interpolated (deployment env,
        compose YAML, workflow steps) with <Code>[[name]]</Code> or{" "}
        <Code>[[name.field]]</Code>. <Code>[[name]]</Code> resolves to the{" "}
        <Code>value</Code> field, or the sole field when a secret has exactly
        one. Make the source explicit with{" "}
        <Code>[[secret::name]]</Code> (any stored secret) or{" "}
        <Code>[[plugin::name.field]]</Code> (requires an external,
        plugin-backed secret like Vault/Infisical). Use the resolver below to
        preview what a reference resolves to.
      </Callout>

      <SectionCard
        plain
        title="Secrets"
        icon={<Icon name="key-outline-rounded" />}
        actions={
          !secretsQuery.isLoading && !secretsQuery.isError ? (
            <Text size="sm" color="muted">
              {secrets.length} {secrets.length === 1 ? "secret" : "secrets"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {secretsQuery.isError ? (
          <Box padding={4}>
            <Callout
              color="danger"
              iconLeft={<Icon name="warning-outline-rounded" />}
              title="Failed to load secrets"
            >
              {errorMessage(
                secretsQuery.error,
                "Could not load the secret list.",
              )}
            </Callout>
          </Box>
        ) : (
          <Table<Secret>
            data={secrets}
            columns={columns}
            rowKey={(s) => s.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={secretsQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={
                  <Icon name="key-outline-rounded" size="xl" />
                }
                title="No secrets yet"
                description="Create a secret to store encrypted credentials referenced across the platform."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                  >
                    New secret
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      <SectionCard
        title="Resolve references"
        icon={<Icon name="link-rounded" />}
        description="Preview what [[…]] tokens resolve to. Values are masked; external/plugin-backed secrets are fetched live, so this also checks backend connectivity."
      >
        <Box direction="column" gap={2}>
          <TextArea
            placeholder={"Paste text with references, e.g.\nDATABASE_URL=[[db.url]]\nTOKEN=[[plugin::myvault.api_key]]"}
            rows={3}
            value={refText}
            onChange={(e) => setRefText(e.target.value)}
            style={{ fontFamily: "monospace" }}
          />
          {!hasTokens ? (
            <Text size="sm" color="muted">
              No <Code>[[…]]</Code> tokens yet. Type a reference above to preview
              it live.
            </Text>
          ) : previewQuery.isFetching ? (
            <Box direction="row" gap={2} align="center">
              <Spinner size="xs" />
              <Text size="sm" color="muted">
                Resolving…
              </Text>
            </Box>
          ) : previewQuery.isError ? (
            <Callout
              color="danger"
              variant="light"
              iconLeft={<Icon name="warning-outline-rounded" />}
            >
              {errorMessage(previewQuery.error, "Could not resolve references.")}
            </Callout>
          ) : (previewQuery.data ?? []).length === 0 ? (
            <Text size="sm" color="muted">
              No recognized reference tokens found.
            </Text>
          ) : (
            <ReferencePreviewTable refs={previewQuery.data ?? []} />
          )}
        </Box>
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
              {editTarget ? `Edit ${editTarget.name}` : "New secret"}
            </Popup.Title>
          </Popup.Header>
          <form onSubmit={submitForm}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <TextInput
                  label="Name"
                  placeholder="my-secret"
                  description="Referenced as [[name]] or [[name.field]]."
                  autoFocus
                  value={form.name}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, name: e.target.value }))
                  }
                />
                <Select
                  label="Source"
                  description="Type a value here, or fetch it live from a connection (Vault, Infisical, …)."
                  options={[
                    { value: "static", label: "Static value" },
                    { value: "connection", label: "From a connection" },
                  ]}
                  value={form.source}
                  onChange={(v: string) =>
                    changeSource((v as SecretSource) ?? "static")
                  }
                  // Static/connection layout is structural; keep it fixed on edit
                  // so the stored kind (value vs backend) can't be flipped.
                  disabled={editTarget !== null}
                />
                {form.source === "static" ? (
                  <Select
                    label="Type"
                    description="Static types hold their value directly."
                    options={STATIC_SECRET_TYPES}
                    value={form.type}
                    onChange={(v: string) =>
                      setForm((f) => ({ ...f, type: v ?? "generic" }))
                    }
                  />
                ) : null}
                <TextArea
                  label="Description"
                  placeholder="What this secret is used for"
                  rows={1}
                  value={form.description}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, description: e.target.value }))
                  }
                />

                {form.source === "connection" ? (
                  <Box direction="column" gap={2}>
                    <Text size="xs" color="muted">
                      The value is fetched live from the connection each time the
                      secret is referenced as{" "}
                      <Code>[[{form.name.trim() || "name"}]]</Code>; nothing is
                      stored statically. <Code>[[name.field]]</Code> selects a
                      different key/secret. Credentials come from the connection —
                      only the per-secret selectors are entered here.
                    </Text>

                    {/* Reference-only: select a connection, never enter creds. */}
                    <ConnectionField
                      capability="secretFetch"
                      value={form.connectionRef}
                      onChange={(ref) =>
                        setForm((f) => ({ ...f, connectionRef: ref }))
                      }
                      isEdit={!!editTarget}
                    />

                    {editTarget ? (
                      <Text size="xs" color="muted">
                        Leave a selector blank to keep the currently stored value.
                      </Text>
                    ) : null}

                    {/* Per-secret NON-SENSITIVE selector fields (mount/path/key)
                        from the selected connection's type. Rendered only once a
                        connection is chosen. Sensitive backend fields
                        (address/token) are NEVER shown — they live in the
                        connection. */}
                    {form.connectionRef.length > 0
                      ? selectorFields.map((spec) => (
                          <TextInput
                            key={spec.key}
                            label={spec.label}
                            placeholder={spec.placeholder}
                            required={(spec.required ?? false) && !editTarget}
                            value={form.external[spec.key] ?? ""}
                            onChange={(e) =>
                              updateExternal(spec.key, e.target.value)
                            }
                          />
                        ))
                      : null}
                  </Box>
                ) : (
                  <KeyValueEditor
                    label="Fields"
                    addLabel="Add field"
                    emptyText="No fields. Add at least one."
                    removeAriaLabel="Remove field"
                    keyPlaceholder="field"
                    valueType="password"
                    valueAutoComplete="new-password"
                    valuePlaceholder={editTarget ? "(unchanged)" : "value"}
                    rows={form.rows}
                    onAdd={addRow}
                    onUpdate={updateRow}
                    onRemove={removeRow}
                    hint={
                      editTarget ? (
                        <Text
                          size="xs"
                          color="muted"
                          style={{ marginBottom: 8 }}
                        >
                          Leave a value blank to keep the currently stored value.
                          Field values are never displayed here.
                        </Text>
                      ) : (
                        <Text
                          size="xs"
                          color="muted"
                          style={{ marginBottom: 8 }}
                        >
                          Use a field named <Code>value</Code> for the simple{" "}
                          <Code>[[name]]</Code> form.
                        </Text>
                      )
                    }
                  />
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
                <Button color="info" type="submit" loading={saveMutation.isPending}>
                  {editTarget ? "Save" : "Create"}
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      {/* Reveal modal */}
      <Popup
        open={revealTarget !== null}
        onOpenChange={(open) => {
          if (!open) closeReveal();
        }}
        size="lg"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {revealTarget ? `Reveal — ${revealTarget.name}` : "Reveal secret"}
            </Popup.Title>
          </Popup.Header>
          <Popup.Body>
            <Box direction="column" gap={4}>
              <Callout variant="light" color="yellow" title="Sensitive values">
                These decrypted values are shown only to you and the access is
                audited. Do not share them.
              </Callout>
              {revealMutation.isPending || revealed === null ? (
                <Box direction="row" justify="center" py={4}>
                  <Spinner />
                </Box>
              ) : Object.keys(revealed).length === 0 ? (
                <Text color="muted">This secret has no fields.</Text>
              ) : (
                <Table bordered>
                  <Table.Header>
                    <Table.Row>
                      <Table.Head>Field</Table.Head>
                      <Table.Head>Value</Table.Head>
                      <Table.Head />
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {Object.entries(revealed).map(([key, value]) => (
                      <RevealRow key={key} field={key} value={value} />
                    ))}
                  </Table.Body>
                </Table>
              )}
            </Box>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" justify="end">
              <Button variant="outline" color="neutral" onClick={closeReveal}>
                Close
              </Button>
            </Box>
          </Popup.Footer>
        </Popup.Content>
      </Popup>

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete secret"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? Anything referencing <Code>[[{deleteTarget?.name}]]</Code> will no
            longer resolve. This cannot be undone.
            {deleteTarget?.deploymentId && (
              <>
                {" "}It holds the secret variables of{" "}
                <Text as="span" weight="semibold">{depNames.get(deleteTarget.deploymentId) ?? "a workload"}</Text>, which
                then needs their values typed again.
              </>
            )}
          </>
        }
      />
    </Box>
  );
}

/** One reveal-table row with its own copy state. */
function RevealRow({ field, value }: { field: string; value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void copyText(value).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <Table.Row>
      <Table.Cell>
        <Code>{field}</Code>
      </Table.Cell>
      <Table.Cell>
        <Code
          style={{
            maxWidth: 360,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            display: "inline-block",
            verticalAlign: "middle",
          }}
        >
          {value}
        </Code>
      </Table.Cell>
      <Table.Cell>
        <Tooltip content={copied ? "Copied" : "Copy value"}>
          <Button
            variant="clear"
            color={copied ? "green" : "neutral"}
            iconLeft={
              copied
                ? "check-rounded"
                : "content-copy-outline-rounded"
            }
            aria-label={`Copy ${field}`}
            onClick={copy}
          />
        </Tooltip>
      </Table.Cell>
    </Table.Row>
  );
}

/**
 * Renders the masked, per-token outcome of resolving [[…]] references. Resolved
 * values are shown only as a masked preview + length; the source prefix and
 * matched secret type are surfaced so a mismatch (e.g. plugin:: on a static
 * secret) is obvious.
 */
function ReferencePreviewTable({ refs }: { refs: ReferencePreview[] }) {
  return (
    <Table hoverable scrollable>
      <Table.Header>
        <Table.Row>
          <Table.Head>Reference</Table.Head>
          <Table.Head>Source</Table.Head>
          <Table.Head>Resolves to</Table.Head>
          <Table.Head>Status</Table.Head>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {refs.map((ref) => (
          <Table.Row key={ref.token}>
            <Table.Cell>
              <Code>{ref.token}</Code>
            </Table.Cell>
            <Table.Cell>
              <Box direction="row" gap={2} wrap={false}>
                <Badge
                  size="sm"
                  variant="light"
                  color={ref.source ? "blue" : "gray"}
                >
                  {ref.source || "implicit"}
                </Badge>
                {ref.secretType ? (
                  <Badge
                    size="sm"
                    variant="light"
                    color={toKitColor(TYPE_COLORS[ref.secretType] ?? "gray")}
                  >
                    {ref.secretType}
                  </Badge>
                ) : null}
                {ref.external ? (
                  <Badge size="sm" variant="outline" color="info">
                    live
                  </Badge>
                ) : null}
              </Box>
            </Table.Cell>
            <Table.Cell>
              {ref.status === "resolved" ? (
                <Box direction="row" gap={2} wrap={false} align="center">
                  <Code>{ref.masked || "(empty)"}</Code>
                  {ref.length ? (
                    <Text size="xs" color="muted">
                      {ref.length} chars
                    </Text>
                  ) : null}
                </Box>
              ) : (
                <Text size="xs" color="muted">
                  {ref.message ?? "—"}
                </Text>
              )}
            </Table.Cell>
            <Table.Cell>
              <Badge
                variant="light"
                color={toKitColor(REF_STATUS_COLOR[ref.status])}
                iconLeft={
                  ref.status === "resolved"
                    ? "check-rounded"
                    : ref.status === "error"
                      ? "close-rounded"
                      : undefined
                }
              >
                {ref.status}
              </Badge>
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
}
