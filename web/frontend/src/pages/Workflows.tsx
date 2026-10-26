import { useMemo, useState } from "react";
import {
  Badge,
  Box,
  Button,
  Icon,
  Popup,
  Table,
  Text,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import {
  deleteWorkflow,
  listWorkflowRuns,
  listWorkflows,
  runWorkflow,
} from "../api/client";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import { LIVE_REFETCH_MS } from "../lib/live";
import { useCanOperate } from "../components/deployment/shared";
import type { Workflow, WorkflowRunStatus } from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";

/** Pull a human-friendly error message out of an axios error. */

/** Cron expression from the workflow's first cron trigger, if any. */
function cronSchedule(w: Workflow): string {
  const t = (w.triggers ?? []).find((t) => t.type === "cron");
  return t?.config?.cron ?? "";
}

export default function Workflows() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const workflowsQuery = useQuery({
    queryKey: ["workflows"],
    queryFn: listWorkflows,
    refetchInterval: LIVE_REFETCH_MS,
  });

  const workflows = useMemo(
    () => workflowsQuery.data ?? [],
    [workflowsQuery.data],
  );

  // Fetch the latest run per workflow so the list can show last-run status.
  // Run history is operator-only (it can hold script output and secrets).
  const canOperate = useCanOperate();
  const runQueries = useQueries({
    queries: workflows.map((w) => ({
      queryKey: ["workflow-runs", w.id],
      queryFn: () => listWorkflowRuns(w.id),
      refetchInterval: LIVE_REFETCH_MS,
      enabled: canOperate,
    })),
  });

  // Latest run status per workflow id. The table sorts/paginates rows, so we
  // key by id instead of relying on the runQueries array index.
  const lastRunByWorkflow = useMemo(() => {
    const map = new Map<string, WorkflowRunStatus>();
    workflows.forEach((w, i) => {
      const runs = runQueries[i]?.data;
      if (runs && runs.length > 0) map.set(w.id, runs[0].status);
    });
    return map;
  }, [workflows, runQueries]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["workflows"] });

  const runMutation = useMutation({
    mutationFn: (id: string) => runWorkflow(id),
    onSuccess: (_data, id) => {
      notify({
        color: "green",
        title: "Workflow",
        message: "Run started",
      });
      queryClient.invalidateQueries({ queryKey: ["workflow-runs", id] });
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Run failed",
        message: errorMessage(err, "Could not start the workflow"),
      });
    },
  });

  const [deleteTarget, setDeleteTarget] = useState<Workflow | null>(null);

  const deleteMutation = useMutation({
    mutationFn: (w: Workflow) => deleteWorkflow(w.id),
    onSuccess: (_data, w) => {
      notify({
        color: "green",
        title: "Workflow deleted",
        message: `"${w.name}" was removed`,
      });
      setDeleteTarget(null);
      invalidate();
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Delete failed",
        message: errorMessage(err, "Could not delete the workflow"),
      });
    },
  });

  const detailPath = (w: Workflow) => `/workflows/${w.id}`;

  // Select-filter options are derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted). Labels mirror the
  // old filterLabel mapping.
  const enabledOptions = useMemo(
    () =>
      Array.from(new Set(workflows.map((w) => (w.enabled ? "enabled" : "disabled"))))
        .sort()
        .map((v) => ({ value: v, label: v === "enabled" ? "Enabled" : "Disabled" })),
    [workflows],
  );

  const columns: Column<Workflow>[] = [
    {
      key: "name",
      header: "Name",
      // Native link column: a real <a href> (open-in-new-tab works), but
      // the click is intercepted for SPA navigation through the router. The
      // updatedAt subtitle is kept via the link label override.
      type: "link",
      sortable: true,
      accessor: (w) => w.name,
      href: (w) => detailPath(w),
      link: {
        color: "info",
        label: (w) => (
          <>
            {w.name}
            <Text size="xs" color="muted">
              {formatDate(w.updatedAt)}
            </Text>
          </>
        ),
        onClick: (w, e) => {
          e.preventDefault();
          navigate(detailPath(w));
        },
      },
    },
    {
      key: "steps",
      header: "Steps",
      sortable: true,
      accessor: (w) => (w.steps ?? []).length,
      cell: (w) => <Text size="sm">{(w.steps ?? []).length}</Text>,
    },
    {
      key: "cron",
      header: "Cron",
      accessor: (w) => cronSchedule(w),
      cell: (w) =>
        cronSchedule(w) ? (
          <Text size="sm" mono>
            {cronSchedule(w)}
          </Text>
        ) : (
          <Text size="sm" color="muted">
            -
          </Text>
        ),
    },
    {
      key: "enabled",
      header: "Enabled",
      sortable: true,
      accessor: (w) => (w.enabled ? "enabled" : "disabled"),
      filter: { kind: "select", options: enabledOptions },
      filterFn: (w, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(w.enabled ? "enabled" : "disabled"),
      cell: (w) => <StatusBadge status={w.enabled ? "enabled" : "disabled"} />,
    },
    {
      key: "lastRun",
      header: "Last run",
      cell: (w) => {
        const status = lastRunByWorkflow.get(w.id);
        return status ? (
          <StatusBadge status={status} />
        ) : (
          <Text size="sm" color="muted">
            never
          </Text>
        );
      },
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (w) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(w)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (w: Workflow) => (
    <Box direction="row" wrap="nowrap" gap={1} justify="end">
      <Tooltip content="Run now">
        <Button
          variant="clear"
          color="success"
          aria-label="Run now"
          iconLeft="play-arrow-rounded"
          loading={runMutation.isPending && runMutation.variables === w.id}
          onClick={() => runMutation.mutate(w.id)}
        />
      </Tooltip>
      <Tooltip content="Delete">
        <Button
          variant="clear"
          color="danger"
          aria-label="Delete"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(w)}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Workflows"
        description="A workflow is a named, ordered list of steps run sequentially. Trigger it manually, on a cron schedule, or via a public webhook URL."
        icon={<Icon name="route-outline" size="lg" />}
        actions={
          <Button
            color="info"
            iconLeft="add-rounded"
            onClick={() => navigate("/workflows/new")}
          >
            New workflow
          </Button>
        }
      />

      <SectionCard
        plain
        title="All workflows"
        actions={
          !workflowsQuery.isLoading &&
          !workflowsQuery.isError && (
            <Badge variant="light" color="neutral" size="lg">
              {workflows.length}
            </Badge>
          )
        }
        noBodyPadding
      >
        {workflowsQuery.isError ? (
          <Text color="danger" style={{ padding: 16 }}>
            {errorMessage(workflowsQuery.error, "Failed to load workflows")}
          </Text>
        ) : (
          <Table<Workflow>
            data={workflows}
            columns={columns}
            rowKey={(w) => w.id}
            onRowClick={(w) => navigate(detailPath(w))}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={workflowsQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="route-outline" size="xl" />}
                title="No workflows yet"
                description="Create a workflow to run an ordered set of steps on demand, on a schedule, or via webhook."
                action={
                  <Button
                    color="info"
                    iconLeft="add-rounded"
                    onClick={() => navigate("/workflows/new")}
                  >
                    New workflow
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      {/* Delete confirmation */}
      <Popup
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        size="sm"
      >
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Delete workflow</Popup.Title>
          </Popup.Header>
          <Popup.Body>
            <Text>
              Permanently delete{" "}
              <Text as="span" weight="semibold">
                {deleteTarget?.name}
              </Text>
              ? Its run history is removed and any cron schedule or webhook stops
              working. This cannot be undone.
            </Text>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" wrap gap={2} justify="end">
              <Button
                variant="outline"
                color="neutral"
                onClick={() => setDeleteTarget(null)}
                disabled={deleteMutation.isPending}
              >
                Cancel
              </Button>
              <Button
                color="danger"
                loading={deleteMutation.isPending}
                onClick={() =>
                  deleteTarget && deleteMutation.mutate(deleteTarget)
                }
              >
                Delete
              </Button>
            </Box>
          </Popup.Footer>
        </Popup.Content>
      </Popup>
    </Box>
  );
}
