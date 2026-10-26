import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
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
import { createScopedApiKey, deleteApiKey, listApiKeys, listTeams } from "../api/client";
import GrantFields, { EMPTY_GRANT, GrantBadge, describeGrant, grantValid, useScopeNames } from "../auth/GrantFields";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import type { GrantInput, ScopedAPIKey as APIKey } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";
import { toKitColor } from "../ui/colors";
import { SLOW_REFETCH_MS } from "../lib/live";

const ROLE_OPTIONS = [
  { value: "admin", label: "Admin" },
  { value: "operator", label: "Operator" },
  { value: "viewer", label: "Viewer" },
];

function roleColor(role: string): string {
  switch (role) {
    case "admin":
      return "red";
    case "operator":
      return "blue";
    case "viewer":
      return "gray";
    default:
      return "gray";
  }
}


interface CreateValues {
  name: string;
  role: string;
  /** "" = all resources the role allows; "team" = act as a team; "scopes" = narrowed grants. */
  binding: "" | "team" | "scopes";
  teamId: string;
  scopes: GrantInput[];
}

const EMPTY_CREATE: CreateValues = { name: "", role: "operator", binding: "", teamId: "", scopes: [] };

export default function ApiKeys() {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();

  const isAdmin = currentUser?.role === "admin";

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<APIKey | null>(null);
  // The full secret is returned only once at creation; hold it here to show in
  // a one-time reveal modal, then drop it from memory when the modal closes.
  const [createdSecret, setCreatedSecret] = useState<{
    key: APIKey;
    secret: string;
  } | null>(null);

  // Plain controlled form state (replaces the previous form-library hook).
  const [createValues, setCreateValues] = useState<CreateValues>(EMPTY_CREATE);
  const [draftScope, setDraftScope] = useState<GrantInput>(EMPTY_GRANT);
  const names = useScopeNames();
  const [createError, setCreateError] = useState<string | null>(null);

  const resetCreateForm = () => {
    setCreateValues(EMPTY_CREATE);
    setDraftScope(EMPTY_GRANT);
    setCreateError(null);
  };

  const validateCreate = (values: CreateValues): string | null => {
    if (values.name.trim().length === 0) return "Name is required";
    if (!values.role) return "Role is required";
    if (values.binding && values.role === "admin") return "A team-bound or scoped key cannot be an admin key";
    if (values.binding === "team" && !values.teamId) return "Pick a team";
    if (values.binding === "scopes" && values.scopes.length === 0) return "Add at least one scope";
    return null;
  };

  // One-time secret copy state.
  const [secretCopied, setSecretCopied] = useState(false);
  const copySecret = () => {
    if (!createdSecret) return;
    void navigator.clipboard.writeText(createdSecret.secret).then(() => {
      setSecretCopied(true);
      window.setTimeout(() => setSecretCopied(false), 1500);
    });
  };

  const keysQuery = useQuery({
    queryKey: ["apikeys"],
    queryFn: listApiKeys,
    enabled: isAdmin,
    refetchInterval: SLOW_REFETCH_MS,
  });
  const teamsQuery = useQuery({ queryKey: ["teams"], queryFn: listTeams, enabled: isAdmin });
  const teamName = (id: string) => teamsQuery.data?.find((t) => t.id === id)?.name ?? id;

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["apikeys"] });

  const createMutation = useMutation({
    mutationFn: (values: CreateValues) =>
      createScopedApiKey({
        name: values.name,
        role: values.role,
        teamId: values.binding === "team" ? values.teamId : undefined,
        scopes: values.binding === "scopes" ? values.scopes : undefined,
      }),
    onSuccess: (data) => {
      setCreateOpen(false);
      resetCreateForm();
      setCreatedSecret({ key: data.apiKey, secret: data.secret });
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not create API key",
        message: errorMessage(err, "Failed to create API key"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteApiKey(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.name ?? id;
      notify({
        color: "green",
        title: "API key deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete API key",
        message: errorMessage(err, "Failed to delete API key"),
      });
    },
  });

  const keys = keysQuery.data ?? [];

  // Select-filter options derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted). Declared before the
  // early return below so the hook order stays stable.
  const roleOptions = useMemo(
    () =>
      Array.from(new Set(keys.map((key) => key.role).filter(Boolean)))
        .sort()
        .map((v) => ({ value: v, label: v.charAt(0).toUpperCase() + v.slice(1) })),
    [keys],
  );

  if (!isAdmin) {
    return (
      <Box direction="column" gap={5}>
        <PageHeader
          title="API Keys"
          description="Role-scoped keys to authenticate API requests without a login."
          icon={<Icon name="key-outline-rounded" size="lg" />}
        />
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage API keys.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    resetCreateForm();
    setCreateOpen(true);
  };

  const submitCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const err = validateCreate(createValues);
    setCreateError(err);
    if (err) return;
    createMutation.mutate({ ...createValues, name: createValues.name.trim() });
  };

  // --- Table columns + row actions ------------------------------------------
  const columns: Column<APIKey>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      accessor: (key) => key.name,
      cell: (key) => <Text weight="medium">{key.name}</Text>,
    },
    {
      key: "prefix",
      header: "Prefix",
      accessor: (key) => key.prefix,
      cell: (key) => <Code>{key.prefix}…</Code>,
    },
    {
      key: "role",
      header: "Role",
      sortable: true,
      accessor: (key) => key.role,
      filter: { kind: "select", options: roleOptions },
      filterFn: (key, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(key.role),
      cell: (key) => (
        <Badge color={toKitColor(roleColor(key.role))} variant="light">
          {key.role}
        </Badge>
      ),
    },
    {
      key: "access",
      header: "Access",
      accessor: (key) => (key.teamId ? teamName(key.teamId) : key.scopes?.length ? "scoped" : "all"),
      cell: (key) =>
        key.teamId ? (
          <Text>team {teamName(key.teamId)}</Text>
        ) : key.scopes && key.scopes.length > 0 ? (
          <Box direction="column" gap={1}>
            {key.scopes.map((g) => (
              <Box key={g.id} direction="row" gap={2} align="center">
                <GrantBadge level={g.level} />
                <Text size="sm">{describeGrant(g, names.hostName, names.depName)}</Text>
              </Box>
            ))}
          </Box>
        ) : (
          <Text color="muted">everything the role allows</Text>
        ),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (key) => key.createdAt,
      cell: (key) => formatDate(key.createdAt),
    },
    {
      key: "lastUsed",
      header: "Last used",
      sortable: true,
      accessor: (key) => key.lastUsed ?? "",
      cell: (key) =>
        key.lastUsed ? (
          formatDate(key.lastUsed)
        ) : (
          <Text color="muted">Never</Text>
        ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (key) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(key)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (key: APIKey) => (
    <Box direction="row" gap={2} justify="end" wrap={false}>
      <Tooltip content={`Delete ${key.name}`}>
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(key)}
          aria-label={`Delete ${key.name}`}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="API Keys"
        description="Role-scoped keys to authenticate API requests without a login."
        icon={<Icon name="key-outline-rounded" size="lg" />}
        actions={
          <Button
            color="info"
            iconLeft="add-rounded"
            onClick={openCreate}
          >
            New API key
          </Button>
        }
      />

      <Callout variant="light" color="blue" title="Using API keys">
        Authenticate with a key by sending it as the{" "}
        <Code>X-API-Key</Code> header, or with kowanctl via{" "}
        <Code>--api-key</Code> (or the <Code>KOWAN_API_KEY</Code> environment
        variable). The key inherits the role you choose here.
      </Callout>

      <SectionCard
        plain
        title="API keys"
        icon={<Icon name="key-outline-rounded" />}
        actions={
          !keysQuery.isLoading && !keysQuery.isError ? (
            <Text size="sm" color="muted">
              {keys.length} {keys.length === 1 ? "key" : "keys"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {keysQuery.isError ? (
          <Box padding={4}>
            <Callout
              color="danger"
              iconLeft={<Icon name="warning-outline-rounded" />}
              title="Failed to load API keys"
            >
              {errorMessage(keysQuery.error, "Could not load the API key list.")}
            </Callout>
          </Box>
        ) : (
          <Table<APIKey>
            data={keys}
            columns={columns}
            rowKey={(key) => key.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={keysQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="key-outline-rounded" size="xl" />}
                title="No API keys yet"
                description="Create a key to authenticate kowanctl, webhook scripts or CI without a login."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                  >
                    New API key
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      {/* Create API key modal */}
      <Popup
        open={createOpen}
        onOpenChange={(open) => {
          if (!open) setCreateOpen(false);
        }}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>New API key</Popup.Title>
          </Popup.Header>
          <form onSubmit={submitCreate}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <TextInput
                  label="Name"
                  placeholder="ci-deploy"
                  description="A label to recognise this key. The key acts as a synthetic user named key:name."
                  autoFocus
                  value={createValues.name}
                  onChange={(e) =>
                    setCreateValues((v) => ({ ...v, name: e.target.value }))
                  }
                  errorText={createError ?? undefined}
                />
                <Select
                  label="Role"
                  description="The role this key is granted when authenticating."
                  options={ROLE_OPTIONS}
                  value={createValues.role}
                  onChange={(value: string) =>
                    setCreateValues((v) => ({ ...v, role: value }))
                  }
                />
                <Select
                  label="Access"
                  description="Limit what the key reaches; the role stays the ceiling."
                  options={[
                    { value: "", label: "Everything the role allows" },
                    { value: "team", label: "Act as a team" },
                    { value: "scopes", label: "Only specific scopes" },
                  ]}
                  value={createValues.binding}
                  onChange={(value: string) =>
                    setCreateValues((v) => ({ ...v, binding: value as CreateValues["binding"] }))
                  }
                />
                {createValues.binding === "team" && (
                  <Select
                    label="Team"
                    options={(teamsQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
                    value={createValues.teamId || null}
                    onChange={(value: string) => setCreateValues((v) => ({ ...v, teamId: value }))}
                    emptyText="No teams yet — create one on the Teams page."
                  />
                )}
                {createValues.binding === "scopes" && (
                  <Box direction="column" gap={3}>
                    {createValues.scopes.map((g, i) => (
                      <Box key={i} direction="row" gap={2} align="center">
                        <GrantBadge level={g.level} />
                        <Text size="sm">{describeGrant({ scope: g.scope, hostId: g.hostId ?? "", target: g.target ?? "" }, names.hostName, names.depName)}</Text>
                        <Button
                          size="sm"
                          variant="clear"
                          color="danger"
                          iconLeft="delete-outline-rounded"
                          aria-label="Remove scope"
                          onClick={() => setCreateValues((v) => ({ ...v, scopes: v.scopes.filter((_, j) => j !== i) }))}
                        />
                      </Box>
                    ))}
                    <GrantFields value={draftScope} onChange={setDraftScope} />
                    <Box direction="row">
                      <Button
                        size="sm"
                        variant="outline"
                        iconLeft="add-rounded"
                        onClick={() => {
                          const err = grantValid(draftScope);
                          setCreateError(err);
                          if (err) return;
                          setCreateValues((v) => ({ ...v, scopes: [...v.scopes, draftScope] }));
                          setDraftScope(EMPTY_GRANT);
                        }}
                      >
                        Add scope
                      </Button>
                    </Box>
                  </Box>
                )}
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" justify="end" gap={2}>
                <Button
                  variant="outline"
                  color="neutral"
                  onClick={() => setCreateOpen(false)}
                  disabled={createMutation.isPending}
                >
                  Cancel
                </Button>
                <Button color="info" type="submit" loading={createMutation.isPending}>
                  Create
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      {/* One-time secret reveal modal */}
      <Popup
        open={createdSecret !== null}
        onOpenChange={(open) => {
          if (!open) setCreatedSecret(null);
        }}
        size="lg"
        closeOnBackdropClick={false}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {createdSecret
                ? `API key — ${createdSecret.key.name}`
                : "API key created"}
            </Popup.Title>
          </Popup.Header>
          {createdSecret && (
            <>
              <Popup.Body>
                <Box direction="column" gap={4}>
                  <Callout
                    variant="light"
                    color="yellow"
                    iconLeft={
                      <Icon name="warning-outline-rounded" />
                    }
                    title="Copy this key now"
                  >
                    This is the only time the full key is shown. It is stored
                    hashed and cannot be retrieved again — if you lose it, delete
                    this key and create a new one.
                  </Callout>

                  <div>
                    <Box direction="row" justify="between" style={{ marginBottom: 4 }}>
                      <Text size="sm" weight="medium">
                        Secret
                      </Text>
                      <Button
                        size="sm"
                        variant="light"
                        color={secretCopied ? "green" : "info"}
                        iconLeft={
                          secretCopied
                            ? "check-rounded"
                            : "content-copy-outline-rounded"
                        }
                        onClick={copySecret}
                      >
                        {secretCopied ? "Copied" : "Copy"}
                      </Button>
                    </Box>
                    <Code
                      block
                      style={{ wordBreak: "break-all", whiteSpace: "pre-wrap" }}
                    >
                      {createdSecret.secret}
                    </Code>
                  </div>
                </Box>
              </Popup.Body>
              <Popup.Footer>
                <Box direction="row" justify="end">
                  <Button color="info" onClick={() => setCreatedSecret(null)}>Done</Button>
                </Box>
              </Popup.Footer>
            </>
          )}
        </Popup.Content>
      </Popup>

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete API key"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? Any client still using this key will immediately stop
            authenticating. This cannot be undone.
          </>
        }
      />
    </Box>
  );
}
