import { useEffect, useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
  Icon,
  Popup,
  Skeleton,
  Table,
  Tabs,
  Text,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { useNavigate, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getAgentUpdateStatus,
  createHost,
  deleteHost,
  getEdgeCommand,
  listHosts,
  startAgentUpdate,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useHost } from "../HostContext";
import { notify } from "../ui/toast";
import ConfirmModal from "../components/ConfirmModal";
import RenameHostDialog from "../components/hosts/RenameHostDialog";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import RollingUpdateDialog from "../components/updates/RollingUpdateDialog";
import StatusBadge from "../components/StatusBadge";
import { LIVE_REFETCH_MS } from "../lib/live";
import type { EnrollResponse, Host, HostMode } from "../types";
import { errorMessage, formatDate, timeAgo } from "../utils";

const HOSTS_QUERY_KEY = ["hosts"] as const;

/** Extract a human-readable error message from an axios error. */

interface AddFormValues {
  name: string;
  mode: HostMode;
  address: string;
}

interface AddFormErrors {
  name?: string;
  address?: string;
}

export default function Hosts() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isAdmin = user?.role === "admin";
  const { hostId: selectedHost, setHostId } = useHost();

  const [addOpen, setAddOpen] = useState(false);
  const [rollingOpen, setRollingOpen] = useState(false);
  // Deep link from the host switcher and dashboard: /hosts?add=1 opens the dialog.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    if (params.get("add") === "1") {
      // Only admins can add hosts; others just land on the list.
      if (isAdmin) setAddOpen(true);
      params.delete("add");
      setParams(params, { replace: true });
    }
  }, [params, setParams, isAdmin]);
  const [deleteTarget, setDeleteTarget] = useState<Host | null>(null);
  const [renameTarget, setRenameTarget] = useState<Host | null>(null);
  const [updateTarget, setUpdateTarget] = useState<Host | null>(null);
  const [enrollment, setEnrollment] = useState<EnrollResponse | null>(null);

  // Plain controlled form state.
  const [addValues, setAddValues] = useState<AddFormValues>({
    name: "",
    mode: "agent",
    address: "",
  });
  const [addErrors, setAddErrors] = useState<AddFormErrors>({});

  const resetAddForm = () => {
    setAddValues({ name: "", mode: "agent", address: "" });
    setAddErrors({});
  };

  /** Validate the add-host form; returns errors keyed by field (empty = valid). */
  const validateAdd = (values: AddFormValues): AddFormErrors => {
    const errors: AddFormErrors = {};
    if (values.name.trim().length === 0) {
      errors.name = "Name is required";
    }
    // Only agent (server->agent) mode needs an address: the server dials it.
    // The token is always generated server-side.
    if (values.mode === "agent" && values.address.trim().length === 0) {
      errors.address = "Address is required";
    }
    return errors;
  };

  const hostsQuery = useQuery({
    queryKey: HOSTS_QUERY_KEY,
    queryFn: listHosts,
    refetchInterval: LIVE_REFETCH_MS,
  });

  const createMutation = useMutation({
    mutationFn: (values: AddFormValues) =>
      createHost(
        values.mode === "edge"
          ? { name: values.name, mode: "edge" }
          : { name: values.name, mode: "agent", address: values.address },
      ),
    onSuccess: (result) => {
      notify({
        color: "green",
        title: "Host added",
        message: `${result.host.name} was added successfully`,
      });
      resetAddForm();
      setAddOpen(false);
      // Both modes get a docker command to run on the target host, so always
      // show it after creation.
      setEnrollment(result);
      void queryClient.invalidateQueries({ queryKey: HOSTS_QUERY_KEY });
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not add host",
        message: errorMessage(err, "Failed to add host"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteHost(id),
    onSuccess: (_data, id) => {
      notify({
        color: "green",
        title: "Host removed",
        message: `Host ${id} was removed`,
      });
      setDeleteTarget(null);
      // Don't leave the global scope (persisted in localStorage) pointing at a
      // host that no longer exists: every host-scoped page would 404.
      if (selectedHost === id) setHostId("local");
      void queryClient.invalidateQueries({ queryKey: HOSTS_QUERY_KEY });
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not remove host",
        message: errorMessage(err, "Failed to remove host"),
      });
    },
  });

  const updateAgentMutation = useMutation({
    mutationFn: (id: string) => startAgentUpdate(id),
    onSuccess: (_res, id) => {
      setUpdateTarget(null);
      void queryClient.invalidateQueries({ queryKey: HOSTS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: ["agent-update", id] });
      // The host page shows the live phases, the result and the history.
      navigate(`/hosts/${id}`);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Update failed",
        message: errorMessage(err, "Could not start the agent update."),
      });
    },
  });

  const edgeCommandMutation = useMutation({
    mutationFn: (id: string) => getEdgeCommand(id),
    onSuccess: (result) => {
      setEnrollment(result);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not load join command",
        message: errorMessage(err, "Failed to load the join command"),
      });
    },
  });

  const handleAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const errors = validateAdd(addValues);
    setAddErrors(errors);
    if (errors.name || errors.address) return;
    createMutation.mutate({
      name: addValues.name.trim(),
      mode: addValues.mode,
      address: addValues.address.trim(),
    });
  };

  const closeAdd = () => {
    if (createMutation.isPending) return;
    resetAddForm();
    setAddOpen(false);
  };

  const hosts = hostsQuery.data ?? [];
  const isEdge = addValues.mode === "edge";

  // --- Table columns + row actions ------------------------------------------
  // Select-filter options are derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted).
  const statusOptions = useMemo(
    () =>
      Array.from(new Set(hosts.map((h) => (h.online ? "online" : "offline"))))
        .sort()
        .map((v) => ({ value: v, label: v.charAt(0).toUpperCase() + v.slice(1) })),
    [hosts],
  );

  const columns: Column<Host>[] = [
    {
      key: "name",
      header: "Name",
      type: "link",
      sortable: true,
      accessor: (h) => h.name,
      href: (h) => `/hosts/${h.id}`,
      link: {
        color: "info",
        // Per-row leading icon (local host vs. agent host), preserved via the
        // link label so the native <a href> still renders (open-in-new-tab, etc.).
        label: (h) => (
          <Box direction="row" gap={2} align="center" wrap={false}>
            <Icon
              name={
                h.id === "local"
                  ? "dns-outline"
                  : "storage-rounded"
              }
            />
            {h.name}
          </Box>
        ),
        onClick: (h, e) => {
          e.preventDefault();
          navigate(`/hosts/${h.id}`);
        },
      },
    },
    {
      key: "type",
      header: "Type",
      sortable: true,
      accessor: (h) => h.type || (h.id === "local" ? "local" : "agent"),
      cell: (h) => (
        <Badge variant="light" color="gray">
          {h.type || (h.id === "local" ? "local" : "agent")}
        </Badge>
      ),
    },
    {
      key: "mode",
      header: "Mode",
      sortable: true,
      accessor: (h) => h.mode || "agent",
      cell: (h) => (
        <Badge variant="light" color={h.mode === "edge" ? "purple" : "blue"}>
          {h.mode || "agent"}
        </Badge>
      ),
    },
    {
      key: "address",
      header: "Address",
      accessor: (h) => h.address || "",
      cell: (h) => (
        <Text size="sm" color="muted">
          {h.address || "-"}
        </Text>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      accessor: (h) => (h.online ? "online" : "offline"),
      filter: { kind: "select", options: statusOptions },
      filterFn: (h, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(h.online ? "online" : "offline"),
      cell: (h) => <StatusBadge status={h.online ? "online" : "offline"} />,
    },
    {
      key: "lastSeen",
      header: "Last seen",
      sortable: true,
      accessor: (h) => (h.lastSeen ? Date.parse(h.lastSeen) || 0 : 0),
      cell: (h) => (
        <Tooltip content={formatDate(h.lastSeen)}>
          <Text size="sm">{timeAgo(h.lastSeen)}</Text>
        </Tooltip>
      ),
    },
    {
      key: "agent",
      header: "Agent",
      cell: (h) =>
        h.id === "local" ? (
          <Text size="sm" color="muted">
            -
          </Text>
        ) : (
          <AgentUpdateCell
            host={h}
            isAdmin={isAdmin}
            pending={
              updateAgentMutation.isPending &&
              updateAgentMutation.variables === h.id
            }
            onUpdate={setUpdateTarget}
          />
        ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (h) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(h)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (host: Host) => {
    const isLocal = host.id === "local";
    if (isLocal) return null;
    return (
      <Box direction="row" gap={2} justify="end" wrap={false}>
        <Tooltip content="Join command">
          <Button
            variant="clear"
            color="purple"
            iconLeft="cable-rounded"
            aria-label={`Join command for ${host.name}`}
            loading={
              edgeCommandMutation.isPending &&
              edgeCommandMutation.variables === host.id
            }
            onClick={() => edgeCommandMutation.mutate(host.id)}
          />
        </Tooltip>
        {isAdmin && (
          <Tooltip content="Rename host">
            <Button
              variant="clear"
              color="neutral"
              iconLeft="edit-outline-rounded"
              aria-label={`Rename ${host.name}`}
              onClick={() => setRenameTarget(host)}
            />
          </Tooltip>
        )}
        {isAdmin && (
          <Tooltip content="Remove host">
            <Button
              variant="clear"
              color="danger"
              iconLeft="delete-outline-rounded"
              aria-label={`Remove ${host.name}`}
              onClick={() => setDeleteTarget(host)}
            />
          </Tooltip>
        )}
      </Box>
    );
  };

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Hosts"
        description="Docker hosts connected to this console."
        icon={<Icon name="dns-outline" size="lg" />}
        actions={
          isAdmin && (
            <>
            <Button variant="outline" iconLeft="upgrade-rounded" onClick={() => setRollingOpen(true)}>
              Update agents
            </Button>
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={() => setAddOpen(true)}
            >
              Add host
            </Button>
            </>
          )
        }
      />

      <SectionCard
        plain
        title="Hosts"
        icon={<Icon name="storage-rounded" />}
        actions={
          !hostsQuery.isLoading &&
          !hostsQuery.isError && (
            <Badge variant="light" color="gray">
              {hosts.length}
            </Badge>
          )
        }
        noBodyPadding
      >
        {hostsQuery.isError ? (
          <Callout
            color="danger"
            variant="light"
            iconLeft={
              <Icon name="warning-outline-rounded" />
            }
            style={{ margin: 16 }}
          >
            {errorMessage(hostsQuery.error, "Failed to load hosts")}
          </Callout>
        ) : (
          <Table<Host>
            data={hosts}
            columns={columns}
            rowKey={(h) => h.id}
            onRowClick={(h) => navigate(`/hosts/${h.id}`)}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={hostsQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="dns-outline" size="xl" />}
                title="No hosts found"
                description="Connect a Docker host to start managing containers, images and more."
                action={
                  isAdmin && (
                    <Button
                      color="info"
                      iconLeft="add-rounded"
                      onClick={() => setAddOpen(true)}
                    >
                      Add host
                    </Button>
                  )
                }
              />
            }
          />
        )}
      </SectionCard>

      <RollingUpdateDialog open={rollingOpen} onOpenChange={setRollingOpen} hosts={hosts} />

      {/* Add host modal */}
      <Popup
        open={addOpen}
        onOpenChange={(open) => {
          if (!open) closeAdd();
        }}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Add host</Popup.Title>
          </Popup.Header>
          <form onSubmit={handleAddSubmit}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                <Tabs
                  variant="segmented"
                  fullWidth
                  value={addValues.mode}
                  onValueChange={(value) =>
                    setAddValues((v) => ({ ...v, mode: value as HostMode }))
                  }
                >
                  <Tabs.List>
                    <Tabs.Trigger value="agent">Agent</Tabs.Trigger>
                    <Tabs.Trigger value="edge">Edge</Tabs.Trigger>
                  </Tabs.List>
                </Tabs>
                <Text size="xs" color="muted">
                  {isEdge
                    ? "Edge (agent → server): the agent dials out to this server and keeps a persistent connection. No inbound address needed. A token and a docker command are generated for you."
                    : "Agent (server → agent): this server dials the agent at the address you provide. A token is generated for you and baked into the docker command."}
                </Text>
                <TextInput
                  label="Name"
                  placeholder="prod-node-1"
                  autoFocus
                  value={addValues.name}
                  onChange={(e) =>
                    setAddValues((v) => ({ ...v, name: e.target.value }))
                  }
                  errorText={addErrors.name}
                />
                {!isEdge && (
                  <TextInput
                    label="Address"
                    description="Reachable IP/host and port the server will dial"
                    placeholder="192.168.1.10:9001"
                    value={addValues.address}
                    onChange={(e) =>
                      setAddValues((v) => ({ ...v, address: e.target.value }))
                    }
                    errorText={addErrors.address}
                  />
                )}
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" justify="end" gap={2}>
                <Button
                  variant="outline"
                  color="neutral"
                  onClick={closeAdd}
                  disabled={createMutation.isPending}
                >
                  Cancel
                </Button>
                <Button type="submit" color="info" loading={createMutation.isPending}>
                  Add host
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      {/* Edge enrollment / join command modal */}
      <Popup
        open={enrollment !== null}
        onOpenChange={(open) => {
          if (!open) setEnrollment(null);
        }}
        size="lg"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {enrollment
                ? `Join command — ${enrollment.host.name}`
                : "Join command"}
            </Popup.Title>
          </Popup.Header>
          {enrollment && (
            <>
              <Popup.Body>
                <Box direction="column" gap={4}>
                  <Text size="sm" color="muted">
                    {enrollment.host.mode === "edge"
                      ? "Run this on the target machine. The agent dials out to this server and keeps a persistent connection — no inbound ports required."
                      : "Run this on the target machine. The agent listens on port 9001 and this server dials the address you configured to reach it."}
                  </Text>

                  <CommandBlock label="Docker" command={enrollment.dockerRun} />
                </Box>
              </Popup.Body>
              <Popup.Footer>
                <Box direction="row" justify="end">
                  <Button
                    variant="outline"
                    color="neutral"
                    onClick={() => setEnrollment(null)}
                  >
                    Close
                  </Button>
                </Box>
              </Popup.Footer>
            </>
          )}
        </Popup.Content>
      </Popup>

      <RenameHostDialog host={renameTarget} onClose={() => setRenameTarget(null)} />

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Remove host"
        confirmLabel="Remove"
        loading={deleteMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          if (deleteTarget) deleteMutation.mutate(deleteTarget.id);
        }}
        message={
          <>
            Are you sure you want to remove{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? This only removes it from kowan and does not affect the host itself.
          </>
        }
      />

      <ConfirmModal
        opened={updateTarget !== null}
        onClose={() => setUpdateTarget(null)}
        title="Update agent"
        confirmLabel="Update agent"
        loading={updateAgentMutation.isPending}
        lockWhileLoading
        onConfirm={() => {
          if (updateTarget) updateAgentMutation.mutate(updateTarget.id);
        }}
        message={
          <>
            This pulls and smoke-tests the new image on{" "}
            <Text as="span" weight="semibold">
              {updateTarget?.name}
            </Text>
            , then swaps the agent container, keeping its configuration. If the new
            agent does not pass its health check, the previous one is restored
            automatically.
          </>
        }
      />
    </Box>
  );
}

// Per-row agent update status for the Hosts list. Queries the update check for
// this host (cache shared with the Host detail page via the same query key) and
// shows an "Update available" / "Up to date" badge, plus an admin-only inline
// button that opens the shared confirm modal. Fetch-once (no polling) so the list
// doesn't hammer registries/agents — the detail page is the live view.
function AgentUpdateCell({
  host,
  isAdmin,
  pending,
  onUpdate,
}: {
  host: Host;
  isAdmin: boolean;
  pending: boolean;
  onUpdate: (h: Host) => void;
}) {
  const statusQuery = useQuery({
    queryKey: ["agent-update", host.id],
    queryFn: () => getAgentUpdateStatus(host.id),
    staleTime: 30000,
  });

  if (statusQuery.isLoading) return <Skeleton width={96} height={22} />;

  const s = statusQuery.data;
  if (!s || !s.supported) {
    return (
      <Text size="sm" color="muted">
        -
      </Text>
    );
  }
  if (s.updateAvailable) {
    return (
      <Box direction="row" gap={2} align="center" wrap={false}>
        <Badge variant="light" color="yellow" iconLeft="upgrade-rounded">
          Update available
        </Badge>
        {isAdmin && (
          <Button
            variant="light"
            color="info"
            size="sm"
            loading={pending}
            onClick={(e) => {
              // The row itself opens the host: don't navigate away from the confirm.
              e.stopPropagation();
              onUpdate(host);
            }}
          >
            Update
          </Button>
        )}
      </Box>
    );
  }
  const upToDate = !!s.running && !!s.latest;
  return upToDate ? (
    <Badge variant="light" color="green" iconLeft="check-circle-outline-rounded">
      Up to date
    </Badge>
  ) : (
    <Tooltip content={s.error || "Could not read the registry digest."}>
      <Badge variant="light" color="gray">
        Version unknown
      </Badge>
    </Tooltip>
  );
}

/** A labelled command shown in a copyable code block. */
function CommandBlock({ label, command }: { label: string; command: string }) {
  // Local copy state.
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void navigator.clipboard.writeText(command).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <Box direction="column" gap={1}>
      <Box direction="row" justify="between" align="center">
        <Text size="xs" weight="medium" color="muted">
          {label}
        </Text>
        <Tooltip content={copied ? "Copied" : "Copy"}>
          <Button
            variant="clear"
            color={copied ? "success" : "gray"}
            iconLeft={
              copied
                ? "check-rounded"
                : "content-copy-outline-rounded"
            }
            aria-label={`Copy ${label} command`}
            onClick={copy}
          />
        </Tooltip>
      </Box>
      <Code block>{command}</Code>
    </Box>
  );
}
