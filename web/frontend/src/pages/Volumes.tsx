import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
  Icon,
  InputToggle,
  Popup,
  Select,
  Skeleton,
  Table,
  Text,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import {
  createVolume,
  installHostPlugin,
  listHosts,
  listVolumePluginTypes,
  listVolumes,
  prune,
  removeVolume,
} from "../api/client";
import { notify } from "../ui/toast";
import ConfirmModal from "../components/ConfirmModal";
import ConnectionField from "../components/ConnectionField";
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
import type {
  VolumeCreateOptions,
  VolumePluginType,
  Volume,
} from "../types";
import { errorMessage, formatBytes, formatDate } from "../utils";

type VolumeRow = Volume & WithHost;

// ---------------------------------------------------------------------------
// Unified "Create volume" popup: a single Provider Select drives which guided
// fields render. "local", "nfs", "cifs", "tmpfs" and "custom" are built in;
// "s3fs"/"sshfs" come from the managed-plugin catalog (listVolumePluginTypes).
// The two plugin-backed providers and cifs resolve their secret server-side via
// a selected connection (connectionRef + provider) — the secret is NEVER
// rendered or sent from the frontend.
// ---------------------------------------------------------------------------

/** Provider ids the popup offers. Plugin providers use their catalog `type`. */
type Provider = "local" | "nfs" | "cifs" | "tmpfs" | "custom" | "s3fs" | "sshfs";

/** Drop empty parts and join the rest with ",". */
function joinComma(parts: string[]): string {
  return parts.filter((p) => p.trim().length > 0).join(",");
}

export default function Volumes() {
  const { hostId } = useHost();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const aggregated = isAllHosts(hostId);

  const [createOpen, setCreateOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<VolumeRow | null>(null);
  const [pruneOpen, setPruneOpen] = useState(false);

  // Explicit target host for create/prune actions. In the aggregated "All
  // hosts" view there is no single global host to target, so the user picks one
  // inside the modal (defaulted from the global selection).
  const [targetHost, setTargetHost] = useState("");
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hosts = hostsQuery.data ?? [];

  const volumesQuery = useHostScopedList<Volume>(
    "volumes",
    hostId,
    (h) => listVolumes(h),
    { refetchInterval: LIVE_REFETCH_MS },
  );

  // The effective host for a row's actions/links: its own host in the aggregated
  // view, otherwise the single selected host.
  const rowHost = (v: VolumeRow) => v._hostId ?? hostId;
  const detailPath = (v: VolumeRow) =>
    `/volumes/${encodeURIComponent(v.name)}?host=${encodeURIComponent(rowHost(v))}`;

  const invalidate = () =>
    queryClient.invalidateQueries({
      predicate: (q) => String(q.queryKey[0]).startsWith("volumes"),
    });

  // --- Unified create form state -------------------------------------------
  // The Provider Select drives which of the guided fields below apply. Fields
  // shared by all providers (name, labels) live at the top; provider-specific
  // fields are namespaced and reset when the provider changes.
  const [provider, setProvider] = useState<Provider>("local");
  const [name, setName] = useState("");
  const [labels, setLabels] = useState<KeyValueRow[]>([]);

  // nfs
  const [nfsServer, setNfsServer] = useState("");
  const [nfsExport, setNfsExport] = useState("");
  const [nfsReadOnly, setNfsReadOnly] = useState(false);
  const [nfsExtra, setNfsExtra] = useState("");

  // cifs / smb
  const [cifsServer, setCifsServer] = useState("");
  const [cifsShare, setCifsShare] = useState("");
  const [cifsExtra, setCifsExtra] = useState("");

  // tmpfs
  const [tmpfsSize, setTmpfsSize] = useState("");

  // custom
  const [customDriver, setCustomDriver] = useState("");
  const [customOptions, setCustomOptions] = useState<KeyValueRow[]>([]);

  // plugin providers (s3fs/sshfs): collected non-sensitive field values
  const [pluginValues, setPluginValues] = useState<Record<string, string>>({});

  // Connection ref for the providers whose secret is injected server-side
  // (cifs/s3fs/sshfs). Preselected by ConnectionField when eligible ones exist.
  const [connectionRef, setConnectionRef] = useState("");

  // The managed-plugin catalog powers the s3fs/sshfs providers. Fetched once the
  // modal is open (same key the old "from driver" flow used, so the cache is
  // shared).
  const pluginTypesQuery = useQuery({
    queryKey: ["volume-plugin-types"],
    queryFn: listVolumePluginTypes,
    enabled: createOpen,
  });
  const pluginTypes: VolumePluginType[] = pluginTypesQuery.data ?? [];
  const currentPlugin =
    provider === "s3fs" || provider === "sshfs"
      ? pluginTypes.find((p) => p.type === provider) ?? null
      : null;
  // Only the plugin providers actually present in the catalog are offered.
  const s3fsAvailable = pluginTypes.some((p) => p.type === "s3fs");
  const sshfsAvailable = pluginTypes.some((p) => p.type === "sshfs");

  // Reset every provider-specific field (and the connection) to defaults.
  const resetProviderFields = () => {
    setNfsServer("");
    setNfsExport("");
    setNfsReadOnly(false);
    setNfsExtra("");
    setCifsServer("");
    setCifsShare("");
    setCifsExtra("");
    setTmpfsSize("");
    setCustomDriver("");
    setCustomOptions([]);
    setPluginValues({});
    setConnectionRef("");
  };

  const resetForm = () => {
    setProvider("local");
    setName("");
    setLabels([]);
    resetProviderFields();
  };

  // Switching provider clears the fields of the previous one so stale values
  // never leak into the next body.
  const changeProvider = (next: Provider) => {
    setProvider(next);
    resetProviderFields();
  };

  const providerOptions = [
    { value: "local", label: "Local (default)" },
    { value: "nfs", label: "NFS" },
    { value: "cifs", label: "CIFS / SMB" },
    { value: "tmpfs", label: "tmpfs (RAM)" },
    ...(s3fsAvailable ? [{ value: "s3fs", label: "S3 (s3fs)" }] : []),
    ...(sshfsAvailable ? [{ value: "sshfs", label: "SSH (sshfs)" }] : []),
    { value: "custom", label: "Custom driver" },
  ];

  // Build the createVolume body for the current provider, or a validation error.
  // The secret is NEVER placed in the body — only connectionRef + provider +
  // the non-secret options; the control plane injects the credential.
  const buildBody = ():
    | { body: VolumeCreateOptions; pluginRef?: string }
    | { error: string } => {
    const trimmedName = name.trim();
    if (trimmedName.length === 0) return { error: "Name is required" };
    const labelsRecord = rowsToRecord(labels);

    switch (provider) {
      case "local":
        return {
          body: {
            name: trimmedName,
            driver: "local",
            options: {},
            labels: labelsRecord,
          },
        };
      case "nfs": {
        const server = nfsServer.trim();
        const exportPath = nfsExport.trim();
        if (server.length === 0) return { error: "Server is required" };
        if (exportPath.length === 0) return { error: "Export path is required" };
        return {
          body: {
            name: trimmedName,
            driver: "local",
            options: {
              type: "nfs",
              device: ":" + exportPath,
              o: joinComma([
                "addr=" + server,
                nfsReadOnly ? "ro" : "rw",
                nfsExtra.trim(),
              ]),
            },
            labels: labelsRecord,
          },
        };
      }
      case "cifs": {
        const server = cifsServer.trim();
        const share = cifsShare.trim();
        if (server.length === 0) return { error: "Server is required" };
        if (share.length === 0) return { error: "Share is required" };
        if (connectionRef.trim().length === 0)
          return { error: "An SMB credential is required" };
        return {
          body: {
            name: trimmedName,
            driver: "local",
            options: {
              type: "cifs",
              device: "//" + server + "/" + share,
              o: cifsExtra.trim(),
            },
            labels: labelsRecord,
            connectionRef,
            provider: "cifs",
          },
        };
      }
      case "tmpfs": {
        const size = tmpfsSize.trim();
        if (size.length === 0) return { error: "Size is required" };
        return {
          body: {
            name: trimmedName,
            driver: "local",
            options: { type: "tmpfs", o: "size=" + size },
            labels: labelsRecord,
          },
        };
      }
      case "s3fs":
      case "sshfs": {
        const plugin = currentPlugin;
        if (!plugin) return { error: "Plugin driver is not available" };
        // Only non-sensitive fields are collected/rendered; the secret keys are
        // injected server-side from the connection.
        const options: Record<string, string> = {};
        for (const f of plugin.options ?? []) {
          if (f.sensitive || f.fromConnection) continue;
          const v = pluginValues[f.key]?.trim();
          if (v) options[f.key] = v;
        }
        if (provider === "s3fs" && !options["bucket"])
          return { error: "Bucket is required" };
        if (provider === "sshfs" && !options["host"])
          return { error: "Host is required" };
        if (connectionRef.trim().length === 0)
          return {
            error:
              provider === "s3fs"
                ? "An S3 / AWS credential is required"
                : "An SSH credential is required",
          };
        return {
          body: {
            name: trimmedName,
            driver: plugin.driver || plugin.pluginRef,
            options,
            labels: labelsRecord,
            connectionRef,
            provider: plugin.type,
          },
          pluginRef: plugin.pluginRef,
        };
      }
      case "custom": {
        const driver = customDriver.trim();
        if (driver.length === 0) return { error: "Driver is required" };
        return {
          body: {
            name: trimmedName,
            driver,
            options: rowsToRecord(customOptions),
            labels: labelsRecord,
          },
        };
      }
    }
  };

  const createMutation = useMutation({
    mutationFn: async (input: {
      body: VolumeCreateOptions;
      pluginRef?: string;
    }) => {
      // Plugin providers first ensure the managed plugin exists on the host
      // (idempotent), then create the volume with that driver.
      if (input.pluginRef) {
        await installHostPlugin(targetHost, { remote: input.pluginRef });
      }
      return createVolume(targetHost, input.body);
    },
    onSuccess: (volume) => {
      notify({
        color: "green",
        title: "Volume created",
        message: `Volume "${volume.name}" was created.`,
      });
      invalidate();
      setCreateOpen(false);
      resetForm();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to create volume",
        message: errorMessage(err, "Could not create the volume."),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: ({ host, name }: { host: string; name: string }) =>
      removeVolume(host, name),
    onSuccess: (_data, vars) => {
      notify({
        color: "green",
        title: "Volume removed",
        message: `Volume "${vars.name}" was removed.`,
      });
      invalidate();
      setDeleteTarget(null);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to remove volume",
        message: errorMessage(err, "Could not remove the volume."),
      });
    },
  });

  const pruneMutation = useMutation({
    mutationFn: () => prune(targetHost, "volumes"),
    onSuccess: (res) => {
      notify({
        color: "green",
        title: "Volumes pruned",
        message: `Reclaimed ${formatBytes(res.spaceReclaimed)}`,
      });
      setPruneOpen(false);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Prune failed",
        message: errorMessage(err, "Could not prune volumes."),
      });
    },
  });

  const openCreate = () => {
    resetForm();
    setTargetHost(defaultTargetHost(hostId, hosts));
    setCreateOpen(true);
  };

  // Seed the target host when the prune modal opens, so the HostSelectField
  // inside it starts on a sensible default.
  useEffect(() => {
    if (pruneOpen) {
      setTargetHost(defaultTargetHost(hostId, hosts));
    }
    // Only re-seed on the transition to open; hosts/hostId are read fresh here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pruneOpen]);

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (targetHost === "") {
      notify({
        color: "red",
        title: "Host required",
        message: "Pick a target host for the volume.",
      });
      return;
    }
    const built = buildBody();
    if ("error" in built) {
      notify({
        color: "red",
        title: "Cannot create volume",
        message: built.error,
      });
      return;
    }
    createMutation.mutate(built);
  };

  const volumes = volumesQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  // Select-filter options derived from the data (data-mode needs explicit options).
  const driverOptions = useMemo(
    () =>
      Array.from(new Set(volumes.map((v) => v.driver || "local")))
        .sort()
        .map((d) => ({ value: d, label: d })),
    [volumes],
  );
  const hostOptions = useMemo(
    () =>
      Array.from(new Set(volumes.map((v) => v._hostName || "").filter(Boolean)))
        .sort()
        .map((h) => ({ value: h, label: h })),
    [volumes],
  );

  const hostColumn: Column<VolumeRow> = {
    key: "host",
    header: "Host",
    sortable: true,
    accessor: (v) => v._hostName ?? "",
    filter: { kind: "select", options: hostOptions },
    filterFn: (v, value) =>
      !Array.isArray(value) ||
      value.length === 0 ||
      value.includes(v._hostName ?? ""),
    cell: (v) => (
      <Badge
        variant="light"
        color="magenta"
        iconLeft="dns-outline"
      >
        {v._hostName ?? "—"}
      </Badge>
    ),
  };

  const columns: Column<VolumeRow>[] = [
    {
      key: "name",
      header: "Name",
      type: "link",
      sortable: true,
      accessor: (v) => v.name,
      href: (v) => detailPath(v),
      link: {
        color: "info",
        onClick: (v, e) => {
          e.preventDefault();
          navigate(detailPath(v));
        },
      },
    },
    ...(aggregated ? [hostColumn] : []),
    {
      key: "driver",
      header: "Driver",
      sortable: true,
      accessor: (v) => v.driver || "local",
      filter: { kind: "select", options: driverOptions },
      filterFn: (v, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(v.driver || "local"),
      cell: (v) => (
        <Badge variant="light" color="blue">
          {v.driver || "local"}
        </Badge>
      ),
    },
    {
      key: "mountpoint",
      header: "Mountpoint",
      grow: true,
      accessor: (v) => v.mountpoint || "",
      // `data-wrap-cell` opts this cell out of the global single-line ellipsis
      // clamp (see scale.css) so the full path wraps instead of truncating.
      cell: (v) =>
        v.mountpoint ? (
          <span data-wrap-cell>
            <Code>{v.mountpoint}</Code>
          </span>
        ) : (
          <Text color="muted">-</Text>
        ),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (v) => v.createdAt,
      cell: (v) => <Text size="sm">{formatDate(v.createdAt)}</Text>,
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (v) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(v)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (v: VolumeRow) => (
    <Box direction="row" wrap={false} justify="end" gap={1}>
      <Tooltip content="Explore files">
        <Button
          variant="clear"
          color="neutral"
          iconLeft="folder-open-outline-rounded"
          aria-label={`Explore volume ${v.name}`}
          onClick={() => {
            const params = new URLSearchParams({
              host: rowHost(v),
              name: v.name,
            });
            navigate(`/volume-explorer?${params.toString()}`);
          }}
        />
      </Tooltip>
      <Tooltip content="Remove volume">
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          aria-label={`Remove volume ${v.name}`}
          onClick={() => setDeleteTarget(v)}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Volumes"
        description="Manage Docker volumes, attach external storage via plugins, and reclaim unused space."
        icon={<Icon name="database-outline" size="lg" />}
        actions={
          <>
            <Tooltip content="Refresh">
              <Button
                variant="outline"
                color="neutral"
                size="lg"
                aria-label="Refresh volumes"
                iconLeft="refresh-rounded"
                loading={volumesQuery.isFetching}
                onClick={() => void volumesQuery.refetch()}
              />
            </Tooltip>
            <Button
              variant="outline"
              color="neutral"
              iconLeft="delete-outline-rounded"
              onClick={() => setPruneOpen(true)}
            >
              Prune unused
            </Button>
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={openCreate}
            >
              Create volume
            </Button>
          </>
        }
      />

      <SectionCard
        plain
        title="Volumes"
        icon={<Icon name="database-outline" size="md" />}
        actions={
          <Badge variant="light" color="gray" size="lg">
            {volumes.length}
          </Badge>
        }
        noBodyPadding
      >
        {volumesQuery.isError ? (
          <Box padding={4}>
            <Text color="danger">
              {errorMessage(volumesQuery.error, "Failed to load volumes.")}
            </Text>
          </Box>
        ) : (
          <Table<VolumeRow>
            data={volumes}
            columns={columns}
            rowKey={(v) => `${v._hostId ?? ""}/${v.name}`}
            onRowClick={(v) => navigate(detailPath(v))}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            defaultSort={[{ key: "name", dir: "asc" }]}
            loading={volumesQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="database-outline" size="xl" />}
                title="No volumes on this host"
                description="Create a volume to persist data outside of container filesystems."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                  >
                    Create volume
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      <Popup
        open={createOpen}
        onOpenChange={(open) => {
          if (!open && !createMutation.isPending) setCreateOpen(false);
        }}
        size="md"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Create volume</Popup.Title>
          </Popup.Header>
          <form onSubmit={handleCreate}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <HostSelectField value={targetHost} onChange={setTargetHost} />

                <Select
                  label="Provider"
                  description="How the volume's storage is backed."
                  options={providerOptions}
                  value={provider}
                  onChange={(v) => changeProvider((v as Provider) ?? "local")}
                />

                <TextInput
                  label="Name"
                  placeholder="my-volume"
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />

                {/* --- Provider-specific fields ------------------------- */}

                {provider === "local" && (
                  <Text size="sm" color="muted">
                    A standard local volume managed by Docker on the host.
                  </Text>
                )}

                {provider === "nfs" && (
                  <>
                    <TextInput
                      label="Server"
                      placeholder="nfs.example.com"
                      value={nfsServer}
                      onChange={(e) => setNfsServer(e.target.value)}
                    />
                    <TextInput
                      label="Export path"
                      placeholder="/exports/data"
                      value={nfsExport}
                      onChange={(e) => setNfsExport(e.target.value)}
                    />
                    <InputToggle
                      label="Read-only"
                      checked={nfsReadOnly}
                      onChange={(e) => setNfsReadOnly(e.target.checked)}
                    />
                    <TextInput
                      label="Extra options"
                      description="Comma-separated mount options (optional)."
                      placeholder="nfsvers=4,soft"
                      value={nfsExtra}
                      onChange={(e) => setNfsExtra(e.target.value)}
                    />
                  </>
                )}

                {provider === "cifs" && (
                  <>
                    <TextInput
                      label="Server"
                      placeholder="fileserver.example.com"
                      value={cifsServer}
                      onChange={(e) => setCifsServer(e.target.value)}
                    />
                    <TextInput
                      label="Share"
                      placeholder="share-name"
                      value={cifsShare}
                      onChange={(e) => setCifsShare(e.target.value)}
                    />
                    <TextInput
                      label="Extra options"
                      description="Comma-separated mount options (optional)."
                      placeholder="vers=3.0"
                      value={cifsExtra}
                      onChange={(e) => setCifsExtra(e.target.value)}
                    />
                    <ConnectionField
                      capability="httpBearer"
                      types={["basic-auth"]}
                      emptyLabel="SMB"
                      value={connectionRef}
                      onChange={setConnectionRef}
                      isEdit={false}
                    />
                  </>
                )}

                {provider === "tmpfs" && (
                  <TextInput
                    label="Size"
                    description="Max size held in RAM."
                    placeholder="256m"
                    value={tmpfsSize}
                    onChange={(e) => setTmpfsSize(e.target.value)}
                  />
                )}

                {(provider === "s3fs" || provider === "sshfs") &&
                  (pluginTypesQuery.isLoading ? (
                    <Skeleton height={120} />
                  ) : currentPlugin ? (
                    <>
                      {currentPlugin.description && (
                        <Text size="xs" color="muted">
                          {currentPlugin.description}
                        </Text>
                      )}
                      {currentPlugin.pluginRef && (
                        <Text size="xs" color="muted">
                          Installs managed plugin{" "}
                          <Code>{currentPlugin.pluginRef}</Code> on the host if
                          not present.
                        </Text>
                      )}
                      {(currentPlugin.options ?? [])
                        .filter((f) => !f.sensitive && !f.fromConnection)
                        .map((f) => (
                          <TextInput
                            key={f.key}
                            label={f.label}
                            placeholder={f.placeholder}
                            value={pluginValues[f.key] ?? ""}
                            onChange={(e) =>
                              setPluginValues((s) => ({
                                ...s,
                                [f.key]: e.target.value,
                              }))
                            }
                          />
                        ))}
                      {provider === "s3fs" ? (
                        <ConnectionField
                          capability="dockerRegistryAuth"
                          types={["aws"]}
                          emptyLabel="S3 / AWS"
                          value={connectionRef}
                          onChange={setConnectionRef}
                          isEdit={false}
                        />
                      ) : (
                        <ConnectionField
                          capability="httpBearer"
                          types={["basic-auth"]}
                          emptyLabel="SSH"
                          value={connectionRef}
                          onChange={setConnectionRef}
                          isEdit={false}
                        />
                      )}
                    </>
                  ) : (
                    <Callout color="danger">
                      This driver type is not available.
                    </Callout>
                  ))}

                {provider === "custom" && (
                  <>
                    <TextInput
                      label="Driver"
                      placeholder="local"
                      value={customDriver}
                      onChange={(e) => setCustomDriver(e.target.value)}
                    />
                    <KeyValueEditor
                      label="Driver options"
                      emptyText="No driver options."
                      removeAriaLabel="Remove option"
                      rows={customOptions}
                      onAdd={() =>
                        setCustomOptions((rows) => [
                          ...rows,
                          { key: "", value: "" },
                        ])
                      }
                      onUpdate={(i, patch) =>
                        setCustomOptions((rows) =>
                          rows.map((row, idx) =>
                            idx === i ? { ...row, ...patch } : row,
                          ),
                        )
                      }
                      onRemove={(i) =>
                        setCustomOptions((rows) =>
                          rows.filter((_, idx) => idx !== i),
                        )
                      }
                    />
                  </>
                )}

                {/* Labels (available to all providers) */}
                <KeyValueEditor
                  label="Labels"
                  emptyText="No labels."
                  removeAriaLabel="Remove label"
                  rows={labels}
                  onAdd={() =>
                    setLabels((rows) => [...rows, { key: "", value: "" }])
                  }
                  onUpdate={(i, patch) =>
                    setLabels((rows) =>
                      rows.map((row, idx) =>
                        idx === i ? { ...row, ...patch } : row,
                      ),
                    )
                  }
                  onRemove={(i) =>
                    setLabels((rows) => rows.filter((_, idx) => idx !== i))
                  }
                />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" justify="end" gap={2}>
                <Button
                  variant="light"
                  color="neutral"
                  onClick={() => setCreateOpen(false)}
                  disabled={createMutation.isPending}
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
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Remove volume"
        confirmLabel="Remove"
        loading={deleteMutation.isPending}
        onConfirm={() => {
          if (deleteTarget)
            deleteMutation.mutate({
              host: rowHost(deleteTarget),
              name: deleteTarget.name,
            });
        }}
        message={
          <>
            Are you sure you want to remove the volume{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            {deleteTarget?._hostName ? ` on ${deleteTarget._hostName}` : ""}? This
            will force-remove it and cannot be undone.
          </>
        }
      />

      <ConfirmModal
        opened={pruneOpen}
        onClose={() => setPruneOpen(false)}
        title="Prune unused volumes"
        confirmLabel="Prune"
        loading={pruneMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          // Guard: never fire without a target host (no host to prune on).
          if (targetHost === "") return;
          pruneMutation.mutate();
        }}
        message="Remove all volumes not referenced by any container? Data in those volumes will be permanently deleted."
      >
        <HostSelectField value={targetHost} onChange={setTargetHost} />
      </ConfirmModal>
    </Box>
  );
}
