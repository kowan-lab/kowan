import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Checkbox,
  Code,
  Icon,
  Popup,
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
import { useNavigate } from "react-router";
import {
  createRegistry,
  deleteRegistry,
  listConnections,
  listRegistries,
  testRegistry,
  updateRegistry,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import ConnectionField from "../components/ConnectionField";
import { DeployRegistryDialog } from "../components/registries/DeployRegistryDialog";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import { SLOW_REFETCH_MS } from "../lib/live";
import { notify } from "../ui/toast";
import { toKitColor } from "../ui/colors";
import type { Connection, Registry, RegistryProviderType } from "../types";
import { errorMessage, formatDate } from "../utils";


const REGISTRY_TYPES: {
  value: RegistryProviderType;
  label: string;
  beta?: boolean;
}[] = [
  { value: "generic", label: "Generic (Docker v2)" },
  { value: "ghcr", label: "GitHub Container Registry" },
  { value: "ecr", label: "Amazon ECR", beta: true },
  { value: "gcp", label: "Google Artifact Registry", beta: true },
  { value: "acr", label: "Azure Container Registry", beta: true },
];

const TYPE_COLORS: Record<string, string> = {
  generic: "gray",
  ghcr: "gray",
  ecr: "orange",
  gcp: "blue",
  acr: "cyan",
};

/** Provider types still marked beta (cloud token exchange, not live-tested). */
const BETA_TYPES = new Set<string>(["ecr", "gcp", "acr"]);

function typeLabel(type: string): string {
  return REGISTRY_TYPES.find((t) => t.value === type)?.label ?? type;
}

function isBeta(type: string): boolean {
  return BETA_TYPES.has(type);
}

/**
 * Auth is a property of the CONNECTION a registry references, not of the
 * registry itself: the connection's type decides the provider (a `aws`
 * connection makes it an ECR registry, exchanging IAM creds for a short-lived
 * ECR token on every pull/push — no manual `docker login`). So the type/auth the
 * UI shows is DERIVED from that connection. This maps a dockerRegistryAuth
 * connection type to its display label, color and a URL hint.
 */
const CONN_TYPE_META: Record<
  string,
  { label: string; color: string; urlHint: string; cloud?: boolean }
> = {
  generic: { label: "Generic (Docker v2)", color: "gray", urlHint: "docker.io, registry.example.com:5000" },
  ghcr: { label: "GitHub Container Registry", color: "gray", urlHint: "ghcr.io" },
  aws: { label: "Amazon ECR", color: "orange", urlHint: "<account>.dkr.ecr.<region>.amazonaws.com", cloud: true },
  gcp: { label: "Google Artifact Registry", color: "blue", urlHint: "<region>-docker.pkg.dev (or gcr.io)", cloud: true },
  azure: { label: "Azure Container Registry", color: "cyan", urlHint: "<registry>.azurecr.io", cloud: true },
};

function connMeta(type: string) {
  return (
    CONN_TYPE_META[type] ?? {
      label: type || "Generic",
      color: "gray",
      urlHint: "docker.io, registry.example.com:5000",
    }
  );
}

/**
 * Resolve how a registry authenticates for display: from its referenced
 * connection when set, else the registry's own type (a registry with inline
 * auth).
 */
function authMeta(
  registry: Registry,
  connById: Map<string, Connection>,
): { label: string; color: string; cloud: boolean; connName: string | null } {
  const conn = registry.connectionRef
    ? connById.get(registry.connectionRef)
    : undefined;
  if (conn) {
    const m = connMeta(conn.type);
    return { label: m.label, color: m.color, cloud: !!m.cloud, connName: conn.name };
  }
  const t = registry.type || "generic";
  return {
    label: typeLabel(t),
    color: TYPE_COLORS[t] ?? "gray",
    cloud: isBeta(t),
    connName: null,
  };
}

interface RegistryFormState {
  name: string;
  /** The host, without a scheme: `plainHttp` decides it. */
  url: string;
  /** Reached over plain HTTP (stored as an `http://` URL), not HTTPS. */
  plainHttp: boolean;
  /**
   * The shared connection (by id) whose type implements `dockerRegistryAuth`.
   * Credentials live only in the connection — the registry just references it.
   * Empty for a public registry: pulls are anonymous.
   */
  connectionRef: string;
}

function emptyForm(): RegistryFormState {
  return {
    name: "",
    url: "",
    plainHttp: false,
    connectionRef: "",
  };
}

const stripScheme = (url: string) => url.replace(/^[a-z]+:\/\//i, "");

/** Seed the form for editing: name, url and the referenced connection. */
function formFromRegistry(registry: Registry): RegistryFormState {
  return {
    name: registry.name,
    url: stripScheme(registry.url),
    plainHttp: /^http:\/\//i.test(registry.url),
    connectionRef: registry.connectionRef ?? "",
  };
}

/** The URL to store: the host, with `http://` when it answers in plain HTTP. */
function formUrl(form: RegistryFormState): string {
  const host = stripScheme(form.url.trim());
  return form.plainHttp ? `http://${host}` : host;
}

export default function Registries({ embedded }: { embedded?: boolean } = {}) {
  const { user: currentUser } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const isAdmin = currentUser?.role === "admin";

  const [formOpen, setFormOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<Registry | null>(null);
  const [form, setForm] = useState<RegistryFormState>(emptyForm());
  const [deleteTarget, setDeleteTarget] = useState<Registry | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  // Errors shown in the form: a missing field, or the connection check that
  // runs on save (the registry is then not saved unless asked to anyway).
  const [formErrors, setFormErrors] = useState<{ name?: string; url?: string; check?: string }>({});
  const editForm = (patch: Partial<RegistryFormState>) => {
    setForm((f) => ({ ...f, ...patch }));
    setFormErrors({});
  };
  const [deployOpen, setDeployOpen] = useState(false);

  const registriesQuery = useQuery({
    queryKey: ["registries"],
    queryFn: listRegistries,
    enabled: isAdmin,
    refetchInterval: SLOW_REFETCH_MS,
  });

  // The registries' auth is defined by the connections they reference, so we load
  // connections too and index them by id to derive each registry's type/auth.
  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
    enabled: isAdmin,
  });
  const connById = new Map(
    (connectionsQuery.data ?? []).map((c) => [c.id, c]),
  );
  const selectedConn = form.connectionRef
    ? connById.get(form.connectionRef)
    : undefined;

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["registries"] });

  const saveMutation = useMutation({
    mutationFn: ({ verify }: { verify: boolean }) => {
      // Auth comes from the referenced connection, or none for a public
      // registry: send name + url + the connection id, no inline credentials.
      // With verify the server checks the registry first and saves only if
      // it answers.
      if (editTarget) {
        return updateRegistry(editTarget.id, {
          name: form.name.trim(),
          url: formUrl(form),
          connectionRef: form.connectionRef,
        }, { verify });
      }
      return createRegistry({
        name: form.name.trim(),
        url: formUrl(form),
        type: "generic",
        connectionRef: form.connectionRef,
      }, { verify });
    },
    onSuccess: (saved) => {
      notify({
        color: "green",
        title: editTarget ? "Registry updated" : "Registry created",
        message: saved.verified
          ? `${saved.name} was saved and verified.`
          : `${saved.name} was saved.${
              saved.lastError ? ` Validation: ${saved.lastError}` : ""
            }`,
      });
      setFormOpen(false);
      setEditTarget(null);
      setForm(emptyForm());
      setFormErrors({});
      void invalidate();
    },
    onError: (err) => {
      if ((err as { response?: { status?: number } })?.response?.status === 422) {
        setFormErrors({ check: errorMessage(err, "The registry did not answer.") });
        return;
      }
      notify({
        color: "red",
        title: editTarget ? "Could not update registry" : "Could not create registry",
        message: errorMessage(err, "Failed to save registry"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteRegistry(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.name ?? id;
      notify({
        color: "green",
        title: "Registry deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete registry",
        message: errorMessage(err, "Failed to delete registry"),
      });
    },
  });

  const testMutation = useMutation({
    mutationFn: (registry: Registry) => {
      setTestingId(registry.id);
      return testRegistry(registry.id);
    },
    onSuccess: (result, registry) => {
      setTestingId(null);
      if (result.verified) {
        notify({
          color: result.insecure ? "yellow" : "green",
          title: "Registry reachable",
          message: result.insecure
            ? `${registry.name} is reachable but insecure (plain HTTP or untrusted TLS).`
            : `${registry.name} was verified successfully.`,
        });
      } else {
        notify({
          color: "red",
          title: "Validation failed",
          message: result.error || `Could not reach ${registry.name}.`,
        });
      }
      void invalidate();
    },
    onError: (err) => {
      setTestingId(null);
      notify({
        color: "red",
        title: "Test failed",
        message: errorMessage(err, "Could not run the registry test."),
      });
    },
  });

  const registries = registriesQuery.data ?? [];

  // Select-filter options for the Type facet, derived from each registry's
  // connection-resolved display label (data-mode needs explicit options, unlike
  // the old DataTable which auto-faceted).
  const typeOptions = useMemo(
    () =>
      Array.from(
        new Set(
          registries.map((r) => authMeta(r, connById).label).filter(Boolean),
        ),
      )
        .sort()
        .map((v) => ({ value: v, label: v })),
    [registries, connById],
  );

  if (!isAdmin) {
    return (
      <Box direction="column" gap={6}>
        {!embedded && (
          <PageHeader
            title="Registries"
            description="Credentials used to authenticate private image pulls."
            icon={<Icon name="database-outline" size="lg" />}
          />
        )}
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage registries.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    setEditTarget(null);
    setForm(emptyForm());
    setFormErrors({});
    setFormOpen(true);
  };

  const openEdit = (registry: Registry) => {
    setEditTarget(registry);
    setForm(formFromRegistry(registry));
    setFormErrors({});
    setFormOpen(true);
  };

  const submitForm = (e: React.FormEvent) => {
    e.preventDefault();
    const errors = {
      name: form.name.trim() ? undefined : "Give the registry a name.",
      url: stripScheme(form.url.trim()) ? undefined : "Enter the registry host.",
    };
    if (errors.name || errors.url) {
      setFormErrors(errors);
      return;
    }
    saveMutation.mutate({ verify: true });
  };

  const columns: Column<Registry>[] = [
    {
      key: "name",
      header: "Name",
      // Name links to the explorer — same destination as the "explore" action.
      type: "link",
      sortable: true,
      accessor: (r) => r.name,
      href: (r) => `/registry-explorer?id=${r.id}`,
      link: {
        color: "info",
        onClick: (r, e) => {
          e.preventDefault();
          navigate(`/registry-explorer?id=${r.id}`);
        },
      },
    },
    {
      key: "type",
      header: "Type",
      // Type is DERIVED from the referenced connection, so an ECR-backed
      // registry reads "Amazon ECR", not "generic".
      sortable: true,
      accessor: (r) => authMeta(r, connById).label,
      filter: { kind: "select", options: typeOptions },
      filterFn: (r, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(authMeta(r, connById).label),
      cell: (r) => {
        const auth = authMeta(r, connById);
        return (
          <Box direction="row" wrap={false} gap={2} align="center">
            <Badge variant="light" color={toKitColor(auth.color, "gray")}>
              {auth.label}
            </Badge>
            {auth.cloud && (
              <Badge variant="outline" color="yellow" size="sm">
                beta
              </Badge>
            )}
          </Box>
        );
      },
    },
    {
      key: "url",
      header: "URL",
      sortable: true,
      accessor: (r) => r.url,
      cell: (r) => <Code>{r.url}</Code>,
    },
    {
      key: "auth",
      header: "Auth",
      // The credential (connection) that authenticates this registry.
      cell: (r) => {
        const auth = authMeta(r, connById);
        return auth.connName ? (
          <Badge
            variant="light"
            color="magenta"
            iconLeft="key-outline-rounded"
          >
            {auth.connName}
          </Badge>
        ) : r.username ? (
          <Text size="sm">{r.username}</Text>
        ) : (
          <StatusBadge
            status={r.hasPassword ? "enabled" : "none"}
            value={r.hasPassword ? "Token" : "Anonymous"}
          />
        );
      },
    },
    {
      key: "status",
      header: "Status",
      cell: (r) => (
        <Box direction="row" wrap={false} gap={2} align="center">
          {r.verified ? (
            <Tooltip content="Verified — credentials work">
              <Badge
                variant="light"
                color="green"
                iconLeft="check-circle-outline-rounded"
              >
                Verified
              </Badge>
            </Tooltip>
          ) : (
            <Tooltip content={r.lastError || "Not verified yet — run a test"}>
              <Badge
                variant="light"
                color="red"
                iconLeft="cancel-outline-rounded"
              >
                Unverified
              </Badge>
            </Tooltip>
          )}
          {r.insecure && (
            <Tooltip content="Plain HTTP or untrusted/self-signed TLS certificate">
              <Badge
                variant="light"
                color="yellow"
                iconLeft="gpp-maybe-outline-rounded"
              >
                Insecure
              </Badge>
            </Tooltip>
          )}
        </Box>
      ),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (r) => r.createdAt,
      cell: (r) => formatDate(r.createdAt),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (r) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(r)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (registry: Registry) => (
    <Box direction="row" wrap={false} gap={2} justify="end" align="center">
      <Tooltip content="Explore repositories & tags">
        <Button
          variant="clear"
          color="magenta"
          iconLeft="visibility-outline-rounded"
          onClick={() => navigate(`/registry-explorer?id=${registry.id}`)}
          aria-label={`Explore ${registry.name}`}
        />
      </Tooltip>
      <Tooltip content="Test connectivity & credentials">
        <Button
          variant="clear"
          color="green"
          loading={testMutation.isPending && testingId === registry.id}
          iconLeft="power-outline-rounded"
          onClick={() => testMutation.mutate(registry)}
          aria-label={`Test ${registry.name}`}
        />
      </Tooltip>
      <Tooltip content="Edit registry">
        <Button
          variant="clear"
          color="blue"
          iconLeft="edit-outline-rounded"
          onClick={() => openEdit(registry)}
          aria-label={`Edit ${registry.name}`}
        />
      </Tooltip>
      <Tooltip content={`Delete ${registry.name}`}>
        <Button
          variant="clear"
          color="red"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(registry)}
          aria-label={`Delete ${registry.name}`}
        />
      </Tooltip>
    </Box>
  );

  let tableBody: React.ReactNode;
  if (registriesQuery.isError) {
    tableBody = (
      <Box padding={4}>
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Failed to load registries"
        >
          {errorMessage(registriesQuery.error, "Could not load the registry list.")}
        </Callout>
      </Box>
    );
  } else {
    tableBody = (
      <Table<Registry>
        data={registries}
        columns={columns}
        rowKey={(r) => r.id}
        globalSearch
        pagination
        defaultPageSize={50}
        pageSizeOptions={[10, 25, 50, 100]}
        loading={registriesQuery.isLoading}
        stickyHeader
        striped
        hoverable
        emptyState={
          <EmptyState
            icon={<Icon name="database-outline" size="xl" />}
            title="No registries yet"
            description="Add a registry to authenticate private image pulls."
            action={
              <Button color="info" iconLeft="add-rounded" onClick={openCreate}>
                New registry
              </Button>
            }
          />
        }
      />
    );
  }

  const deployButton = (
    <Button variant="outline" color="neutral" iconLeft="rocket-launch-outline-rounded" onClick={() => setDeployOpen(true)}>
      Deploy a registry
    </Button>
  );

  return (
    <Box direction="column" gap={6}>
      {embedded ? (
        <Box direction="row" justify="end" gap={2} style={{ marginBottom: 16 }}>
          {deployButton}
          <Button color="info" iconLeft="add-rounded" onClick={openCreate}>
            New registry
          </Button>
        </Box>
      ) : (
        <PageHeader
          title="Registries"
          description="Credentials used to authenticate private image pulls."
          icon={<Icon name="database-outline" size="lg" />}
          actions={
            <>
              {deployButton}
              <Button color="info" iconLeft="add-rounded" onClick={openCreate}>
                New registry
              </Button>
            </>
          }
        />
      )}

      <SectionCard
        plain
        title="Registries"
        icon={<Icon name="database-outline" />}
        actions={
          !registriesQuery.isLoading && !registriesQuery.isError ? (
            <Text size="sm" color="muted">
              {registries.length}{" "}
              {registries.length === 1 ? "registry" : "registries"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {tableBody}
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
              {editTarget ? `Edit ${editTarget.name}` : "New registry"}
            </Popup.Title>
          </Popup.Header>
          <form onSubmit={submitForm}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <TextInput
                  label="Name"
                  placeholder="My private registry"
                  data-autofocus
                  value={form.name}
                  errorText={formErrors.name}
                  onChange={(e) => editForm({ name: e.target.value })}
                />
                <TextInput
                  label="Registry host"
                  description={
                    selectedConn
                      ? `e.g. ${connMeta(selectedConn.type).urlHint}`
                      : "e.g. docker.io, ghcr.io, registry.example.com:5000"
                  }
                  placeholder="ghcr.io"
                  value={form.url}
                  errorText={formErrors.url}
                  onChange={(e) => {
                    // A pasted http:// URL ticks the box; the field keeps the host.
                    const url = e.target.value;
                    editForm(/^https?:\/\//i.test(url) ? { url: stripScheme(url), plainHttp: /^http:/i.test(url) } : { url });
                  }}
                />
                <Checkbox
                  label="Plain HTTP"
                  description="The registry answers over HTTP, not HTTPS. Docker pulls from it only when the host lists it in insecure-registries."
                  checked={form.plainHttp}
                  onChange={(e) => editForm({ plainHttp: e.target.checked })}
                />

                <ConnectionField
                  capability="dockerRegistryAuth"
                  value={form.connectionRef}
                  onChange={(ref) => editForm({ connectionRef: ref })}
                  isEdit={!!editTarget}
                  optional
                  description="None for a public registry: pulls are anonymous."
                />
                {selectedConn && (
                  <Callout variant="light" color="blue">
                    Auth type <strong>{connMeta(selectedConn.type).label}</strong>{" "}
                    — derived from the selected credential. The provider handles
                    the login automatically (for AWS/GCP/Azure it exchanges the
                    cloud identity for a short-lived registry token on each
                    pull/push; no manual <Code>docker login</Code>).
                  </Callout>
                )}
                {formErrors.check && (
                  <Callout
                    variant="light"
                    color="danger"
                    iconLeft={<Icon name="warning-outline-rounded" />}
                    title="The registry did not answer"
                  >
                    <Box direction="column" gap={2} align="start">
                      <span>{formErrors.check}</span>
                      <Button
                        variant="outline"
                        color="neutral"
                        size="sm"
                        onClick={() => saveMutation.mutate({ verify: false })}
                        loading={saveMutation.isPending}
                      >
                        Save without checking
                      </Button>
                    </Box>
                  </Callout>
                )}
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap justify="end" gap={2}>
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

      {deployOpen && <DeployRegistryDialog open={deployOpen} onClose={() => setDeployOpen(false)} />}

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete registry"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? Image pulls relying on these credentials will fall back to anonymous
            access.
          </>
        }
      />
    </Box>
  );
}
