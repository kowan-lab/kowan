import { useEffect, useMemo, useState } from "react";
import {
  Box,
  Button,
  Icon,
  Popup,
  NumberInput,
  Select,
  InputToggle,
  Table,
  Text,
  Tabs,
  TextInput,
  Tooltip,
  type Column,
} from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  alertRuleTypes,
  createAlertRule,
  getRuleRunbook,
  listRunbooks,
  saveRuleRunbook,
  deleteAlertRule,
  listAlertRules,
  listAlerts,
  listChannels,
  listHosts,
  resolveAlert,
  updateAlertRule,
  type AlertRuleInput,
} from "../api/client";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import StatusBadge from "../components/StatusBadge";
import type {
  Alert,
  AlertRule,
  AlertRuleType,
  AlertRuleTypeSpec,
  Host,
  NotificationChannel,
} from "../types";
import { errorMessage, formatDate } from "../utils";
import { notify } from "../ui/toast";
import { LIVE_REFETCH_MS, SLOW_REFETCH_MS } from "../lib/live";

const OPERATOR_OPTIONS = [
  { value: ">", label: "> greater than" },
  { value: ">=", label: "≥ at least" },
  { value: "<", label: "< less than" },
  { value: "<=", label: "≤ at most" },
  { value: "==", label: "= equals" },
];

const SEVERITY_OPTIONS = [
  { value: "", label: "Type default" },
  { value: "info", label: "Info" },
  { value: "warning", label: "Warning" },
  { value: "critical", label: "Critical" },
];

const DURATION_OPTIONS = [
  { value: "0", label: "Fire immediately" },
  { value: "60", label: "Sustained 1 min" },
  { value: "300", label: "Sustained 5 min" },
  { value: "600", label: "Sustained 10 min" },
  { value: "1800", label: "Sustained 30 min" },
];


export default function Monitoring() {
  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Monitoring"
        description="Track alerts raised across your hosts and manage the rules that trigger them."
        icon={<Icon name="monitor-heart-outline-rounded" size="lg" />}
      />
      <Tabs defaultValue="alerts">
        <Tabs.List>
          <Tabs.Trigger value="alerts" iconLeft="notifications-outline-rounded">
            Alerts
          </Tabs.Trigger>
          <Tabs.Trigger value="rules" iconLeft="warning-outline-rounded">
            Alert rules
          </Tabs.Trigger>
        </Tabs.List>

        <Tabs.Panel value="alerts">
          <Box direction="column" style={{ paddingTop: 16 }}>
            <AlertsTab />
          </Box>
        </Tabs.Panel>
        <Tabs.Panel value="rules">
          <Box direction="column" style={{ paddingTop: 16 }}>
            <AlertRulesTab />
          </Box>
        </Tabs.Panel>
      </Tabs>
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Shared host lookup
// ---------------------------------------------------------------------------

function useHostLookup() {
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hosts: Host[] = hostsQuery.data ?? [];
  const nameFor = (hostId: string): string => {
    if (!hostId) return "All hosts";
    return hosts.find((h) => h.id === hostId)?.name ?? hostId;
  };
  return { hosts, nameFor };
}

// ---------------------------------------------------------------------------
// Alerts tab
// ---------------------------------------------------------------------------

function AlertsTab() {
  const queryClient = useQueryClient();
  const { nameFor } = useHostLookup();

  const alertsQuery = useQuery({
    queryKey: ["alerts"],
    queryFn: () => listAlerts(100),
    refetchInterval: LIVE_REFETCH_MS,
  });

  const resolveMutation = useMutation({
    mutationFn: (id: string) => resolveAlert(id),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Alert resolved",
        message: "The alert has been marked as resolved.",
      });
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to resolve alert",
        message: errorMessage(err, "Could not resolve the alert."),
      });
    },
  });

  const alerts = alertsQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  // Select-filter options are derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted).
  const levelOptions = useMemo(
    () =>
      Array.from(new Set(alerts.map((a) => a.level).filter(Boolean)))
        .sort()
        .map((v) => ({
          value: v,
          label: v.charAt(0).toUpperCase() + v.slice(1),
        })),
    [alerts],
  );

  const columns: Column<Alert>[] = [
    {
      key: "level",
      header: "Level",
      sortable: true,
      accessor: (a) => a.level,
      filter: { kind: "select", options: levelOptions },
      filterFn: (a, value) =>
        !Array.isArray(value) || value.length === 0 || value.includes(a.level),
      cell: (a) => <StatusBadge status={a.level} />,
    },
    {
      key: "rule",
      header: "Rule",
      sortable: true,
      accessor: (a) => a.title,
      cell: (a) => a.title,
    },
    {
      key: "resource",
      header: "Resource",
      accessor: (a) => a.resource ?? "",
      cell: (a) =>
        a.resource ? (
          <Text size="sm" mono>
            {a.resource}
          </Text>
        ) : (
          <Text size="sm" color="muted">
            —
          </Text>
        ),
    },
    {
      key: "message",
      header: "Message",
      accessor: (a) => a.message,
      cell: (a) => (
        <Text size="sm" color="muted" lines={2}>
          {a.message}
        </Text>
      ),
    },
    {
      key: "host",
      header: "Host",
      sortable: true,
      accessor: (a) => nameFor(a.hostId),
      cell: (a) => nameFor(a.hostId),
    },
    {
      key: "created",
      header: "Created",
      sortable: true,
      accessor: (a) => a.createdAt,
      cell: (a) => formatDate(a.createdAt),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      accessor: (a) => (a.resolved ? "resolved" : "open"),
      cell: (a) => (
        <StatusBadge
          status={a.resolved ? "resolved" : "warning"}
          value={a.resolved ? "Resolved" : "Open"}
        />
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (a) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(a)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (alert: Alert) =>
    !alert.resolved ? (
      <Tooltip content="Resolve">
        <Button
          variant="clear"
          color="success"
          aria-label="Resolve alert"
          iconLeft="check-rounded"
          loading={
            resolveMutation.isPending && resolveMutation.variables === alert.id
          }
          onClick={() => resolveMutation.mutate(alert.id)}
        />
      </Tooltip>
    ) : null;

  return (
    <SectionCard
      title="Alerts"
      icon={<Icon name="notifications-outline-rounded" size="md" />}
      description="The most recent alerts raised across your hosts."
      actions={
        !alertsQuery.isLoading && !alertsQuery.isError ? (
          <Text size="sm" color="muted">
            {alerts.length} {alerts.length === 1 ? "alert" : "alerts"}
          </Text>
        ) : undefined
      }
      noBodyPadding
    >
      {alertsQuery.isError ? (
        <Text color="danger" as="p">
          Failed to load alerts.
        </Text>
      ) : (
        <Table<Alert>
          data={alerts}
          columns={columns}
          rowKey={(a) => a.id}
          globalSearch
          pagination
          defaultPageSize={50}
          pageSizeOptions={[10, 25, 50, 100]}
          loading={alertsQuery.isLoading}
          stickyHeader
          striped
          hoverable
          emptyState={
            <EmptyState
              icon={<Icon name="notifications-off-outline-rounded" size="xl" />}
              title="No alerts"
              description="When a rule trips, the resulting alert will show up here."
            />
          }
        />
      )}
    </SectionCard>
  );
}

// ---------------------------------------------------------------------------
// Alert rules tab
// ---------------------------------------------------------------------------

interface RuleFormValues {
  name: string;
  type: AlertRuleType;
  operator: string;
  threshold: number;
  severity: string;
  forDuration: number;
  hostId: string;
  match: string;
  labelKey: string;
  labelValue: string;
  channelId: string;
  autoResolve: boolean;
  enabled: boolean;
  /** A runbook to run when the rule fires ("" none), at most once per cooldown. */
  runbookId: string;
  cooldownMinutes: number;
}

function defaultsFor(spec: AlertRuleTypeSpec | undefined): Partial<RuleFormValues> {
  return {
    operator: spec?.defaultOperator ?? ">",
    threshold: spec?.defaultThreshold ?? 0,
    severity: "",
  };
}

function blankRule(spec: AlertRuleTypeSpec | undefined): RuleFormValues {
  return {
    name: "",
    type: (spec?.type ?? "cpu_high") as AlertRuleType,
    operator: spec?.defaultOperator ?? ">",
    threshold: spec?.defaultThreshold ?? 0,
    severity: "",
    forDuration: 0,
    hostId: "",
    match: "",
    labelKey: "",
    labelValue: "",
    channelId: "",
    autoResolve: true,
    enabled: true,
    runbookId: "",
    cooldownMinutes: 10,
  };
}

function ruleToForm(rule: AlertRule): RuleFormValues {
  return {
    name: rule.name,
    type: rule.type,
    operator: rule.operator || ">",
    threshold: rule.threshold,
    severity: rule.severity || "",
    forDuration: rule.forDuration || 0,
    hostId: rule.hostId || "",
    match: rule.match || "",
    labelKey: rule.labelKey || "",
    labelValue: rule.labelValue || "",
    channelId: rule.channelId || "",
    autoResolve: rule.autoResolve,
    enabled: rule.enabled,
    runbookId: "",
    cooldownMinutes: 10,
  };
}

function AlertRulesTab() {
  const queryClient = useQueryClient();
  const { hosts, nameFor } = useHostLookup();

  const [createOpen, setCreateOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<AlertRule | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<AlertRule | null>(null);

  const rulesQuery = useQuery({
    queryKey: ["alert-rules"],
    queryFn: listAlertRules,
    refetchInterval: SLOW_REFETCH_MS,
  });
  const typesQuery = useQuery({ queryKey: ["alert-rule-types"], queryFn: alertRuleTypes });
  const channelsQuery = useQuery({ queryKey: ["channels"], queryFn: listChannels });

  const ruleTypes = typesQuery.data ?? [];
  const channels = channelsQuery.data ?? [];
  const specFor = (t: AlertRuleType) => ruleTypes.find((s) => s.type === t);

  const createMutation = useMutation({
    mutationFn: async (values: RuleFormValues) => {
      const rule = await createAlertRule(toInput(values));
      if (values.runbookId) await saveRuleRunbook(rule.id, values.runbookId, values.cooldownMinutes);
      return rule;
    },
    onSuccess: () => {
      notify({
        color: "green",
        title: "Rule created",
        message: "The alert rule has been created.",
      });
      queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
      setCreateOpen(false);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to create rule",
        message: errorMessage(err, "Could not create the alert rule."),
      });
    },
  });

  const editMutation = useMutation({
    mutationFn: async (vars: { id: string; values: RuleFormValues }) => {
      const rule = await updateAlertRule(vars.id, toInput(vars.values));
      await saveRuleRunbook(vars.id, vars.values.runbookId, vars.values.cooldownMinutes);
      return rule;
    },
    onSuccess: () => {
      notify({
        color: "green",
        title: "Rule updated",
        message: "The alert rule has been updated.",
      });
      queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
      setEditTarget(null);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to update rule",
        message: errorMessage(err, "Could not update the alert rule."),
      });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: (vars: { id: string; enabled: boolean }) =>
      updateAlertRule(vars.id, { enabled: vars.enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alert-rules"] }),
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to update rule",
        message: errorMessage(err, "Could not update the alert rule."),
      });
      queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteAlertRule(id),
    onSuccess: () => {
      notify({
        color: "green",
        title: "Rule deleted",
        message: "The alert rule has been deleted.",
      });
      queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
      setDeleteTarget(null);
    },
    onError: (err) => {
      notify({
        color: "red",
        title: "Failed to delete rule",
        message: errorMessage(err, "Could not delete the alert rule."),
      });
    },
  });

  const rules = rulesQuery.data ?? [];

  // --- Table columns + row actions ------------------------------------------
  // Select-filter options are derived from the data (data-mode needs explicit
  // options, unlike the old DataTable which auto-faceted).
  const ruleTypeOptions = useMemo(
    () =>
      Array.from(new Set(rules.map((rule) => rule.type).filter(Boolean)))
        .sort()
        .map((t) => ({
          value: t,
          label: specFor(t as AlertRuleType)?.label ?? t,
        })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rules, ruleTypes],
  );

  const columns: Column<AlertRule>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      accessor: (rule) => rule.name,
      cell: (rule) => <Text weight="medium">{rule.name}</Text>,
    },
    {
      key: "type",
      header: "Type",
      sortable: true,
      accessor: (rule) => specFor(rule.type)?.label ?? rule.type,
      filter: { kind: "select", options: ruleTypeOptions },
      filterFn: (rule, value) =>
        !Array.isArray(value) ||
        value.length === 0 ||
        value.includes(rule.type),
      cell: (rule) => (
        <StatusBadge status="info" value={specFor(rule.type)?.label ?? rule.type} />
      ),
    },
    {
      key: "condition",
      header: "Condition",
      accessor: (rule) => conditionText(rule, specFor(rule.type)),
      cell: (rule) => {
        const spec = specFor(rule.type);
        return (
          <>
            <Text size="sm">{conditionText(rule, spec)}</Text>
            {rule.forDuration > 0 && (
              <Text size="xs" color="muted">
                for {formatDuration(rule.forDuration)}
              </Text>
            )}
          </>
        );
      },
    },
    {
      key: "scope",
      header: "Scope",
      sortable: true,
      accessor: (rule) => nameFor(rule.hostId),
      cell: (rule) => (
        <>
          <Text size="sm">{nameFor(rule.hostId)}</Text>
          {scopeText(rule) && (
            <Text size="xs" color="muted">
              {scopeText(rule)}
            </Text>
          )}
        </>
      ),
    },
    {
      key: "severity",
      header: "Severity",
      sortable: true,
      accessor: (rule) =>
        rule.severity || specFor(rule.type)?.defaultSeverity || "warning",
      cell: (rule) => (
        <StatusBadge
          status={rule.severity || specFor(rule.type)?.defaultSeverity || "warning"}
        />
      ),
    },
    {
      key: "channel",
      header: "Channel",
      accessor: (rule) => channelName(channels, rule.channelId),
      cell: (rule) => (
        <Text size="sm" color={rule.channelId ? undefined : "muted"}>
          {channelName(channels, rule.channelId)}
        </Text>
      ),
    },
    {
      key: "enabled",
      header: "Enabled",
      cell: (rule) => (
        <InputToggle
          variant="inline"
          checked={rule.enabled}
          onChange={(e) =>
            toggleMutation.mutate({ id: rule.id, enabled: e.target.checked })
          }
          aria-label="Toggle rule enabled"
        />
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (rule) => (
        <Box
          direction="row"
          onClick={(e) => e.stopPropagation()}
          style={{ display: "inline-flex" }}
        >
          {renderRowActions(rule)}
        </Box>
      ),
    },
  ];

  const renderRowActions = (rule: AlertRule) => (
    <Box direction="row" gap={2} wrap="nowrap">
      <Tooltip content="Edit">
        <Button
          variant="clear"
          color="neutral"
          aria-label="Edit rule"
          iconLeft="edit-outline-rounded"
          onClick={() => setEditTarget(rule)}
        />
      </Tooltip>
      <Tooltip content="Delete">
        <Button
          variant="clear"
          color="danger"
          aria-label="Delete rule"
          iconLeft="delete-outline-rounded"
          onClick={() => setDeleteTarget(rule)}
        />
      </Tooltip>
    </Box>
  );

  return (
    <Box direction="column" gap={6}>
      <SectionCard
        title="Alert rules"
        icon={<Icon name="warning-outline-rounded" size="md" />}
        description="Rules that evaluate host and container state and raise alerts. Each rule can compare a metric, require a sustained duration, target a host/container, and route to a specific channel."
        actions={
          <Button
            color="info"
            iconLeft="add-rounded"
            onClick={() => setCreateOpen(true)}
            size="sm"
            disabled={typesQuery.isLoading}
          >
            New rule
          </Button>
        }
        noBodyPadding
      >
        {rulesQuery.isError ? (
          <Text color="danger" as="p">
            Failed to load alert rules.
          </Text>
        ) : (
          <Table<AlertRule>
            data={rules}
            columns={columns}
            rowKey={(rule) => rule.id}
            globalSearch
            pagination
            defaultPageSize={50}
            pageSizeOptions={[10, 25, 50, 100]}
            loading={rulesQuery.isLoading}
            stickyHeader
            striped
            hoverable
            emptyState={
              <EmptyState
                icon={<Icon name="warning-outline-rounded" size="xl" />}
                title="No alert rules"
                description="Create a rule to start monitoring your containers and hosts."
                action={
                  <Button color="info" iconLeft="add-rounded" onClick={() => setCreateOpen(true)}>
                    New rule
                  </Button>
                }
              />
            }
          />
        )}
      </SectionCard>

      <RuleFormModal
        opened={createOpen}
        title="New alert rule"
        ruleTypes={ruleTypes}
        hosts={hosts}
        channels={channels}
        initial={null}
        submitting={createMutation.isPending}
        onClose={() => setCreateOpen(false)}
        onSubmit={(values) => createMutation.mutate(values)}
      />

      <RuleFormModal
        opened={editTarget !== null}
        title="Edit alert rule"
        ruleTypes={ruleTypes}
        hosts={hosts}
        channels={channels}
        initial={editTarget ? ruleToForm(editTarget) : null}
        ruleId={editTarget?.id}
        submitting={editMutation.isPending}
        onClose={() => setEditTarget(null)}
        onSubmit={(values) =>
          editTarget && editMutation.mutate({ id: editTarget.id, values })
        }
      />

      <ConfirmModal
        opened={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="Delete alert rule"
        confirmLabel="Delete"
        loading={deleteMutation.isPending}
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        message={
          <>
            Are you sure you want to delete the rule{" "}
            <Text as="span" weight="semibold">
              {deleteTarget?.name}
            </Text>
            ? This action cannot be undone.
          </>
        }
      />
    </Box>
  );
}

// ---------------------------------------------------------------------------
// Rule form modal (create + edit)
// ---------------------------------------------------------------------------

function RuleFormModal({
  opened,
  title,
  ruleTypes,
  hosts,
  channels,
  initial,
  submitting,
  onClose,
  onSubmit,
  ruleId,
}: {
  ruleId?: string;
  opened: boolean;
  title: string;
  ruleTypes: AlertRuleTypeSpec[];
  hosts: Host[];
  channels: NotificationChannel[];
  initial: RuleFormValues | null;
  submitting: boolean;
  onClose: () => void;
  onSubmit: (values: RuleFormValues) => void;
}) {
  const firstSpec = ruleTypes[0];
  const [values, setValues] = useState<RuleFormValues>(
    initial ?? blankRule(firstSpec),
  );
  const [nameError, setNameError] = useState<string | null>(null);

  const setFieldValue = <K extends keyof RuleFormValues>(
    key: K,
    value: RuleFormValues[K],
  ) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  // Re-seed the form whenever the modal opens (so create starts blank and edit
  // shows the rule's current values). `opened` in the deps makes it fire on open.
  useEffect(() => {
    if (!opened) return;
    setValues(initial ?? blankRule(firstSpec));
    setNameError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  const spec = ruleTypes.find((s) => s.type === values.type);

  // The runbook it starts lives apart from the rule: load it into the form.
  const runbooksQuery = useQuery({ queryKey: ["runbooks", ""], queryFn: () => listRunbooks(), enabled: opened });
  const linkQuery = useQuery({ queryKey: ["rule-runbook", ruleId], queryFn: () => getRuleRunbook(ruleId!), enabled: opened && !!ruleId });
  useEffect(() => {
    if (opened && linkQuery.data) setValues((v) => ({ ...v, runbookId: linkQuery.data!.runbookId, cooldownMinutes: linkQuery.data!.cooldownMinutes }));
  }, [opened, linkQuery.data]);
  const runbookOptions = [{ value: "", label: "None" }, ...(runbooksQuery.data ?? []).map((r) => ({ value: r.id, label: r.name }))];

  // Switching type pulls in that type's sensible defaults for the numeric
  // fields, so the threshold/operator never carry over nonsensically.
  const onTypeChange = (value: string | null) => {
    if (!value) return;
    const next = ruleTypes.find((s) => s.type === value);
    const d = defaultsFor(next);
    setValues((prev) => ({
      ...prev,
      type: value as AlertRuleType,
      operator: d.operator ?? ">",
      threshold: d.threshold ?? 0,
    }));
  };

  const hostOptions = useMemo(() => {
    const opts = [{ value: "", label: "All hosts" }];
    for (const h of hosts) opts.push({ value: h.id, label: h.name || h.id });
    return opts;
  }, [hosts]);

  const channelOptions = useMemo(() => {
    const opts = [{ value: "", label: "All enabled channels" }];
    for (const c of channels) opts.push({ value: c.id, label: c.name });
    return opts;
  }, [channels]);

  const typeOptions = ruleTypes.map((s) => ({ value: s.type, label: s.label }));

  const validate = (): string | null => {
    if (values.name.trim().length === 0) return "Name is required";
    return null;
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const err = validate();
    setNameError(err);
    if (err) return;
    onSubmit(values);
  };

  return (
    <Popup
      open={opened}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      size="lg"
    >
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{title}</Popup.Title>
        </Popup.Header>
        <form onSubmit={handleSubmit}>
          <Popup.Body>
            <Box direction="column" gap={4}>
              <TextInput
                label="Name"
                placeholder="High CPU on web"
                data-autofocus
                value={values.name}
                errorText={nameError ?? undefined}
                onChange={(e) => setFieldValue("name", e.target.value)}
              />

              <Select
                label="Condition type"
                options={typeOptions}
                value={values.type}
                onChange={onTypeChange}
                description={spec?.description}
              />

              {spec?.numeric && (
                <Box direction="row" wrap align="end" gap={4}>
                  <Select
                    label="Operator"
                    options={OPERATOR_OPTIONS}
                    fullWidth
                    value={values.operator}
                    onChange={(v) => setFieldValue("operator", v ?? ">")}
                  />
                  <NumberInput
                    label={`Threshold${spec.unit ? ` (${spec.unit})` : ""}`}
                    min={0}
                    fullWidth
                    value={values.threshold}
                    onChange={(v) => setFieldValue("threshold", v === "" ? 0 : v)}
                  />
                </Box>
              )}

              <Box direction="row" wrap gap={4}>
                <Select
                  label="Severity"
                  options={SEVERITY_OPTIONS}
                  fullWidth
                  value={values.severity}
                  onChange={(v) => setFieldValue("severity", v ?? "")}
                />
                <Select
                  label="Sustained for"
                  description="Only fire if the condition holds this long."
                  options={DURATION_OPTIONS}
                  fullWidth
                  value={String(values.forDuration)}
                  onChange={(v) => setFieldValue("forDuration", Number(v ?? 0))}
                />
              </Box>

              <Select
                label="Host"
                options={hostOptions}
                value={values.hostId}
                onChange={(v) => setFieldValue("hostId", v ?? "")}
              />

              {spec?.containerScoped && (
                <>
                  <TextInput
                    label="Container name filter"
                    description="Optional. Only containers whose name contains this text. Leave blank for all."
                    placeholder="e.g. web"
                    value={values.match}
                    onChange={(e) => setFieldValue("match", e.target.value)}
                  />
                  <Box direction="row" wrap gap={4}>
                    <TextInput
                      label="Label key"
                      description="Optional label selector."
                      placeholder="com.docker.compose.project"
                      fullWidth
                      value={values.labelKey}
                      onChange={(e) => setFieldValue("labelKey", e.target.value)}
                    />
                    <TextInput
                      label="Label value"
                      description="Blank matches any value."
                      placeholder="myproject"
                      fullWidth
                      value={values.labelValue}
                      onChange={(e) => setFieldValue("labelValue", e.target.value)}
                    />
                  </Box>
                </>
              )}

              <Select
                label="Notification channel"
                description="Where this rule's alerts are delivered."
                options={channelOptions}
                value={values.channelId}
                onChange={(v) => setFieldValue("channelId", v ?? "")}
              />

              <Box direction="row" wrap gap={4}>
                <Select
                  label="Run a runbook"
                  description="When the rule fires, with its parameters' defaults. The step reads $KOWAN_ALERT_CONTAINER, $KOWAN_ALERT_HOST, $KOWAN_ALERT_MESSAGE."
                  options={runbookOptions}
                  value={values.runbookId}
                  onChange={(v) => setFieldValue("runbookId", v ?? "")}
                />
                {values.runbookId && (
                  <NumberInput
                    label="At most every (minutes)"
                    description="For the same container."
                    min={0}
                    max={1440}
                    value={values.cooldownMinutes}
                    onChange={(v) => setFieldValue("cooldownMinutes", typeof v === "number" ? v : 10)}
                  />
                )}
              </Box>

              <Box direction="row" wrap gap={4}>
                <InputToggle
                  label="Auto-resolve"
                  description="Mark the alert resolved when the condition clears."
                  checked={values.autoResolve}
                  onChange={(e) => setFieldValue("autoResolve", e.target.checked)}
                />
                <InputToggle
                  label="Enabled"
                  checked={values.enabled}
                  onChange={(e) => setFieldValue("enabled", e.target.checked)}
                />
              </Box>
            </Box>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" wrap justify="end" gap={2}>
              <Button variant="light" color="neutral" onClick={onClose}>
                Cancel
              </Button>
              <Button color="info" type="submit" loading={submitting}>
                {initial ? "Save" : "Create"}
              </Button>
            </Box>
          </Popup.Footer>
        </form>
      </Popup.Content>
    </Popup>
  );
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

function toInput(values: RuleFormValues): Partial<AlertRuleInput> {
  return {
    name: values.name.trim(),
    type: values.type,
    operator: values.operator,
    threshold: values.threshold,
    severity: values.severity,
    forDuration: values.forDuration,
    hostId: values.hostId,
    match: values.match.trim(),
    labelKey: values.labelKey.trim(),
    labelValue: values.labelValue.trim(),
    channelId: values.channelId,
    autoResolve: values.autoResolve,
    enabled: values.enabled,
  };
}

function conditionText(rule: AlertRule, spec: AlertRuleTypeSpec | undefined): string {
  if (spec && !spec.numeric) return spec.label;
  const unit = spec?.unit ?? "";
  return `${rule.operator} ${rule.threshold}${unit}`;
}

function scopeText(rule: AlertRule): string {
  const parts: string[] = [];
  if (rule.match) parts.push(`name~"${rule.match}"`);
  if (rule.labelKey) parts.push(`${rule.labelKey}${rule.labelValue ? `=${rule.labelValue}` : ""}`);
  return parts.join(", ");
}

function channelName(channels: NotificationChannel[], id: string): string {
  if (!id) return "All channels";
  return channels.find((c) => c.id === id)?.name ?? "(deleted channel)";
}

function formatDuration(seconds: number): string {
  if (seconds % 60 === 0) {
    const m = seconds / 60;
    return `${m} min`;
  }
  return `${seconds}s`;
}
