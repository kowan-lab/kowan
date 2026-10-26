import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Callout,
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
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  createChannel,
  deleteChannel,
  listChannels,
  listConnections,
  testChannel,
  updateChannel,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import ConnectionField from "../components/ConnectionField";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import DigestCard from "../components/notifications/DigestCard";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import { SLOW_REFETCH_MS } from "../lib/live";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";
import { toKitColor } from "../ui/colors";
import type {
  NotificationChannel,
  NotificationChannelType,
} from "../types";


const CHANNEL_TYPES: { value: NotificationChannelType; label: string }[] = [
  { value: "smtp", label: "Email (SMTP)" },
  { value: "webhook", label: "Webhook" },
  { value: "ntfy", label: "ntfy" },
  { value: "telegram", label: "Telegram" },
  { value: "slack", label: "Slack" },
  { value: "discord", label: "Discord" },
];

const TYPE_COLORS: Record<string, string> = {
  smtp: "blue",
  webhook: "magenta",
  ntfy: "teal",
  telegram: "cyan",
  slack: "purple",
  discord: "blue",
};

const LEVELS: { value: string; label: string }[] = [
  { value: "info", label: "Info" },
  { value: "warning", label: "Warning" },
  { value: "critical", label: "Critical" },
];

function typeLabel(type: string): string {
  return CHANNEL_TYPES.find((t) => t.value === type)?.label ?? type;
}

interface ChannelFormState {
  name: string;
  minLevel: string;
  enabled: boolean;
  /**
   * The shared connection (by id) whose type implements `messageSend`. The
   * connection provides the entire transport — the channel carries no inline
   * config. The backend still needs a `type`, derived from this connection.
   */
  connectionRef: string;
}

function emptyForm(): ChannelFormState {
  return {
    name: "",
    minLevel: "warning",
    enabled: true,
    connectionRef: "",
  };
}

/** Seed the form for editing: name, level, enabled and the referenced connection. */
function formFromChannel(channel: NotificationChannel): ChannelFormState {
  return {
    name: channel.name,
    minLevel: channel.minLevel || "warning",
    enabled: channel.enabled,
    connectionRef: channel.connectionRef ?? "",
  };
}

export default function Notifications({ embedded }: { embedded?: boolean } = {}) {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();

  const isAdmin = currentUser?.role === "admin";

  const [formOpen, setFormOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<NotificationChannel | null>(null);
  const [form, setForm] = useState<ChannelFormState>(emptyForm());
  const [deleteTarget, setDeleteTarget] = useState<NotificationChannel | null>(
    null,
  );
  const [testingId, setTestingId] = useState<string | null>(null);

  const channelsQuery = useQuery({
    queryKey: ["channels"],
    queryFn: listChannels,
    enabled: isAdmin,
    // Channels are configuration that changes rarely — poll on the slow tier.
    refetchInterval: SLOW_REFETCH_MS,
  });

  // Shared connections cache (same key ConnectionField uses). Used to derive the
  // channel `type` the backend expects from the SELECTED connection's type.
  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: listConnections,
    enabled: isAdmin,
  });

  // The type of the currently selected connection — this becomes the channel's
  // transport type on submit. Falls back to the edited channel's existing type.
  const derivedType = useMemo(() => {
    const selected = (connectionsQuery.data ?? []).find(
      (c) => c.id === form.connectionRef,
    );
    return selected?.type ?? editTarget?.type ?? "";
  }, [connectionsQuery.data, form.connectionRef, editTarget]);

  // Select-filter options for the Type column, derived from the data (data-mode
  // needs explicit options, unlike the old DataTable which auto-faceted). Labels
  // come from typeLabel(), matching the old column's filterLabel.
  const typeOptions = useMemo(
    () =>
      Array.from(
        new Set((channelsQuery.data ?? []).map((ch) => ch.type).filter(Boolean)),
      )
        .sort()
        .map((v) => ({ value: v, label: typeLabel(v) })),
    [channelsQuery.data],
  );

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["channels"] });

  const saveMutation = useMutation({
    mutationFn: () => {
      // The transport always comes from the referenced connection. On update the
      // type is immutable, so it is not sent; on create the type is derived from
      // the selected connection and there is no inline config.
      if (editTarget) {
        return updateChannel(editTarget.id, {
          name: form.name.trim(),
          enabled: form.enabled,
          minLevel: form.minLevel,
          connectionRef: form.connectionRef,
        });
      }
      return createChannel({
        name: form.name.trim(),
        type: derivedType,
        enabled: form.enabled,
        minLevel: form.minLevel,
        connectionRef: form.connectionRef,
        config: {},
      });
    },
    onSuccess: (saved) => {
      notify({
        color: "green",
        title: editTarget ? "Channel updated" : "Channel created",
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
        title: "Could not save channel",
        message: errorMessage(err, "Failed to save channel"),
      });
    },
  });

  // Toggle enabled directly from the table row.
  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      updateChannel(id, { enabled }),
    onSuccess: () => {
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not update channel",
        message: errorMessage(err, "Failed to update channel"),
      });
      void invalidate();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteChannel(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.name ?? id;
      notify({
        color: "green",
        title: "Channel deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete channel",
        message: errorMessage(err, "Failed to delete channel"),
      });
    },
  });

  const testMutation = useMutation({
    mutationFn: (channel: NotificationChannel) => {
      setTestingId(channel.id);
      return testChannel(channel.id);
    },
    onSuccess: (_data, channel) => {
      setTestingId(null);
      notify({
        color: "green",
        title: "Test sent",
        message: `A test message was sent via ${channel.name}.`,
      });
    },
    onError: (err) => {
      setTestingId(null);
      notify({
        color: "red",
        title: "Test failed",
        message: errorMessage(err, "Could not send the test message."),
      });
    },
  });

  if (!isAdmin) {
    return (
      <Box direction="column" gap={4}>
        {!embedded && (
          <PageHeader
            title="Notifications"
            description="Deliver monitoring alerts to email, chat and webhook destinations. Each channel receives only alerts at or above its minimum level."
            icon={<Icon name="notifications-outline-rounded" size="lg" />}
          />
        )}
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage notification channels.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    setEditTarget(null);
    setForm(emptyForm());
    setFormOpen(true);
  };

  const openEdit = (channel: NotificationChannel) => {
    setEditTarget(channel);
    setForm(formFromChannel(channel));
    setFormOpen(true);
  };

  const submitForm = (e: React.FormEvent) => {
    e.preventDefault();
    if (form.name.trim().length === 0) {
      notify({
        color: "red",
        title: "Name is required",
        message: "Give the channel a name.",
      });
      return;
    }
    if (form.connectionRef.length === 0) {
      notify({
        color: "red",
        title: "Select a credential",
        message: "Create a notification credential first, then select it.",
      });
      return;
    }
    saveMutation.mutate();
  };

  const channels = channelsQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  const columns: Column<NotificationChannel>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      accessor: (ch) => ch.name,
      cell: (ch) => <Text weight="medium">{ch.name}</Text>,
    },
    {
      key: "type",
      header: "Type",
      sortable: true,
      accessor: (ch) => ch.type,
      filter: { kind: "select", options: typeOptions },
      filterFn: (ch, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(ch.type),
      cell: (ch) => (
        <Badge
          variant="light"
          color={toKitColor(TYPE_COLORS[ch.type] ?? "gray")}
        >
          {typeLabel(ch.type)}
        </Badge>
      ),
    },
    {
      key: "minLevel",
      header: "Min level",
      sortable: true,
      accessor: (ch) => ch.minLevel,
      cell: (ch) => <StatusBadge status={ch.minLevel} />,
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (ch) => (
        <InputToggle
          variant="inline"
          checked={ch.enabled}
          onChange={(e) =>
            toggleMutation.mutate({
              id: ch.id,
              enabled: e.target.checked,
            })
          }
          aria-label={`Toggle ${ch.name}`}
        />
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (ch) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(ch)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (channel: NotificationChannel) => (
    <Box direction="row" gap={1} justify="end" wrap="nowrap">
      <Tooltip content="Send a test message">
        <Button
          variant="clear"
          color="green"
          iconLeft="send-outline-rounded"
          loading={testMutation.isPending && testingId === channel.id}
          onClick={() => testMutation.mutate(channel)}
          aria-label={`Test ${channel.name}`}
        />
      </Tooltip>
      <Tooltip content="Edit channel">
        <Button
          variant="clear"
          color="blue"
          iconLeft="edit-outline-rounded"
          onClick={() => openEdit(channel)}
          aria-label={`Edit ${channel.name}`}
        />
      </Tooltip>
      <Tooltip content={`Delete ${channel.name}`}>
        <Button
          variant="clear"
          color="danger"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(channel)}
          aria-label={`Delete ${channel.name}`}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={4}>
      {embedded ? (
        <Box direction="row" justify="end" style={{ marginBottom: 16 }}>
          <Button
            color="info"
            iconLeft="add-rounded"
            onClick={openCreate}
            disabled={connectionsQuery.isLoading || connectionsQuery.isError}
          >
            New channel
          </Button>
        </Box>
      ) : (
        <PageHeader
          title="Notifications"
          description="Deliver monitoring alerts to email, chat and webhook destinations. Each channel receives only alerts at or above its minimum level."
          icon={<Icon name="notifications-outline-rounded" size="lg" />}
          actions={
            <Button
              color="info"
              iconLeft="add-rounded"
              onClick={openCreate}
              disabled={connectionsQuery.isLoading || connectionsQuery.isError}
            >
              New channel
            </Button>
          }
        />
      )}

      <SectionCard
        plain
        title="Channels"
        icon={<Icon name="notifications-outline-rounded" />}
        actions={
          !channelsQuery.isLoading && !channelsQuery.isError ? (
            <Text size="sm" color="muted">
              {channels.length} {channels.length === 1 ? "channel" : "channels"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {channelsQuery.isError ? (
          <Callout
            color="danger"
            iconLeft={<Icon name="warning-outline-rounded" />}
            title="Failed to load channels"
            style={{ margin: 16 }}
          >
            {errorMessage(
              channelsQuery.error,
              "Could not load the channel list.",
            )}
          </Callout>
        ) : (
          <Table<NotificationChannel>
            data={channels}
            columns={columns}
            rowKey={(ch) => ch.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={channelsQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={
                  <Icon
                    name="notifications-outline-rounded"
                    size="xl"
                  />
                }
                title="No notification channels yet"
                description="Add a channel to start receiving monitoring alerts."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={openCreate}
                    disabled={
                      connectionsQuery.isLoading || connectionsQuery.isError
                    }
                  >
                    New channel
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
        onOpenChange={setFormOpen}
        size="lg"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {editTarget ? `Edit ${editTarget.name}` : "New channel"}
            </Popup.Title>
          </Popup.Header>
          <Popup.Body>
            <form onSubmit={submitForm}>
              <Box direction="column" gap={3}>
                <TextInput
                  label="Name"
                  placeholder="Ops email"
                  autoFocus
                  value={form.name}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, name: e.target.value }))
                  }
                />
                <Select
                  label="Minimum level"
                  description="Only alerts at or above this level are delivered."
                  options={LEVELS}
                  value={form.minLevel}
                  onChange={(v) =>
                    setForm((f) => ({ ...f, minLevel: (v as string) ?? "warning" }))
                  }
                />
                <InputToggle
                  label="Enabled"
                  checked={form.enabled}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, enabled: e.target.checked }))
                  }
                />

                <ConnectionField
                  capability="messageSend"
                  value={form.connectionRef}
                  onChange={(ref) =>
                    setForm((f) => ({ ...f, connectionRef: ref }))
                  }
                  isEdit={!!editTarget}
                />

                <Box direction="row" justify="end" gap={2} style={{ marginTop: 4 }}>
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
              </Box>
            </form>
          </Popup.Body>
        </Popup.Content>
      </Popup>

      <DigestCard channels={channels} />

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete channel"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? Alerts will no longer be delivered through this channel.
          </>
        }
      />
    </Box>
  );
}
