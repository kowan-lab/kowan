import { useMemo, useState } from "react";
import { toKitColor } from "../ui/colors";
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
  createUser,
  deleteUser,
  listUsers,
  updateUser,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import UserPermissionsPopup from "../auth/UserPermissionsPopup";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import type { User } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";
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


interface CreateFormValues {
  username: string;
  password: string;
  role: string;
  email: string;
}

interface EditFormValues {
  role: string;
  password: string;
  email: string;
}

function validateCreate(values: CreateFormValues): string | null {
  if (values.username.trim().length === 0) return "Username is required";
  if (values.password.length < 1) return "Password is required";
  if (!values.role) return "Role is required";
  return null;
}

function validateEdit(values: EditFormValues): string | null {
  if (!values.role) return "Role is required";
  return null;
}

const EMPTY_CREATE: CreateFormValues = {
  username: "",
  password: "",
  role: "viewer",
  email: "",
};

const EMPTY_EDIT: EditFormValues = { role: "viewer", password: "", email: "" };

export default function Users() {
  const { user: currentUser } = useAuth();
  const queryClient = useQueryClient();

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<User | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<User | null>(null);
  const [permTarget, setPermTarget] = useState<User | null>(null);

  const [createValues, setCreateValues] = useState<CreateFormValues>(EMPTY_CREATE);
  const [createError, setCreateError] = useState<string | null>(null);

  const [editValues, setEditValues] = useState<EditFormValues>(EMPTY_EDIT);
  const [editError, setEditError] = useState<string | null>(null);

  const isAdmin = currentUser?.role === "admin";

  const usersQuery = useQuery({
    queryKey: ["users"],
    queryFn: listUsers,
    enabled: isAdmin,
    refetchInterval: SLOW_REFETCH_MS,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["users"] });

  const createMutation = useMutation({
    mutationFn: (values: {
      username: string;
      password: string;
      role: string;
      email?: string;
    }) => createUser(values),
    onSuccess: (created) => {
      notify({
        color: "green",
        title: "User created",
        message: `${created.username} was created`,
      });
      setCreateOpen(false);
      setCreateValues(EMPTY_CREATE);
      setCreateError(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not create user",
        message: errorMessage(err, "Failed to create user"),
      });
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Partial<{ role: string; password: string; email: string }>;
    }) => updateUser(id, body),
    onSuccess: (updated) => {
      notify({
        color: "green",
        title: "User updated",
        message: `${updated.username} was updated`,
      });
      setEditTarget(null);
      setEditValues(EMPTY_EDIT);
      setEditError(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not update user",
        message: errorMessage(err, "Failed to update user"),
      });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteUser(id),
    onSuccess: (_data, id) => {
      const name = deleteTarget?.username ?? id;
      notify({
        color: "green",
        title: "User deleted",
        message: `${name} was deleted`,
      });
      setDeleteTarget(null);
      void invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Could not delete user",
        message: errorMessage(err, "Failed to delete user"),
      });
    },
  });

  // Select-filter options for the Role facet, derived from the data (data-mode
  // needs explicit options, unlike the old DataTable which auto-faceted). Must
  // run before the non-admin early return to respect the rules of hooks.
  const roleOptions = useMemo(
    () =>
      Array.from(
        new Set((usersQuery.data ?? []).map((u) => u.role).filter(Boolean)),
      )
        .sort()
        .map((r) => ({ value: r, label: r.charAt(0).toUpperCase() + r.slice(1) })),
    [usersQuery.data],
  );

  if (!isAdmin) {
    return (
      <Box direction="column" gap={6}>
        <PageHeader
          title="Users"
          description="Manage who can access the platform and what they can do."
          icon={<Icon name="group-outline-rounded" size="lg" />}
        />
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to manage users.
        </Callout>
      </Box>
    );
  }

  const openCreate = () => {
    setCreateValues(EMPTY_CREATE);
    setCreateError(null);
    setCreateOpen(true);
  };

  const openEdit = (u: User) => {
    setEditValues({ role: u.role, password: "", email: u.email ?? "" });
    setEditError(null);
    setEditTarget(u);
  };

  const submitCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const err = validateCreate(createValues);
    setCreateError(err);
    if (err) return;
    createMutation.mutate({
      username: createValues.username.trim(),
      password: createValues.password,
      role: createValues.role,
      email: createValues.email.trim(),
    });
  };

  const submitEdit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editTarget) return;
    const err = validateEdit(editValues);
    setEditError(err);
    if (err) return;
    const body: Partial<{ role: string; password: string; email: string }> = {
      role: editValues.role,
      email: editValues.email.trim(),
    };
    if (editValues.password.length > 0) {
      body.password = editValues.password;
    }
    updateMutation.mutate({ id: editTarget.id, body });
  };

  const users = usersQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  const columns: Column<User>[] = [
    {
      key: "username",
      header: "Username",
      sortable: true,
      accessor: (u) => u.username,
      cell: (u) => (
        <Box direction="row" wrap gap={2} align="center">
          <Text weight="medium">{u.username}</Text>
          {currentUser?.id === u.id && (
            <Badge size="sm" variant="light" color="gray">
              you
            </Badge>
          )}
        </Box>
      ),
    },
    {
      key: "email",
      header: "Email",
      sortable: true,
      accessor: (u) => u.email ?? "",
      cell: (u) =>
        u.email ? (
          <Text size="sm">{u.email}</Text>
        ) : (
          <Text size="sm" color="muted">
            —
          </Text>
        ),
    },
    {
      key: "role",
      header: "Role",
      sortable: true,
      accessor: (u) => u.role,
      filter: { kind: "select", options: roleOptions },
      filterFn: (u, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(u.role),
      cell: (u) => (
        <Badge color={toKitColor(roleColor(u.role))} variant="light">
          {u.role}
        </Badge>
      ),
    },
    {
      key: "source",
      header: "Source",
      sortable: true,
      accessor: (u) => u.authSource || "local",
      cell: (u) => (
        <Badge
          variant="light"
          color={u.authSource && u.authSource !== "local" ? "info" : "gray"}
        >
          {u.authSource || "local"}
        </Badge>
      ),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (u) => u.createdAt,
      cell: (u) => formatDate(u.createdAt),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (u) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(u)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (u: User) => {
    const isSelf = currentUser?.id === u.id;
    return (
      <Box direction="row" wrap="nowrap" gap={2} justify="end">
        <Tooltip content="Effective permissions">
          <Button
            variant="clear"
            onClick={() => setPermTarget(u)}
            aria-label={`Permissions of ${u.username}`}
            iconLeft="lock-outline"
          />
        </Tooltip>
        <Tooltip content="Edit role / reset password">
          <Button
            variant="clear"
            color="blue"
            onClick={() => openEdit(u)}
            aria-label={`Edit ${u.username}`}
            iconLeft="edit-outline-rounded"
          />
        </Tooltip>
        <Tooltip
          content={isSelf ? "You cannot delete yourself" : `Delete ${u.username}`}
        >
          <Button
            variant="clear"
            color="danger"
            disabled={isSelf}
            onClick={() => setDeleteTarget(u)}
            aria-label={`Delete ${u.username}`}
            iconLeft="delete-outline-rounded"
          />
        </Tooltip>
      </Box>
    );
  };

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Users"
        description="Manage who can access the platform and what they can do."
        icon={<Icon name="group-outline-rounded" size="lg" />}
        actions={
          <Button color="info" iconLeft="add-rounded" onClick={openCreate}>
            New user
          </Button>
        }
      />

      <SectionCard
        plain
        title="Users"
        icon={<Icon name="group-outline-rounded" />}
        actions={
          !usersQuery.isLoading && !usersQuery.isError ? (
            <Text size="sm" color="muted">
              {users.length} {users.length === 1 ? "user" : "users"}
            </Text>
          ) : undefined
        }
        noBodyPadding
      >
        {usersQuery.isError ? (
          <Box padding={4}>
            <Callout
              color="danger"
              iconLeft={<Icon name="warning-outline-rounded" />}
              title="Failed to load users"
            >
              {errorMessage(usersQuery.error, "Could not load the user list.")}
            </Callout>
          </Box>
        ) : (
          <Table<User>
            data={users}
            columns={columns}
            rowKey={(u) => u.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={usersQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="group-outline-rounded" size="xl" />}
                title="No users yet"
                description="Create a user to grant access to the platform."
                action={
                  <Button color="info" iconLeft="add-rounded" onClick={openCreate}>
                    New user
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      {/* Create user modal */}
      <Popup open={createOpen} onOpenChange={(o) => !o && setCreateOpen(false)}>
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>New user</Popup.Title>
          </Popup.Header>
          <form onSubmit={submitCreate}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                {createError && (
                  <Callout
                    color="danger"
                    iconLeft={<Icon name="warning-outline-rounded" />}
                  >
                    {createError}
                  </Callout>
                )}
                <TextInput
                  label="Username"
                  placeholder="jane"
                  autoFocus
                  value={createValues.username}
                  onChange={(e) =>
                    setCreateValues((v) => ({ ...v, username: e.target.value }))
                  }
                  fullWidth
                />
                <TextInput
                  label="Email"
                  description="Matched against SSO / LDAP identities at login (optional)"
                  placeholder="jane@example.com"
                  value={createValues.email}
                  onChange={(e) =>
                    setCreateValues((v) => ({ ...v, email: e.target.value }))
                  }
                  fullWidth
                />
                <TextInput
                  type="password"
                  label="Password"
                  placeholder="Initial password"
                  autoComplete="new-password"
                  value={createValues.password}
                  onChange={(e) =>
                    setCreateValues((v) => ({ ...v, password: e.target.value }))
                  }
                  fullWidth
                />
                <Select
                  label="Role"
                  options={ROLE_OPTIONS}
                  value={createValues.role}
                  onChange={(value: string) =>
                    setCreateValues((v) => ({ ...v, role: value }))
                  }
                  fullWidth
                />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap gap={2} justify="end">
                <Button variant="outline" onClick={() => setCreateOpen(false)}>
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

      {/* Edit user modal */}
      <Popup
        open={editTarget !== null}
        onOpenChange={(o) => !o && setEditTarget(null)}
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>
              {editTarget ? `Edit ${editTarget.username}` : "Edit user"}
            </Popup.Title>
          </Popup.Header>
          <form onSubmit={submitEdit}>
            <Popup.Body>
              <Box direction="column" gap={4}>
                {editError && (
                  <Callout
                    color="danger"
                    iconLeft={<Icon name="warning-outline-rounded" />}
                  >
                    {editError}
                  </Callout>
                )}
                <Select
                  label="Role"
                  options={ROLE_OPTIONS}
                  value={editValues.role}
                  onChange={(value: string) =>
                    setEditValues((v) => ({ ...v, role: value }))
                  }
                  fullWidth
                />
                <TextInput
                  label="Email"
                  description="Matched against SSO / LDAP identities at login"
                  placeholder="jane@example.com"
                  value={editValues.email}
                  onChange={(e) =>
                    setEditValues((v) => ({ ...v, email: e.target.value }))
                  }
                  fullWidth
                />
                <TextInput
                  type="password"
                  label="Reset password"
                  description="Leave blank to keep the current password"
                  placeholder="New password"
                  autoComplete="new-password"
                  value={editValues.password}
                  onChange={(e) =>
                    setEditValues((v) => ({ ...v, password: e.target.value }))
                  }
                  fullWidth
                />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" wrap gap={2} justify="end">
                <Button variant="outline" onClick={() => setEditTarget(null)}>
                  Cancel
                </Button>
                <Button color="info" type="submit" loading={updateMutation.isPending}>
                  Save
                </Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <UserPermissionsPopup user={permTarget} onClose={() => setPermTarget(null)} />

      {/* Delete confirmation modal */}
      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete user"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.username}
            </Text>
            ? This action cannot be undone.
          </>
        }
      />
    </Box>
  );
}
