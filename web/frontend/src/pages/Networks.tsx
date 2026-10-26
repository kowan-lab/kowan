import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Code,
  Icon,
  InputToggle,
  Popup,
  Select,
  Table,
  Text,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { useFeatures } from "../lib/features";
import { useNavigate } from "react-router";
import { notify } from "../ui/toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  connectContainer,
  createNetwork,
  listContainers,
  listHosts,
  listNetworks,
  prune,
  removeNetwork,
} from "../api/client";
import ConfirmModal from "../components/ConfirmModal";
import HostSelectField, {
  defaultTargetHost,
} from "../components/HostSelectField";
import KeyValueEditor, {
  rowsToRecord,
  type KeyValueRow,
} from "../components/KeyValueEditor";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import EmptyState from "../components/EmptyState";
import { useHost, isAllHosts } from "../HostContext";
import { useHostScopedList, type WithHost } from "../useHostScopedList";
import { LIVE_REFETCH_MS } from "../lib/live";
import type { IPAMConfig, Network, NetworkCreateOptions } from "../types";
import { containerName, errorMessage, shortId } from "../utils";

// Row type for the list: a network optionally tagged with the host it came from
// (populated only in the aggregated "All hosts" view).
type NetworkRow = Network & WithHost;

const DRIVER_OPTIONS = [
  { value: "bridge", label: "bridge" },
  { value: "host", label: "host" },
  { value: "overlay", label: "overlay" },
  { value: "macvlan", label: "macvlan" },
  { value: "ipvlan", label: "ipvlan" },
  { value: "none", label: "none" },
];


interface CreateFormValues {
  name: string;
  driver: string;
  subnet: string;
  gateway: string;
  ipRange: string;
  internal: boolean;
  attachable: boolean;
  options: KeyValueRow[];
  labels: KeyValueRow[];
}

const INITIAL_FORM: CreateFormValues = {
  name: "",
  driver: "bridge",
  subnet: "",
  gateway: "",
  ipRange: "",
  internal: false,
  attachable: false,
  options: [],
  labels: [],
};

export default function Networks() {
  const { hostId } = useHost();
  const features = useFeatures();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const aggregated = isAllHosts(hostId);

  // Shared ["hosts"] cache (same key the host-switcher uses) so the modal's
  // HostSelectField and our defaultTargetHost() computation read the same data.
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hosts = hostsQuery.data ?? [];

  const [createOpen, setCreateOpen] = useState(false);
  const [toDelete, setToDelete] = useState<NetworkRow | null>(null);
  const [pruneOpen, setPruneOpen] = useState(false);
  // Target host for the create/prune modals: unlike the per-row actions (which
  // use the row's own host), these global actions need an explicit host, picked
  // inside the modal via <HostSelectField>. Initialised on open.
  const [targetHost, setTargetHost] = useState("");
  const [connectTarget, setConnectTarget] = useState<NetworkRow | null>(null);
  const [connectContainerId, setConnectContainerId] = useState<string | null>(
    null,
  );

  // Plain controlled form state (replaces the previous form hook).
  const [form, setForm] = useState<CreateFormValues>(INITIAL_FORM);
  const [formError, setFormError] = useState<{
    name?: string | null;
    driver?: string | null;
  }>({});

  const setField = <K extends keyof CreateFormValues>(
    key: K,
    value: CreateFormValues[K],
  ) => setForm((prev) => ({ ...prev, [key]: value }));

  const resetForm = () => {
    setForm(INITIAL_FORM);
    setFormError({});
  };

  // Update a single field of a key/value row within options/labels.
  const setListField = (
    list: "options" | "labels",
    index: number,
    field: keyof KeyValueRow,
    value: string,
  ) =>
    setForm((prev) => ({
      ...prev,
      [list]: prev[list].map((row, i) =>
        i === index ? { ...row, [field]: value } : row,
      ),
    }));

  const insertListItem = (list: "options" | "labels") =>
    setForm((prev) => ({
      ...prev,
      [list]: [...prev[list], { key: "", value: "" }],
    }));

  const removeListItem = (list: "options" | "labels", index: number) =>
    setForm((prev) => ({
      ...prev,
      [list]: prev[list].filter((_, i) => i !== index),
    }));

  const validate = (values: CreateFormValues): boolean => {
    const errors = {
      name: values.name.trim().length === 0 ? "Name is required" : null,
      driver: values.driver.trim().length === 0 ? "Driver is required" : null,
    };
    setFormError(errors);
    return !errors.name && !errors.driver;
  };

  const networksQuery = useHostScopedList<Network>(
    "networks",
    hostId,
    (h) => listNetworks(h),
    { refetchInterval: LIVE_REFETCH_MS },
  );

  // The effective host for a row's actions/links: its own host in the aggregated
  // view, otherwise the single selected host.
  const rowHost = (n: NetworkRow) => n._hostId ?? hostId;
  const detailPath = (n: NetworkRow) =>
    `/networks/${encodeURIComponent(n.id)}?host=${encodeURIComponent(rowHost(n))}`;

  // Host on which the connect modal operates (the selected row's host in the
  // aggregated view).
  const connectHost = connectTarget ? rowHost(connectTarget) : hostId;

  const invalidate = () =>
    queryClient.invalidateQueries({
      predicate: (q) => String(q.queryKey[0]).startsWith("networks"),
    });

  // Running containers, fetched only while the connect modal is open.
  const containersQuery = useQuery({
    queryKey: ["containers", connectHost, false],
    queryFn: () => listContainers(connectHost, false),
    enabled: connectTarget !== null,
  });

  const createMutation = useMutation({
    mutationFn: (body: NetworkCreateOptions) => createNetwork(targetHost, body),
    onSuccess: (network) => {
      notify({
        color: "green",
        title: "Network created",
        message: `Created network ${network.name}`,
      });
      setCreateOpen(false);
      resetForm();
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Create failed",
        message: errorMessage(err, "Could not create network"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (network: NetworkRow) => removeNetwork(rowHost(network), network.id),
    onSuccess: (_data, network) => {
      notify({
        color: "green",
        title: "Network removed",
        message: `Removed network ${network.name}`,
      });
      setToDelete(null);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Remove failed",
        message: errorMessage(err, "Could not remove network"),
      });
    },
  });

  const pruneMutation = useMutation({
    mutationFn: () => prune(targetHost, "networks"),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Networks pruned",
        message: "Removed unused networks",
      });
      setPruneOpen(false);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Prune failed",
        message: errorMessage(err, "Could not prune networks"),
      });
    },
  });

  const connectMutation = useMutation({
    mutationFn: ({
      host,
      networkId,
      containerId,
    }: {
      host: string;
      networkId: string;
      containerId: string;
    }) => connectContainer(host, networkId, containerId),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Container connected",
        message: "The container was attached to the network",
      });
      setConnectTarget(null);
      setConnectContainerId(null);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Connect failed",
        message: errorMessage(err, "Could not connect container"),
      });
    },
  });

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const values = form;
    if (!validate(values)) return;
    const ipam: IPAMConfig[] = [];
    const subnet = values.subnet.trim();
    const gateway = values.gateway.trim();
    const ipRange = values.ipRange.trim();
    if (subnet || gateway || ipRange) {
      ipam.push({
        subnet,
        gateway,
        ...(ipRange ? { ipRange } : {}),
      });
    }
    createMutation.mutate({
      name: values.name.trim(),
      driver: values.driver,
      internal: values.internal,
      attachable: values.attachable,
      options: rowsToRecord(values.options),
      labels: rowsToRecord(values.labels),
      ipam: ipam.length > 0 ? ipam : undefined,
    });
  };

  const openCreate = () => {
    resetForm();
    setTargetHost(defaultTargetHost(hostId, hosts));
    setCreateOpen(true);
  };

  const openPrune = () => {
    setTargetHost(defaultTargetHost(hostId, hosts));
    setPruneOpen(true);
  };

  const openConnect = (network: NetworkRow) => {
    setConnectContainerId(null);
    setConnectTarget(network);
  };

  const networks: NetworkRow[] = networksQuery.data ?? [];

  const containerOptions = (containersQuery.data ?? []).map((c) => ({
    value: c.id,
    label: `${containerName(c.names)} (${shortId(c.id)})`,
  }));

  // --- Table columns + row actions ------------------------------------------
  // Select-filter options are derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted).
  const driverOptions = useMemo(
    () =>
      Array.from(new Set(networks.map((n) => n.driver || "").filter(Boolean)))
        .sort()
        .map((d) => ({ value: d, label: d })),
    [networks],
  );
  const hostOptions = useMemo(
    () =>
      Array.from(new Set(networks.map((n) => n._hostName || "").filter(Boolean)))
        .sort()
        .map((h) => ({ value: h, label: h })),
    [networks],
  );

  const hostColumn: Column<NetworkRow> = {
    key: "host",
    header: "Host",
    sortable: true,
    accessor: (n) => n._hostName ?? "",
    filter: { kind: "select", options: hostOptions },
    filterFn: (n, value) =>
      !Array.isArray(value) ||
      value.length === 0 ||
      value.includes(n._hostName ?? ""),
    cell: (n) => (
      <Badge
        variant="light"
        color="magenta"
        iconLeft="dns-outline"
      >
        {n._hostName ?? "—"}
      </Badge>
    ),
  };

  const columns: Column<NetworkRow>[] = [
    {
      key: "name",
      header: "Name",
      type: "link",
      sortable: true,
      accessor: (n) => n.name,
      grow: true,
      href: (n) => detailPath(n),
      link: {
        color: "info",
        onClick: (n, e) => {
          e.preventDefault();
          navigate(detailPath(n));
        },
      },
    },
    ...(aggregated ? [hostColumn] : []),
    {
      key: "driver",
      header: "Driver",
      sortable: true,
      accessor: (n) => n.driver || "",
      filter: { kind: "select", options: driverOptions },
      filterFn: (n, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(n.driver || ""),
      cell: (n) => <Badge variant="light">{n.driver || "-"}</Badge>,
    },
    {
      key: "scope",
      header: "Scope",
      sortable: true,
      accessor: (n) => n.scope || "",
      cell: (n) => <Text size="sm">{n.scope || "-"}</Text>,
    },
    {
      key: "access",
      header: "Access",
      accessor: (n) => (n.internal ? "internal" : ""),
      cell: (n) =>
        n.internal ? (
          <Badge variant="light" color="warning">
            internal
          </Badge>
        ) : (
          <Text color="muted">-</Text>
        ),
    },
    {
      key: "id",
      header: "ID",
      accessor: (n) => n.id,
      cell: (n) => <Code>{shortId(n.id)}</Code>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (n) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(n)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (network: NetworkRow) => (
    <Box direction="row" gap={1} justify="end" style={{ flexShrink: 0 }}>
      <Tooltip content="Connect container">
        <Button
          variant="clear"
          color="blue"
          iconLeft="power-outline-rounded"
          aria-label={`Connect a container to ${network.name}`}
          onClick={() => openConnect(network)}
        />
      </Tooltip>
      <Tooltip content="Remove network">
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          aria-label={`Remove ${network.name}`}
          onClick={() => setToDelete(network)}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Networks"
        description="Create, inspect and connect Docker networks on this host."
        icon={<Icon name="lan-outline-rounded" size="lg" />}
        actions={
          <>
            <Tooltip content="Refresh">
              <Button
                variant="outline"
                color="neutral"
                size="lg"
                iconLeft="refresh-rounded"
                aria-label="Refresh networks"
                loading={networksQuery.isFetching}
                onClick={() => networksQuery.refetch()}
              />
            </Tooltip>
            {features.networkMap && (
              <Button
                variant="outline"
                color="neutral"
                iconLeft="route-outline"
                onClick={() => navigate("/networks/map")}
              >
                Map
              </Button>
            )}
            <Button
              variant="outline"
              color="neutral"
              iconLeft="delete-outline-rounded"
              onClick={openPrune}
            >
              Prune unused
            </Button>
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={openCreate}
            >
              Create network
            </Button>
          </>
        }
      />

      <SectionCard
        plain
        title="Networks"
        icon={<Icon name="lan-outline-rounded" size="md" />}
        actions={
          <Badge variant="light" color="neutral" size="lg">
            {networks.length}
          </Badge>
        }
        noBodyPadding
      >
        {networksQuery.isError ? (
          <Box padding={4}>
            <Text color="danger">
              {errorMessage(networksQuery.error, "Failed to load networks")}
            </Text>
          </Box>
        ) : (
          <Table<NetworkRow>
            data={networks}
            columns={columns}
            rowKey={(n) => `${n._hostId ?? ""}/${n.id}`}
            onRowClick={(n) => navigate(detailPath(n))}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={networksQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="lan-outline-rounded" size="xl" />}
                title="No networks found"
                description="No networks exist on this host yet. Create one to connect your containers."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                  >
                    Create network
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      <Popup open={createOpen} onOpenChange={setCreateOpen} size="md">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Create network</Popup.Title>
          </Popup.Header>
          <form onSubmit={handleCreate}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <HostSelectField
                  value={targetHost}
                  onChange={setTargetHost}
                  disabled={createMutation.isPending}
                />
                <TextInput
                  label="Name"
                  placeholder="my-network"
                  autoFocus
                  value={form.name}
                  errorText={formError.name ?? undefined}
                  onChange={(e) => setField("name", e.target.value)}
                  fullWidth
                />
                <Select
                  label="Driver"
                  options={DRIVER_OPTIONS}
                  value={form.driver}
                  errorText={formError.driver ?? undefined}
                  onChange={(v) => setField("driver", v as string)}
                  fullWidth
                />

                <Box direction="row" wrap={false} align="start" gap={4}>
                  <TextInput
                    label="Subnet"
                    placeholder="172.20.0.0/16"
                    value={form.subnet}
                    onChange={(e) => setField("subnet", e.target.value)}
                    fullWidth
                  />
                  <TextInput
                    label="Gateway"
                    placeholder="172.20.0.1"
                    value={form.gateway}
                    onChange={(e) => setField("gateway", e.target.value)}
                    fullWidth
                  />
                </Box>
                <TextInput
                  label="IP range"
                  description="Optional sub-range to allocate addresses from."
                  placeholder="172.20.10.0/24"
                  value={form.ipRange}
                  onChange={(e) => setField("ipRange", e.target.value)}
                  fullWidth
                />

                <Box direction="row" wrap gap={4}>
                  <InputToggle
                    label="Internal"
                    description="Restrict external access"
                    checked={form.internal}
                    onChange={(e) => setField("internal", e.target.checked)}
                  />
                  <InputToggle
                    label="Attachable"
                    description="Allow manual container attach"
                    checked={form.attachable}
                    onChange={(e) => setField("attachable", e.target.checked)}
                  />
                </Box>

                {/* Driver options */}
                <KeyValueEditor
                  label="Driver options"
                  emptyText="No driver options."
                  removeAriaLabel="Remove option"
                  rows={form.options}
                  onAdd={() => insertListItem("options")}
                  onUpdate={(i, patch) => {
                    if (patch.key !== undefined)
                      setListField("options", i, "key", patch.key);
                    if (patch.value !== undefined)
                      setListField("options", i, "value", patch.value);
                  }}
                  onRemove={(i) => removeListItem("options", i)}
                />

                {/* Labels */}
                <KeyValueEditor
                  label="Labels"
                  emptyText="No labels."
                  removeAriaLabel="Remove label"
                  rows={form.labels}
                  onAdd={() => insertListItem("labels")}
                  onUpdate={(i, patch) => {
                    if (patch.key !== undefined)
                      setListField("labels", i, "key", patch.key);
                    if (patch.value !== undefined)
                      setListField("labels", i, "value", patch.value);
                  }}
                  onRemove={(i) => removeListItem("labels", i)}
                />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap justify="end" gap={1}>
                <Button
                  variant="outline"
                  color="neutral"
                  onClick={() => setCreateOpen(false)}
                >
                  Cancel
                </Button>
                <Button
                  color="info"
                  type="submit"
                  loading={createMutation.isPending}
                  disabled={targetHost === ""}
                >
                  Create
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <ConfirmModal
        opened={toDelete !== null}
        onClose={() => setToDelete(null)}
        title="Remove network"
        confirmLabel="Remove"
        loading={deleteMutation.isPending}
        onConfirm={() => toDelete && deleteMutation.mutate(toDelete)}
        message={
          <>
            Are you sure you want to remove network{" "}
            <Text as="span" weight="semibold">
              {toDelete?.name}
            </Text>
            ? This action cannot be undone.
          </>
        }
      />

      <ConfirmModal
        opened={pruneOpen}
        onClose={() => setPruneOpen(false)}
        title="Prune unused networks"
        confirmLabel="Prune"
        loading={pruneMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          // Guard: with no host selected there is nothing to target, so the
          // action must not fire.
          if (targetHost !== "") pruneMutation.mutate();
        }}
        message="Remove all custom networks not used by any container? Built-in networks are left untouched."
      >
        <HostSelectField
          value={targetHost}
          onChange={setTargetHost}
          disabled={pruneMutation.isPending}
        />
      </ConfirmModal>

      {/* Connect container modal */}
      <Popup
        open={connectTarget !== null}
        onOpenChange={(o) => {
          if (!o) setConnectTarget(null);
        }}
        size="sm"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {connectTarget
                ? `Connect container to ${connectTarget.name}`
                : "Connect container"}
            </Popup.Title>
          </Popup.Header>
          <Popup.Body>
            <Select
              label="Container"
              placeholder={
                containersQuery.isLoading
                  ? "Loading containers..."
                  : "Select a running container"
              }
              options={containerOptions}
              value={connectContainerId ?? ""}
              onChange={(v) => setConnectContainerId((v as string) || null)}
              searchable
              disabled={containersQuery.isLoading}
              fullWidth
            />
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" wrap justify="end" gap={1}>
              <Button
                variant="outline"
                color="neutral"
                onClick={() => setConnectTarget(null)}
              >
                Cancel
              </Button>
              <Button
                color="info"
                loading={connectMutation.isPending}
                disabled={!connectContainerId}
                onClick={() => {
                  if (connectTarget && connectContainerId) {
                    connectMutation.mutate({
                      host: rowHost(connectTarget),
                      networkId: connectTarget.id,
                      containerId: connectContainerId,
                    });
                  }
                }}
              >
                Connect
              </Button>
            </Box>
          </Popup.Footer>
        </Popup.Content>
      </Popup>
    </Box>
  );
}
