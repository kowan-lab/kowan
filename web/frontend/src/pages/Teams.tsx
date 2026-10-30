import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Box, Button, Callout, Popup, Select, Table, Text, TextArea, TextInput, type Column } from "@/ui/kit";
import {
  addTeamMember,
  createGrant,
  createTeam,
  deleteGrant,
  deleteTeam,
  listTeams,
  listUsers,
  removeTeamMember,
  updateTeam,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import GrantFields, { EMPTY_GRANT, GrantBadge, describeGrant, grantValid, useScopeNames } from "../auth/GrantFields";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import type { Grant, GrantInput, Team, TeamMember } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";

export default function Teams() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const qc = useQueryClient();
  const names = useScopeNames();

  const teamsQ = useQuery({ queryKey: ["teams"], queryFn: listTeams, enabled: isAdmin });
  const usersQ = useQuery({ queryKey: ["users"], queryFn: listUsers, enabled: isAdmin });
  const teams = useMemo(() => teamsQ.data ?? [], [teamsQ.data]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = teams.find((t) => t.id === selectedId) ?? null;

  const [editing, setEditing] = useState<{ id?: string; name: string; description: string } | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const [memberPick, setMemberPick] = useState<string | null>(null);
  const [grantOpen, setGrantOpen] = useState(false);
  const [grant, setGrant] = useState<GrantInput>(EMPTY_GRANT);
  const [grantError, setGrantError] = useState<string | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: ["teams"] });
  const fail = (title: string) => (err: unknown) => notify({ color: "red", title, message: errorMessage(err, title) });

  const saveTeam = useMutation({
    mutationFn: (v: { id?: string; name: string; description: string }) =>
      v.id ? updateTeam(v.id, { name: v.name, description: v.description }) : createTeam({ name: v.name, description: v.description }),
    onSuccess: (t) => {
      setEditing(null);
      setSelectedId(t.id);
      void refresh();
    },
    onError: fail("Could not save team"),
  });
  const removeTeam = useMutation({
    mutationFn: (id: string) => deleteTeam(id),
    onSuccess: () => {
      setDeleting(null);
      setSelectedId(null);
      void refresh();
    },
    onError: fail("Could not delete team"),
  });
  const addMember = useMutation({
    mutationFn: (v: { teamId: string; userId: string }) => addTeamMember(v.teamId, v.userId),
    onSuccess: () => {
      setMemberPick(null);
      void refresh();
    },
    onError: fail("Could not add member"),
  });
  const dropMember = useMutation({
    mutationFn: (v: { teamId: string; userId: string }) => removeTeamMember(v.teamId, v.userId),
    onSuccess: () => void refresh(),
    onError: fail("Could not remove member"),
  });
  const addGrant = useMutation({
    mutationFn: (v: { teamId: string; g: GrantInput }) => createGrant({ ...v.g, subjectType: "team", subjectId: v.teamId }),
    onSuccess: () => {
      setGrantOpen(false);
      void refresh();
    },
    onError: (err) => setGrantError(errorMessage(err, "Could not add grant")),
  });
  const dropGrant = useMutation({
    mutationFn: (id: string) => deleteGrant(id),
    onSuccess: () => void refresh(),
    onError: fail("Could not remove grant"),
  });

  if (!isAdmin) {
    return (
      <Box direction="column" gap={5}>
        <PageHeader title="Teams" />
        <Callout color="danger" title="Access denied">You need administrator privileges to manage teams.</Callout>
      </Box>
    );
  }

  const teamColumns: Column<Team>[] = [
    { key: "name", header: "Team", sortable: true, accessor: (t) => t.name, cell: (t) => <Text weight="medium">{t.name}</Text> },
    { key: "description", header: "Description", accessor: (t) => t.description, cell: (t) => <Text color="muted" truncate>{t.description || "—"}</Text>, grow: true },
    { key: "members", header: "Members", numeric: true, accessor: (t) => t.members.length },
    { key: "grants", header: "Grants", numeric: true, accessor: (t) => t.grants?.length ?? 0 },
    { key: "created", header: "Created", accessor: (t) => t.createdAt, cell: (t) => formatDate(t.createdAt) },
  ];

  const memberColumns: Column<TeamMember>[] = [
    { key: "username", header: "User", accessor: (m) => m.username, cell: (m) => <Text weight="medium">{m.username || m.userId}</Text> },
    {
      key: "source",
      header: "Source",
      accessor: (m) => m.source,
      cell: (m) => (m.source === "manual" ? <Text color="muted">manual</Text> : <Badge color="blue" variant="outline">synced from SSO</Badge>),
    },
    { key: "added", header: "Added", accessor: (m) => m.addedAt, cell: (m) => formatDate(m.addedAt) },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (m) =>
        selected && (
          <Button
            variant="clear"
            color="danger"
            size="sm"
            iconLeft="delete-outline-rounded"
            aria-label={`Remove ${m.username}`}
            onClick={() => dropMember.mutate({ teamId: selected.id, userId: m.userId })}
          />
        ),
    },
  ];

  const grantColumns: Column<Grant>[] = [
    { key: "scope", header: "Scope", accessor: (g) => g.scope, cell: (g) => <Text>{describeGrant(g, names.hostName, names.depName)}</Text>, grow: true },
    { key: "level", header: "Level", accessor: (g) => g.level, cell: (g) => <GrantBadge level={g.level} /> },
    { key: "by", header: "Granted by", accessor: (g) => g.createdBy ?? "", cell: (g) => <Text color="muted">{g.createdBy || "—"}</Text> },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (g) => (
        <Button variant="clear" color="danger" size="sm" iconLeft="delete-outline-rounded" aria-label="Remove grant" onClick={() => dropGrant.mutate(g.id)} />
      ),
    },
  ];

  const memberIds = new Set(selected?.members.map((m) => m.userId));
  const candidates = (usersQ.data ?? []).filter((u) => !memberIds.has(u.id) && u.role !== "admin");

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Teams"
        description="Group users and grant them access to hosts, deployments, stacks or labelled resources. Roles stay the ceiling."
        actions={
          <Button iconLeft="add-rounded" onClick={() => setEditing({ name: "", description: "" })}>
            New team
          </Button>
        }
      />

      <Callout variant="light" color="info" title="How scoping works">
        A user who belongs to a team (or holds a direct grant) only sees and acts on what the grants cover, up to their role:
        viewers read, operators can operate and manage. Users in no team keep their role across everything. Admins are never restricted.
      </Callout>

      <SectionCard plain noBodyPadding>
        <Table<Team>
          data={teams}
          columns={teamColumns}
          rowKey={(t) => t.id}
          loading={teamsQ.isLoading}
          onRowClick={(t) => setSelectedId(t.id)}
          hoverable
          emptyState={
            <EmptyState
              title="No teams yet"
              description="Without teams every user acts with their global role on every host, as before."
              action={<Button iconLeft="add-rounded" onClick={() => setEditing({ name: "", description: "" })}>New team</Button>}
            />
          }
        />
      </SectionCard>

      {selected && (
        <>
          <SectionCard
            title={`${selected.name} · members`}
            description={selected.description || undefined}
            noBodyPadding
            actions={
              <Box direction="row" gap={2} align="center">
                <Select
                  aria-label="Add member"
                  placeholder="Add a user…"
                  searchable
                  width={200}
                  size="sm"
                  options={candidates.map((u) => ({ value: u.id, label: `${u.username} (${u.role})` }))}
                  value={memberPick}
                  onChange={(v) => setMemberPick(v)}
                />
                <Button size="sm" disabled={!memberPick} loading={addMember.isPending} onClick={() => memberPick && addMember.mutate({ teamId: selected.id, userId: memberPick })}>
                  Add
                </Button>
                <Button size="sm" variant="outline" onClick={() => setEditing({ id: selected.id, name: selected.name, description: selected.description })}>
                  Edit
                </Button>
                <Button size="sm" variant="outline" color="danger" onClick={() => setDeleting(selected)}>
                  Delete
                </Button>
              </Box>
            }
          >
            <Table<TeamMember>
              data={selected.members}
              columns={memberColumns}
              rowKey={(m) => m.userId}
              emptyState={<EmptyState title="No members" description="Add users to apply this team's grants to them." />}
            />
          </SectionCard>

          <SectionCard
            title={`${selected.name} · grants`}
            description="What members of this team may access, and how much."
            noBodyPadding
            actions={
              <Button
                size="sm"
                iconLeft="add-rounded"
                onClick={() => {
                  setGrant(EMPTY_GRANT);
                  setGrantError(null);
                  setGrantOpen(true);
                }}
              >
                Add grant
              </Button>
            }
          >
            <Table<Grant>
              data={selected.grants ?? []}
              columns={grantColumns}
              rowKey={(g) => g.id}
              emptyState={<EmptyState title="No grants" description="Members of a team without grants can see nothing. Add a grant to give access." />}
            />
          </SectionCard>
        </>
      )}

      <Popup open={editing !== null} onOpenChange={(o) => !o && setEditing(null)}>
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>{editing?.id ? "Edit team" : "New team"}</Popup.Title>
          </Popup.Header>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (editing && editing.name.trim()) saveTeam.mutate({ ...editing, name: editing.name.trim() });
            }}
          >
            <Popup.Body>
              <Box direction="column" gap={4}>
                <TextInput label="Name" autoFocus value={editing?.name ?? ""} onChange={(e) => setEditing((v) => v && { ...v, name: e.target.value })} />
                <TextArea label="Description" value={editing?.description ?? ""} onChange={(e) => setEditing((v) => v && { ...v, description: e.target.value })} />
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" justify="end" gap={2}>
                <Button variant="outline" onClick={() => setEditing(null)}>Cancel</Button>
                <Button type="submit" loading={saveTeam.isPending} disabled={!editing?.name.trim()}>Save</Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <Popup open={grantOpen} onOpenChange={(o) => !o && setGrantOpen(false)}>
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Add grant to {selected?.name}</Popup.Title>
          </Popup.Header>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const err = grantValid(grant);
              setGrantError(err);
              if (!err && selected) addGrant.mutate({ teamId: selected.id, g: grant });
            }}
          >
            <Popup.Body>
              <Box direction="column" gap={4}>
                <GrantFields value={grant} onChange={setGrant} />
                {grantError && <Callout color="danger">{grantError}</Callout>}
              </Box>
            </Popup.Body>
            <Popup.Footer>
              <Box direction="row" justify="end" gap={2}>
                <Button variant="outline" onClick={() => setGrantOpen(false)}>Cancel</Button>
                <Button type="submit" loading={addGrant.isPending}>Add grant</Button>
              </Box>
            </Popup.Footer>
          </form>
        </Popup.Content>
      </Popup>

      <ConfirmModal
        opened={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete team"
        confirmLabel="Delete"
        loading={removeTeam.isPending}
        onConfirm={() => deleting && removeTeam.mutate(deleting.id)}
        message={
          <>
            Delete <Text as="span" weight="semibold">{deleting?.name}</Text> and its grants? Members who belong to no other team go back to
            their global role on every host. A team with API keys bound to it cannot be deleted: revoke those keys first.
          </>
        }
      />
    </Box>
  );
}
