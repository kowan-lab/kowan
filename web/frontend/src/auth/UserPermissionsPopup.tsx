import { useQuery } from "@tanstack/react-query";
import { Badge, Box, Callout, Popup, Skeleton, Table, Text, type Column } from "@/ui/kit";
import { getUserPermissions } from "../api/client";
import type { EffectiveGrant, User } from "../types";
import { errorMessage, formatDate } from "../utils";
import { GrantBadge, describeGrant, useScopeNames } from "./GrantFields";

const MODE_TEXT: Record<string, string> = {
  admin: "Administrator: every check is bypassed.",
  global: "Not in any team: the role applies to every host, as without teams.",
  scoped: "Scoped: only what the grants below cover, capped by the role.",
};

/** Effective permissions of one user: role ceiling, teams and every grant. */
export default function UserPermissionsPopup({ user, onClose }: { user: User | null; onClose: () => void }) {
  const names = useScopeNames();
  const q = useQuery({
    queryKey: ["user-permissions", user?.id],
    queryFn: () => getUserPermissions(user?.id ?? ""),
    enabled: user !== null,
  });
  const p = q.data;
  const columns: Column<EffectiveGrant>[] = [
    { key: "scope", header: "Scope", grow: true, accessor: (g) => g.scope, cell: (g) => <Text>{describeGrant(g, names.hostName, names.depName)}</Text> },
    {
      key: "level",
      header: "Level",
      accessor: (g) => g.effectiveLevel,
      cell: (g) => (
        <Box direction="row" gap={2} align="center">
          <GrantBadge level={g.effectiveLevel} />
          {g.effectiveLevel !== g.level && <Text size="xs" color="muted">granted {g.level}, capped by role</Text>}
        </Box>
      ),
    },
    { key: "source", header: "Via", accessor: (g) => g.sourceName, cell: (g) => <Text color="muted">{g.sourceName}</Text> },
  ];

  return (
    <Popup open={user !== null} onOpenChange={(o) => !o && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Permissions · {user?.username}</Popup.Title>
        </Popup.Header>
        <Popup.Body>
          {q.isLoading && <Skeleton height={120} />}
          {q.isError && <Callout color="danger">{errorMessage(q.error, "Could not load permissions")}</Callout>}
          {p && (
            <Box direction="column" gap={4}>
              <Box direction="row" gap={2} align="center" wrap>
                <Badge variant="outline">role {p.role}</Badge>
                <Badge variant="outline" color={p.mode === "scoped" ? "blue" : p.mode === "admin" ? "red" : "gray"}>{p.mode}</Badge>
                {p.mode !== "admin" && <Text size="sm" color="muted">ceiling: {p.ceiling}</Text>}
              </Box>
              <Text size="sm">{MODE_TEXT[p.mode]}</Text>
              {p.teams.length > 0 && (
                <Box direction="row" gap={2} align="center" wrap>
                  <Text size="sm" weight="medium">Teams</Text>
                  {p.teams.map((t) => (
                    <Badge key={t.teamId} variant="outline">
                      {t.teamName}
                      {t.source !== "manual" ? " · sso" : ""}
                    </Badge>
                  ))}
                </Box>
              )}
              {p.mode === "scoped" && (
                <Table<EffectiveGrant>
                  data={p.grants}
                  columns={columns}
                  rowKey={(g) => `${g.id}-${g.source}`}
                  emptyState={<Text color="muted">No grants: this user can see nothing yet.</Text>}
                />
              )}
              {p.identities && p.identities.length > 0 && (
                <Box direction="column" gap={1}>
                  <Text size="sm" weight="medium">Linked identities</Text>
                  {p.identities.map((i) => (
                    <Text key={`${i.providerId}-${i.subject}`} size="sm" color="muted">
                      {i.provider || i.providerId} · {i.email || i.subject}
                      {i.lastLogin ? ` · last sign-in ${formatDate(i.lastLogin)}` : ""}
                    </Text>
                  ))}
                </Box>
              )}
            </Box>
          )}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}
