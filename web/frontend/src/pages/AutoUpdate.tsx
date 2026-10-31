import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import {
  Badge,
  Box,
  Button,
  Icon,
  InputToggle,
  NumberInput,
  Select,
  StatePill,
  Table,
  Text,
  Tooltip,
  type Column,
} from "@/ui/kit";
import {
  getAutoUpdateHistory,
  getAutoUpdateOverview,
  putAutoUpdateSettings,
  runAutoUpdateCheck,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import GroupPolicies from "../components/autoupdate/GroupPolicies";
import HistoryTable from "../components/autoupdate/HistoryTable";
import { MODE_OPTIONS, SCAN_OPTIONS, modeLabel, outcomeColor, outcomeText, scanLabel } from "../components/autoupdate/labels";
import { SLOW_REFETCH_MS } from "../lib/live";
import { notify } from "../ui/toast";
import { errorMessage, timeAgo } from "../utils";
import type { AutoUpdateOverviewItem, AutoUpdateSettings, PolicySource } from "../types";

const PAGE = 25;

function Src({ s }: { s?: PolicySource }) {
  if (s !== "label") return null;
  return <Badge size="sm" variant="outline" color="blue">label</Badge>;
}

function objectPath(o: AutoUpdateOverviewItem): string {
  if (o.kind === "deployment") return `/workloads/dep~${o.target}`;
  return `/containers/${encodeURIComponent(o.target)}?host=${encodeURIComponent(o.hostId)}`;
}

export default function AutoUpdate() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const isOperator = isAdmin || user?.role === "operator";
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [outcome, setOutcome] = useState("");

  const overview = useQuery({ queryKey: ["autoupdate-overview"], queryFn: getAutoUpdateOverview, refetchInterval: SLOW_REFETCH_MS * 2 });
  const history = useQuery({
    queryKey: ["autoupdate-history", offset, outcome],
    queryFn: () => getAutoUpdateHistory({ limit: PAGE, offset, outcome: outcome || undefined }),
    refetchInterval: SLOW_REFETCH_MS * 2,
  });

  const [settings, setSettings] = useState<AutoUpdateSettings | null>(null);
  useEffect(() => {
    if (overview.data && !settings) setSettings(overview.data.settings);
  }, [overview.data, settings]);

  const saveSettings = useMutation({
    mutationFn: (s: AutoUpdateSettings) => putAutoUpdateSettings(s),
    onSuccess: (s) => {
      setSettings(s);
      queryClient.invalidateQueries({ queryKey: ["autoupdate-overview"] });
      notify({ color: "green", title: "Auto-update", message: "Settings saved" });
    },
    onError: (err) => notify({ color: "red", title: "Save failed", message: errorMessage(err, "Could not save settings") }),
  });
  const checkAll = useMutation({
    mutationFn: () => runAutoUpdateCheck({}),
    onSuccess: () => {
      notify({ color: "blue", title: "Auto-update", message: "Checking every host now" });
      window.setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ["autoupdate-overview"] });
        queryClient.invalidateQueries({ queryKey: ["autoupdate-history"] });
      }, 5000);
    },
    onError: (err) => notify({ color: "red", title: "Check failed", message: errorMessage(err, "Could not start the check") }),
  });

  const objects = overview.data?.objects ?? [];
  const hostName = (id: string) => objects.find((o) => o.hostId === id)?.hostName ?? id;

  const columns: Column<AutoUpdateOverviewItem>[] = [
    {
      key: "name",
      header: "Object",
      sortable: true,
      cell: (o) => {
        const to = objectPath(o);
        return (
          <Box direction="column" gap={0} style={{ minWidth: 0 }}>
            <Link to={to} className="cell-link">{o.name}</Link>
            <Text size="xs" color="muted">{o.kind}{o.image ? ` · ${o.image}` : ""}</Text>
          </Box>
        );
      },
    },
    { key: "hostName", header: "Host", sortable: true },
    {
      key: "mode",
      header: "Mode",
      accessor: (o) => o.effective.mode,
      filter: { kind: "select", options: MODE_OPTIONS.map((m) => ({ value: m.value, label: m.label })) },
      filterFn: (o, v) => !Array.isArray(v) || v.length === 0 || v.includes(o.effective.mode),
      cell: (o) => (
        <Box direction="row" gap={1} align="center">
          <StatePill status={o.effective.mode} label={modeLabel(o.effective.mode)} color={o.effective.mode === "off" ? "gray" : o.effective.mode === "apply" ? "green" : "blue"} size="sm" />
          <Src s={o.effective.sources.mode} />
        </Box>
      ),
    },
    {
      key: "window",
      header: "Window",
      cell: (o) =>
        !o.window.valid ? (
          <Text size="xs" color="danger">{o.window.error}</Text>
        ) : o.window.always ? (
          <Text size="xs" color="muted">any time</Text>
        ) : (
          <Tooltip content={o.window.describe}>
            <span tabIndex={0}>
              <Text size="xs">{o.window.open ? "open now" : o.window.nextOpen ? `next ${new Date(o.window.nextOpen).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "—"}</Text>
            </span>
          </Tooltip>
        ),
    },
    {
      key: "gates",
      header: "Gates",
      cell: (o) => (
        <Box direction="column" gap={0}>
          <Text size="xs">{scanLabel(o.effective.scanBlock)}</Text>
          {o.effective.minAge && <Text size="xs" color="muted">min age {o.effective.minAge}</Text>}
        </Box>
      ),
    },
    {
      key: "lastResult",
      header: "Last result",
      grow: true,
      cell: (o) =>
        o.lastResult ? (
          <Box direction="column" gap={0} style={{ minWidth: 0 }}>
            <Box direction="row" gap={2} align="center">
              <StatePill status={o.lastResult} label={outcomeText(o.lastResult)} color={outcomeColor(o.lastResult)} size="sm" />
              <Text size="xs" color="muted">{timeAgo(o.lastCheckAt)}</Text>
            </Box>
            <Text size="xs" color="muted" style={{ whiteSpace: "normal" }}>{o.lastMessage}</Text>
          </Box>
        ) : (
          <Text size="xs" color="muted">not checked yet{o.nextCheckAt ? ` · next ${timeAgo(o.nextCheckAt)}` : ""}</Text>
        ),
    },
  ];

  const total = history.data?.total ?? 0;
  const intervalHours = settings ? settings.intervalSeconds / 3600 : 6;

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Auto-update"
        description="Image update policies for containers and deployments, with scan gates, maintenance windows and automatic rollback."
        icon={<Icon name="upgrade-rounded" size="lg" />}
        actions={
          isOperator && (
            <Button variant="outline" iconLeft="refresh-rounded" loading={checkAll.isPending} onClick={() => checkAll.mutate()}>
              Check all now
            </Button>
          )
        }
      />

      {settings && (
        <SectionCard
          title="Settings"
          description={`Background checks every ${intervalHours}h, staggered across hosts.${overview.data?.lastTickAt ? ` Last loop ${timeAgo(overview.data.lastTickAt)}.` : ""} Configure single containers with labels (docs/labels.md) or from their detail page.`}
          actions={
            isAdmin && (
              <Button size="sm" loading={saveSettings.isPending} onClick={() => saveSettings.mutate(settings)}>
                Save settings
              </Button>
            )
          }
        >
          <div className="grid items-end gap-5 md:grid-cols-4">
            <InputToggle
              label="Background checks"
              checked={settings.enabled}
              disabled={!isAdmin}
              onChange={(e) => setSettings({ ...settings, enabled: e.currentTarget.checked })}
            />
            <NumberInput
              label="Check interval (hours)"
              value={intervalHours}
              min={0.25}
              step={0.25}
              precision={2}
              disabled={!isAdmin}
              onChange={(v) => setSettings({ ...settings, intervalSeconds: Math.round(Number(v || 6) * 3600) })}
            />
            <Select
              label="Default scan gate"
              options={SCAN_OPTIONS}
              value={settings.defaultScanBlock}
              disabled={!isAdmin}
              onChange={(v) => setSettings({ ...settings, defaultScanBlock: v as AutoUpdateSettings["defaultScanBlock"] })}
              size="sm"
            />
            <NumberInput
              label="Health verify deadline (s)"
              value={settings.verifyTimeoutSeconds}
              min={15}
              max={1800}
              disabled={!isAdmin}
              onChange={(v) => setSettings({ ...settings, verifyTimeoutSeconds: Number(v || 120) })}
            />
          </div>
        </SectionCard>
      )}

      <SectionCard title="Policies" description="Every container or deployment with a stored or label policy." noBodyPadding>
        <Table<AutoUpdateOverviewItem>
          data={objects}
          columns={columns}
          rowKey={(o) => `${o.kind}/${o.hostId}/${o.target}`}
          globalSearch
          pagination
          defaultPageSize={25}
          loading={overview.isLoading}
          emptyText="No policies yet. Add a kowan.update label to a container, or set a policy on a container or deployment page."
        />
      </SectionCard>

      <GroupPolicies canEdit={isOperator} />

      <SectionCard
        title="History"
        description={`${total} event${total === 1 ? "" : "s"}`}
        noBodyPadding
        actions={
          <Box direction="row" gap={2} align="center">
            <Select
              aria-label="Outcome"
              size="sm"
              width={170}
              options={[
                { value: "", label: "All outcomes" },
                ...["updated", "rolled_back", "blocked", "failed", "notified", "pulled"].map((v) => ({ value: v, label: outcomeText(v) })),
              ]}
              value={outcome}
              onChange={(v) => {
                setOutcome(v);
                setOffset(0);
              }}
            />
            <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))} aria-label="Newer">
              Newer
            </Button>
            <Text size="xs" color="muted">{total ? `${offset + 1}–${Math.min(offset + PAGE, total)}` : "0"}</Text>
            <Button variant="outline" size="sm" disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)} aria-label="Older">
              Older
            </Button>
          </Box>
        }
      >
        <HistoryTable items={history.data?.items ?? []} loading={history.isLoading} showObject hostName={hostName} />
      </SectionCard>
    </Box>
  );
}
