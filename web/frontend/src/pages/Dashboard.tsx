import { useMemo } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { LineChart, Text } from "@/ui/kit";
import {
  containerAction, getDiskUsage, getMetrics, getSystem, listAlerts, listContainers, listDeployments, listHosts, resolveAlert,
} from "../api/client";
import { useContainerFilter } from "../lib/features";
import type { Alert, MetricSample } from "../types";
import { useAuth } from "../auth/AuthContext";
import { ALL_HOSTS, useHost } from "../HostContext";
import { LIVE_REFETCH_MS } from "../lib/live";
import { useContainerUpdates } from "../lib/updates";
import { containerName, errorMessage } from "../utils";
import { notify } from "../ui/toast";
import { OverviewHeader, SumItem } from "../components/overview/OverviewHeader";
import { HostFacts, OfflineBanner } from "../components/overview/HostFacts";
import { HostsTable, type HostRow } from "../components/overview/HostsTable";
import { NeedsYou, buildNeeds } from "../components/overview/NeedsYou";
import { RecentFeed } from "../components/overview/RecentFeed";
import { appIdFor, hostKind, type HostContainer } from "../components/overview/model";
import { Btn, FRAME, PlusIcon, SectHead } from "../components/overview/ui";

/*
 * Overview (route "/"), after mockup D. State first: what needs the operator,
 * worst first; then the hosts (All hosts) or the host's vitals (one host); then
 * what happened recently; the load chart sits below the fold.
 */
export default function Overview() {
  const { hostId, setHostId } = useHost();
  const { user } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const all = hostId === ALL_HOSTS;
  const canOperate = !!user && user.role !== "viewer";
  const isAdmin = user?.role === "admin";

  /* ── data ──────────────────────────────────────────────────────────── */
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, refetchInterval: LIVE_REFETCH_MS });
  const hosts = useMemo(() => hostsQuery.data ?? [], [hostsQuery.data]);
  const scopeHosts = all ? hosts : hosts.filter((h) => h.id === hostId);
  const scopeIds = scopeHosts.map((h) => h.id);
  const online = scopeHosts.filter((h) => h.online);
  const focus = all ? undefined : scopeHosts[0];

  const systems = useQueries({
    queries: online.map((h) => ({ queryKey: ["system", h.id], queryFn: () => getSystem(h.id), refetchInterval: 30_000 })),
  });
  const metrics = useQueries({
    queries: online.map((h) => ({ queryKey: ["metrics", h.id], queryFn: () => getMetrics(h.id), refetchInterval: 15_000 })),
  });
  const containerQs = useQueries({
    queries: online.map((h) => ({ queryKey: ["containers", h.id, "all"], queryFn: () => listContainers(h.id, true), refetchInterval: LIVE_REFETCH_MS })),
  });
  const deploymentsQuery = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, refetchInterval: 30_000 });
  const alertsQuery = useQuery({ queryKey: ["alerts", "recent"], queryFn: () => listAlerts(50), refetchInterval: 30_000 });
  const diskQuery = useQuery({ queryKey: ["disk-usage", hostId], queryFn: () => getDiskUsage(hostId), enabled: !!focus?.online });
  const updates = useContainerUpdates(hostId, { enabled: hosts.length > 0 });

  const visible = useContainerFilter();
  const containers: HostContainer[] = visible(containerQs.flatMap((q, i) => (q.data ?? []).map((c) => ({ ...c, _host: online[i].id }))));
  const inScope = (id: string) => scopeIds.includes(id);
  const deployments = (deploymentsQuery.data ?? []).filter((d) => d.targets.some(inScope));
  const scopedAlerts = (alertsQuery.data ?? []).filter((a) => inScope(a.hostId));
  const openAlerts = scopedAlerts.filter((a) => !a.resolved);
  const waiting = Array.from(updates.data?.entries() ?? [])
    .filter(([, u]) => u.updateAvailable)
    .map(([k, u]) => ({ ...u, hostId: k.slice(0, k.indexOf("/")) }))
    .filter((u) => inScope(u.hostId));

  /* ── actions ───────────────────────────────────────────────────────── */
  const restart = useMutation({
    mutationFn: (c: HostContainer) => containerAction(c._host, c.id, "restart"),
    onSuccess: (_r, c) => notify({ color: "green", title: "Restarted", message: containerName(c.names) }),
    onError: (e) => notify({ color: "red", title: "Restart failed", message: errorMessage(e, "Could not restart the container") }),
    onSettled: () => queryClient.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("containers") }),
  });
  const resolve = useMutation({
    mutationFn: (a: Alert) => resolveAlert(a.id),
    onSuccess: () => notify({ color: "green", title: "Alert resolved" }),
    onError: (e) => notify({ color: "red", title: "Resolve failed", message: errorMessage(e, "Could not resolve the alert") }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["alerts"] }),
  });

  const needs = buildNeeds({
    // On one offline host the banner already says it; the card would repeat it.
    hosts: focus && !focus.online ? [] : scopeHosts,
    containers,
    deployments,
    allDeployments: deploymentsQuery.data ?? [],
    alerts: openAlerts,
    updates: waiting,
    canOperate,
    restarting: restart.isPending ? restart.variables?.id ?? null : null,
    resolving: resolve.isPending ? resolve.variables?.id ?? null : null,
    onRestart: (c) => restart.mutate(c),
    onResolve: (a) => resolve.mutate(a),
  });
  const needsLoading = hostsQuery.isLoading || containerQs.some((q) => q.isLoading) || alertsQuery.isLoading || deploymentsQuery.isLoading;

  /* ── hosts table rows ──────────────────────────────────────────────── */
  const rows: HostRow[] = hosts.map((h) => {
    const i = online.findIndex((o) => o.id === h.id);
    const samples = i >= 0 ? metrics[i]?.data ?? [] : [];
    return {
      host: h,
      system: i >= 0 ? systems[i]?.data : undefined,
      last: samples[samples.length - 1],
      containers: containers.filter((c) => c._host === h.id),
      loaded: i >= 0 ? !containerQs[i]?.isLoading : true,
    };
  });

  /* ── summary ───────────────────────────────────────────────────────── */
  const appCount = new Set(containers.map((c) => appIdFor(c, deploymentsQuery.data))).size;
  const runningCount = containers.filter((c) => c.state.toLowerCase() === "running").length;
  const scopeTone = all
    ? hosts.length > 1 ? ("mixed" as const) : hosts[0]?.online === false ? ("red" as const) : ("green" as const)
    : !focus?.online ? ("red" as const) : ("green" as const);
  const scopeName = all ? "All hosts" : focus?.name || hostId;

  /* ── load chart (below the fold) ───────────────────────────────────── */
  const chart = useMemo(() => {
    const series: { name: string; data: number[] }[] = [];
    let longest: MetricSample[] = [];
    online.forEach((h, i) => {
      const s = metrics[i]?.data ?? [];
      if (s.length > longest.length) longest = s;
      if (!s.length) return;
      series.push({ name: all ? h.name || h.id : "CPU", data: s.map((m) => Number(m.cpuPercent.toFixed(1))) });
      if (!all) series.push({ name: "Memory", data: s.map((m) => (m.memTotal ? Number(((m.memUsed / m.memTotal) * 100).toFixed(1)) : 0)) });
    });
    const labels = longest.map((m) => new Date(m.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }));
    return { labels, series };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [all, online.map((h) => h.id).join(), metrics.map((m) => m.dataUpdatedAt).join()]);

  const newApp = canOperate && (
    <Btn solid to="/deployments/new"><PlusIcon />New workload</Btn>
  );

  /* ── render ────────────────────────────────────────────────────────── */
  return (
    <>
      {all ? (
        <OverviewHeader
          scope="All hosts"
          scopeTone={scopeTone}
          title="Overview"
          summary={<>
            <SumItem n={hosts.length} label={hosts.length === 1 ? "host" : "hosts"} />
            <SumItem n={appCount} label={appCount === 1 ? "workload" : "workloads"} />
            <SumItem n={containers.length} label="containers" />
          </>}
          actions={<><Btn to="/activity">Activity</Btn>{newApp}</>}
        />
      ) : (
        <OverviewHeader
          scope={scopeName}
          scopeTone={scopeTone}
          title={scopeName}
          objectTitle
          summary={<>
            {focus && <SumItem label={hostKind(focus).kind} />}
            <SumItem n={appCount} label={appCount === 1 ? "workload" : "workloads"} />
            <SumItem n={focus?.online ? `${runningCount} / ${containers.length}` : "?"} label="running" />
          </>}
          actions={<><Btn to="/workloads">Workloads</Btn>{newApp}</>}
        />
      )}

      {focus && !focus.online && <OfflineBanner host={focus} />}
      {focus?.online && (
        <HostFacts
          host={focus}
          system={systems[0]?.data}
          last={metrics[0]?.data?.[metrics[0].data.length - 1]}
          disk={diskQuery.data}
          canOperate={canOperate}
        />
      )}
      {!all && !focus && !hostsQuery.isLoading && (
        <section className={`${FRAME} flex flex-wrap items-center gap-[12px] px-[16px] py-[14px] text-[14px] leading-[20px]`}>
          <span>This host no longer exists.</span>
          <Btn sm onClick={() => setHostId(ALL_HOSTS)}>Show all hosts</Btn>
        </section>
      )}

      <NeedsYou needs={needs} scopeName={all ? "any host" : scopeName} loading={needsLoading} label={all ? undefined : `${needs.length} open`} />

      {all && (
        hosts.length ? (
          <HostsTable rows={rows} scopeId={hostId} onPick={setHostId} />
        ) : !hostsQuery.isLoading && (
          <section className={FRAME}>
            <SectHead title="Hosts" />
            <div className="flex flex-wrap items-center gap-[12px] px-[16px] py-[14px] text-[14px] leading-[20px]">
              <span className="text-muted-foreground">No hosts yet. Add one to start managing Docker.</span>
              <Btn sm solid onClick={() => navigate("/hosts?add=1")}>Add host</Btn>
            </div>
          </section>
        )
      )}

      <RecentFeed hosts={hosts} scopeIds={scopeIds} all={all} alerts={scopedAlerts} deployments={deploymentsQuery.data ?? []} isAdmin={isAdmin} />

      {online.length > 0 && (
        <section className={FRAME}>
          <SectHead title={all ? "CPU by host" : "CPU and memory"} label="last hour · every 15 s" />
          <div className="px-[16px] py-[12px]">
            {chart.series.length ? (
              <LineChart labels={chart.labels} series={chart.series} yMax={100} unit="%" height={200} />
            ) : (
              <Text color="muted" size="sm">The monitor has not recorded samples yet.</Text>
            )}
          </div>
        </section>
      )}

    </>
  );
}
