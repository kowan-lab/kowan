import {
  Badge,
  Box,
  Callout,
  Code,
  Icon,
  Table,
  Text,
  type Column,
} from "@/ui/kit";
import { toKitColor } from "../ui/colors";
import { useQuery } from "@tanstack/react-query";
import { listAudit } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import type { AuditEntry } from "../types";
import { errorMessage, formatDate } from "../utils";

// The audit endpoint caps a page at 500 entries server-side. We fetch the most
// recent page and let the Table handle search/sort/pagination client-side.
const FETCH_LIMIT = 500;

/** Map an audit action to a badge colour, per the design spec. */
function actionColor(action: string): string {
  switch (action.toUpperCase()) {
    case "LOGIN":
      return "blue";
    case "LOGIN_FAILED":
      return "red";
    case "POST":
      return "green";
    case "PUT":
    case "PATCH":
      return "yellow";
    case "DELETE":
      return "red";
    default:
      return "gray";
  }
}

/** Map an HTTP status code to a badge color. */
function statusColor(status: number): string {
  if (status >= 500) return "red";
  if (status >= 400) return "orange";
  if (status >= 300) return "yellow";
  if (status >= 200) return "green";
  return "gray";
}


export default function Audit() {
  const { user: currentUser } = useAuth();
  const isAdmin = currentUser?.role === "admin";

  const auditQuery = useQuery({
    queryKey: ["audit", { limit: FETCH_LIMIT }],
    queryFn: () => listAudit({ limit: FETCH_LIMIT, offset: 0 }),
    enabled: isAdmin,
  });

  if (!isAdmin) {
    return (
      <Box direction="column" gap={6}>
        <PageHeader
          title="Audit log"
          description="Review who did what and when across the platform."
          icon={<Icon name="history-rounded" size="lg" />}
        />
        <Callout
          color="danger"
          iconLeft={<Icon name="warning-outline-rounded" />}
          title="Access denied"
        >
          You need administrator privileges to view the audit log.
        </Callout>
      </Box>
    );
  }

  const items = auditQuery.data?.items ?? [];

  const columns: Column<AuditEntry>[] = [
    {
      key: "time",
      header: "Time",
      sortable: true,
      accessor: (entry) => entry.time,
      cell: (entry) => <Text size="sm">{formatDate(entry.time)}</Text>,
    },
    {
      key: "user",
      header: "User",
      sortable: true,
      accessor: (entry) => entry.username || "",
      cell: (entry) => (
        <Text weight="medium" size="sm">
          {entry.username || "-"}
        </Text>
      ),
    },
    {
      key: "action",
      header: "Action",
      sortable: true,
      accessor: (entry) => entry.action,
      cell: (entry) => (
        <Badge color={toKitColor(actionColor(entry.action))} variant="light">
          {entry.action}
        </Badge>
      ),
    },
    {
      key: "path",
      header: "Path",
      sortable: true,
      accessor: (entry) => entry.path,
      cell: (entry) =>
        entry.path ? <Code>{entry.path}</Code> : <Text color="muted">-</Text>,
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      accessor: (entry) => entry.status,
      cell: (entry) =>
        entry.status > 0 ? (
          <Badge color={toKitColor(statusColor(entry.status))} variant="light">
            {entry.status}
          </Badge>
        ) : (
          <Text color="muted">-</Text>
        ),
    },
    {
      key: "ip",
      header: "IP",
      accessor: (entry) => entry.ip || "",
      cell: (entry) => (
        <Text size="sm" color="muted">
          {entry.ip || "-"}
        </Text>
      ),
    },
  ];

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Audit log"
        description="Review who did what and when across the platform."
        icon={<Icon name="history-rounded" size="lg" />}
      />

      <SectionCard
        title="Activity"
        icon={<Icon name="history-rounded" />}
        noBodyPadding
      >
        {auditQuery.isError ? (
          <Callout
            color="danger"
            iconLeft={<Icon name="warning-outline-rounded" />}
            title="Failed to load audit log"
            style={{ margin: "1rem" }}
          >
            {errorMessage(auditQuery.error, "Could not load the audit log.")}
          </Callout>
        ) : (
          <Table<AuditEntry>
            data={items}
            columns={columns}
            rowKey={(entry) => entry.id}
            globalSearch
            pagination
            defaultPageSize={25}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={auditQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="history-rounded" size="xl" />}
                title="No activity recorded"
                description="Mutating actions and authentication events will appear here."
              />
            }
          />
        )}
      </SectionCard>
    </Box>
  );
}
