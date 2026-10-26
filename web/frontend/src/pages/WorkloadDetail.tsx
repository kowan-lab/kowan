import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Navigate, useNavigate, useParams, useSearchParams } from "react-router";
import { CircleAlert, ExternalLink, HeartPulse, MoreHorizontal, Play, RotateCcw, ScrollText, Square, SquareTerminal, WifiOff } from "lucide-react";
import { Menu, Skeleton } from "@/ui/kit";
import {
  containerAction,
  getContainerSpec,
  getDeployment,
  listContainers,
  listDeployments,
  listHostEvents,
  listHosts,
  logsWebSocketURL,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useHost } from "../HostContext";
import { appHref, appIdForContainer, buildApps, KIND_LABEL, parseAppId, type App, type AppContainer } from "../lib/apps";
import { errorStatus, LIVE_REFETCH_MS, liveUnlessGone } from "../lib/live";
import { updateKey, useContainerUpdates } from "../lib/updates";
import { notify } from "../ui/toast";
import { errorMessage, formatDate, timeAgo } from "../utils";
import type { DockerEvent } from "../types";
import DeploymentActions from "../components/deployment/DeploymentActions";
import DeploymentActivity from "../components/deployment/DeploymentActivity";
import DeploymentHistory from "../components/deployment/DeploymentHistory";
import DeploymentPreviews, { PreviewBanner } from "../components/deployment/DeploymentPreviews";
import DeploymentPromotion from "../components/deployment/DeploymentPromotion";
import DeploymentUptime from "../components/deployment/DeploymentUptime";
import DeploymentConfig from "../components/deployment/DeploymentConfig";
import DeploymentLogs from "../components/deployment/DeploymentLogs";
import { DeploymentGitSource, DeploymentHealth, DeploymentWebhook } from "../components/deployment/DeploymentOverview";
import { useCanOperate } from "../components/deployment/shared";
import LogRetentionControl from "../components/logs/LogRetentionControl";
import LogViewer, { type LogSource } from "../components/logs/LogViewer";
import { usePolicyControls } from "../components/apps/policies";
import { PoliciesTab, PolicyStrip } from "../components/apps/PolicyPanels";
import WorkloadResources from "../components/apps/WorkloadResources";
import { Band, Btn, BtnLink, CalloutBar, Crumbs, Dot, Fact, LBL, Pill, Tag, btnClass } from "../components/apps/ui";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// App page (/apps/:appId?tab=…). One page for a deployment, a discovered
// compose stack or a lone container: the header answers what it is and
// whether it runs, the banner says what is wrong, the tabs hold the
// containers, logs, configuration, policies and activity. Deployment apps
// reuse the deployment components; stacks and containers reuse LogViewer and
// the container page.
// ---------------------------------------------------------------------------

const TABS = ["overview", "logs", "config", "history", "policies", "activity"] as const;
type Tab = (typeof TABS)[number];

const ICON = "size-[16px] shrink-0";

export default function AppDetail() {
  const { appId = "" } = useParams<{ appId: string }>();
  const parts = parseAppId(appId);
  const { hostId: scope } = useHost();
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = (TABS as readonly string[]).includes(raw ?? "") ? (raw as Tab) : "overview";

  const depQ = useQuery({
    queryKey: ["deployment", parts?.kind === "deployment" ? parts.deploymentId : ""],
    queryFn: () => getDeployment((parts as { deploymentId: string }).deploymentId),
    enabled: parts?.kind === "deployment",
    refetchInterval: liveUnlessGone,
  });
  const host = parts?.kind === "deployment" ? depQ.data?.targets?.[0] ?? "" : parts?.hostId ?? "";
  const hostsQ = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const ctrsQ = useQuery({
    // Same key as useHostScopedList's single-host list: one cache with the Containers page.
    queryKey: ["containers:all", "host", host],
    queryFn: () => listContainers(host, true),
    enabled: !!host,
    refetchInterval: LIVE_REFETCH_MS,
  });
  const depsQ = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, refetchInterval: 5000 });
  const updates = useContainerUpdates(host || scope, { enabled: !!host });

  const hostInfo = hostsQ.data?.find((h) => h.id === host);
  const app = useMemo<App | undefined>(() => {
    if (!parts || !host || !ctrsQ.data || !depsQ.data) return undefined;
    const deployments = depQ.data ? [depQ.data, ...depsQ.data.filter((d) => d.id !== depQ.data!.id)] : depsQ.data;
    const apps = buildApps({
      containers: ctrsQ.data,
      fallbackHost: host,
      deployments,
      scopeHosts: hostInfo ? [hostInfo] : null,
      updates: updates.data ? (h, id) => updates.data!.get(updateKey(h, id)) : undefined,
    });
    return apps.find((a) => a.id === appId);
  }, [parts, host, ctrsQ.data, depsQ.data, depQ.data, hostInfo, updates.data, appId]);

  // A stack id for a project a deployment owns, or a container id for a
  // container that belongs to a stack: send the link to the right app.
  const redirect = useMemo(() => {
    if (app || !parts || parts.kind === "deployment" || !ctrsQ.data || !depsQ.data) return null;
    const match = ctrsQ.data.find((c) =>
      parts.kind === "stack"
        ? c.labels?.["com.docker.compose.project"] === parts.project
        : (c.names?.[0] ?? "").replace(/^\//, "") === parts.name,
    );
    if (!match) return null;
    const id = appIdForContainer(match, host, depsQ.data);
    return id !== appId ? id : null;
  }, [app, parts, ctrsQ.data, depsQ.data, host, appId]);

  const setTab = (t: Tab, extra?: Record<string, string | null>) =>
    setParams(
      (p) => {
        const n = new URLSearchParams(p);
        if (t === "overview") n.delete("tab");
        else n.set("tab", t);
        for (const [k, v] of Object.entries(extra ?? {})) {
          if (v == null) n.delete(k);
          else n.set(k, v);
        }
        return n;
      },
      { replace: true },
    );

  if (redirect) return <Navigate to={appHref(redirect, tab)} replace />;

  const loading =
    !!parts &&
    ((parts.kind === "deployment" && depQ.isLoading) || (!!host && (ctrsQ.isLoading || depsQ.isLoading)));
  if (loading) return <DetailSkeleton />;

  const offline = !!hostInfo && !hostInfo.online;
  const missing =
    !parts ||
    (parts.kind === "deployment" && errorStatus(depQ.error) === 404) ||
    (!app && !offline && !ctrsQ.isError && !depQ.isError);
  if (!app) {
    // A deployment whose host is offline still has an app page built from its config.
    if (parts?.kind === "deployment" && depQ.data && (offline || ctrsQ.isError)) {
      const stale = buildApps({ containers: [], fallbackHost: host, deployments: [depQ.data], scopeHosts: hostInfo ? [hostInfo] : null })[0];
      if (stale) return <AppView app={{ ...stale, stale: true, attention: { tone: "n", text: "Host offline · last known config" } }} tab={tab} setTab={setTab} />;
    }
    return <NotFound scope={scope} missing={missing} error={depQ.error ?? ctrsQ.error ?? depsQ.error} name={parts ? appId : "Unknown workload"} />;
  }

  return <AppView app={app} tab={tab} setTab={setTab} />;
}

function DetailSkeleton() {
  return (
    <div className="flex flex-col gap-[12px]" aria-busy="true" aria-label="Loading workload">
      <div className="flex flex-col gap-[12px] bg-card px-[16px] py-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
        <Skeleton height={20} width={180} />
        <Skeleton height={36} width={320} />
        <Skeleton height={40} width="70%" />
      </div>
      <div className="bg-card p-[16px] shadow-[inset_0_0_0_1px_var(--border)]">
        <Skeleton height={160} />
      </div>
    </div>
  );
}

function NotFound({ scope, missing, error, name }: { scope: string; missing: boolean; error: unknown; name: string }) {
  return (
    <>
      <section className="bg-card px-[16px] pt-[14px] pb-[16px] shadow-[inset_0_0_0_1px_var(--border)]">
        <Crumbs hostScope={scope} parts={[{ label: "Workloads", to: "/workloads" }, { label: missing ? "Not found" : "Error" }]} />
        <h1 className="m-0 mt-[8px] text-[28px] leading-[36px] font-bold tracking-[-0.015em]">{missing ? "Workload not found" : "Couldn't load the workload"}</h1>
      </section>
      <section className="bg-card shadow-[inset_0_0_0_1px_var(--border)]">
        <div className="flex max-w-[680px] flex-col items-start gap-[10px] px-[16px] py-[32px]">
          <p className="m-0 text-muted-foreground">
            {missing
              ? `Nothing answers to ${name}. The stack or container may have been removed, or the link is wrong.`
              : errorMessage(error, "The server did not answer.")}
          </p>
          <BtnLink to="/workloads">All workloads</BtnLink>
        </div>
      </section>
    </>
  );
}

// ---------------------------------------------------------------------------

function AppView({ app, tab, setTab }: { app: App; tab: Tab; setTab: (t: Tab, extra?: Record<string, string | null>) => void }) {
  const { hostId: scope } = useHost();
  const { user } = useAuth();
  const canOperate = useCanOperate();
  const pc = usePolicyControls(app);
  const [params] = useSearchParams();
  const [configDirty, setConfigDirty] = useState(false);
  const [configVisited, setConfigVisited] = useState(tab === "config");
  useEffect(() => {
    if (tab === "config") setConfigVisited(true);
  }, [tab]);

  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts }).data ?? [];
  const host = app.hostIds[0] ?? "";
  const hostName = hosts.find((h) => h.id === host)?.name || host;
  const hostOnline = hosts.find((h) => h.id === host)?.online ?? !app.stale;
  const d = app.deployment;
  const pub = app.ports.filter((p) => p.public);
  const configLabel = d ? "Configuration" : app.kind === "stack" ? "Compose" : "Config";
  const tabs: { id: Tab; label: string; extra?: ReactNode }[] = [
    { id: "overview", label: "Overview", extra: <span className="font-mono text-[12px] leading-none font-medium text-muted-foreground">{app.total}</span> },
    { id: "logs", label: "Logs" },
    { id: "config", label: configLabel, extra: configDirty ? <><span aria-hidden className="size-[4px] rounded-full bg-[var(--tone-amber-solid)]" /><span className="sr-only">(unsaved changes)</span></> : undefined },
    ...(d ? [{ id: "history" as const, label: "History" }] : []),
    { id: "policies", label: "Policies", extra: app.updates ? <span className="font-mono text-[12px] leading-none font-medium text-[var(--tone-amber-text)]">{app.updates} update{app.updates === 1 ? "" : "s"}</span> : undefined },
    { id: "activity", label: "Activity" },
  ];
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const onTabKey = (e: KeyboardEvent, i: number) => {
    let n: number | null = null;
    if (e.key === "ArrowRight") n = (i + 1) % tabs.length;
    if (e.key === "ArrowLeft") n = (i - 1 + tabs.length) % tabs.length;
    if (e.key === "Home") n = 0;
    if (e.key === "End") n = tabs.length - 1;
    if (n === null) return;
    e.preventDefault();
    setTab(tabs[n].id);
    tabRefs.current[n]?.focus();
  };

  return (
    <>
      <section className="bg-card px-[16px] pt-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
        <Crumbs hostScope={scope} parts={[{ label: "Workloads", to: "/workloads" }, { label: app.name }]} />
        <div className="mt-[8px] flex flex-wrap items-center gap-[14px]">
          <h1 className="m-0 min-w-0 truncate text-[28px] leading-[36px] font-bold tracking-[-0.015em]">{app.name}</h1>
          <Pill tone={app.tone}>{app.label}</Pill>
          <div className="ml-auto flex flex-wrap gap-[8px]">
            {d ? (canOperate ? <DeploymentActions deployment={d} /> : null) : <ContainerActions app={app} canOperate={canOperate} />}
          </div>
        </div>
        <div className="mt-[14px] flex flex-wrap gap-y-[10px] pb-[16px]">
          <Fact label="Host">
            <Dot tone={hostOnline ? "g" : "r"} />
            <Link to={`/hosts/${encodeURIComponent(host)}`} className="text-foreground no-underline hover:underline">{hostName}</Link>
          </Fact>
          <Fact label="Kind">{d ? (d.type === "compose" ? "Compose deployment" : "Container deployment") : KIND_LABEL[app.kind]}</Fact>
          <Fact label="Source">{d ? (d.git?.repoUrl ? "Git repository" : "Kowan deployment") : app.kind === "stack" ? "Discovered from labels" : "Standalone"}</Fact>
          <Fact label="Exposed" mono>{pub.length ? pub.map((p) => `:${p.publicPort}`).join(" ") : "internal only"}</Fact>
          <Fact label="Created">
            <span title={app.createdAt ? formatDate(app.createdAt) : undefined}>{app.createdAt ? timeAgo(app.createdAt) : "—"}</span>
          </Fact>
        </div>
        <div role="tablist" aria-label={`${app.name} sections`} className="-mx-[16px] flex overflow-x-auto px-[8px] shadow-[inset_0_1px_0_var(--border)]">
          {tabs.map((t, i) => {
            const on = t.id === tab;
            return (
              <button
                key={t.id}
                ref={(el) => {
                  tabRefs.current[i] = el;
                }}
                type="button"
                role="tab"
                id={`app-tab-${t.id}`}
                aria-selected={on}
                aria-controls={`app-panel-${t.id}`}
                tabIndex={on ? 0 : -1}
                onClick={() => setTab(t.id)}
                onKeyDown={(e) => onTabKey(e, i)}
                className={cn(
                  "relative inline-flex h-[44px] items-center gap-[8px] px-[12px] text-[14px] leading-none whitespace-nowrap outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:outline-[var(--signal)]",
                  on ? "font-semibold text-foreground after:absolute after:right-[12px] after:bottom-0 after:left-[12px] after:h-[2px] after:bg-[var(--signal)]" : "font-medium text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
                {t.extra}
              </button>
            );
          })}
        </div>
      </section>

      <div role="tabpanel" id={`app-panel-${tab}`} aria-labelledby={`app-tab-${tab}`} className="flex min-w-0 flex-col gap-[12px]">
        {tab === "overview" && <Overview app={app} pc={pc} onLogs={() => setTab("logs")} />}
        {tab === "logs" && <LogRetentionControl workload={app.id} />}
        {tab === "logs" &&
          (d ? (
            <DeploymentLogs deployment={d} service={params.get("service")} onServiceChange={(s) => setTab("logs", { service: s })} />
          ) : (
            <AppLogs app={app} />
          ))}
        {tab === "config" && !d && (app.kind === "stack" ? <ComposeView app={app} /> : <ContainerConfig app={app} />)}
        {tab === "history" && d && <DeploymentHistory deployment={d} canOperate={canOperate} />}
        {tab === "policies" && <PoliciesTab app={app} pc={pc} />}
        {tab === "activity" && (d ? <DeploymentActivity deployment={d} isAdmin={user?.role === "admin"} /> : <AppEvents app={app} />)}
        {/* The configuration form holds unsaved edits: keep it mounted once opened. */}
        {d && configVisited && (
          <div hidden={tab !== "config"} className="min-w-0">
            <DeploymentConfig deployment={d} canOperate={canOperate} onDirtyChange={setConfigDirty} />
          </div>
        )}
      </div>
      {pc.dialogs}
    </>
  );
}

// ---------------------------------------------------------------------------
// Header actions for stacks and lone containers (deployments use theirs).
// ---------------------------------------------------------------------------

function useContainerAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ targets, action }: { targets: AppContainer[]; action: "start" | "stop" | "restart"; label: string }) => {
      await Promise.all(targets.map((c) => containerAction(c.hostId, c.container.id, action)));
    },
    onSuccess: (_r, v) => notify({ color: "green", title: v.action === "stop" ? "Stopped" : v.action === "start" ? "Started" : "Restarted", message: v.label }),
    onError: (err, v) => notify({ color: "red", title: `${v.action[0].toUpperCase()}${v.action.slice(1)} failed`, message: errorMessage(err, `Could not ${v.action} ${v.label}`) }),
    onSettled: () => qc.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("containers") }),
  });
}

function ContainerActions({ app, canOperate }: { app: App; canOperate: boolean }) {
  const act = useContainerAction();
  const running = app.containers.filter((c) => c.container.state === "running");
  const idle = app.containers.filter((c) => c.container.state !== "running");
  const busy = (a: string) => act.isPending && act.variables?.action === a;
  const off = !canOperate || app.stale || act.isPending;
  const single = app.kind === "container" ? app.containers[0] : undefined;
  return (
    <>
      {idle.length > 0 && (
        <Btn disabled={off} aria-busy={busy("start") || undefined} onClick={() => act.mutate({ targets: idle, action: "start", label: app.name })}>
          <Play className={ICON} strokeWidth={1.5} aria-hidden />
          {busy("start") ? "Starting…" : app.kind === "stack" && running.length ? `Start ${idle.length}` : "Start"}
        </Btn>
      )}
      {running.length > 0 && (
        <Btn disabled={off} aria-busy={busy("restart") || undefined} onClick={() => act.mutate({ targets: running, action: "restart", label: app.name })}>
          <RotateCcw className={ICON} strokeWidth={1.5} aria-hidden />
          {busy("restart") ? "Restarting…" : "Restart"}
        </Btn>
      )}
      {running.length > 0 && (
        <Btn disabled={off} aria-busy={busy("stop") || undefined} onClick={() => act.mutate({ targets: running, action: "stop", label: app.name })}>
          <Square className={ICON} strokeWidth={1.5} aria-hidden />
          {busy("stop") ? "Stopping…" : "Stop"}
        </Btn>
      )}
      <Menu placement="bottom-end">
        <Menu.Trigger>
          <Btn icon aria-label="More actions">
            <MoreHorizontal className={ICON} strokeWidth={1.5} aria-hidden />
          </Btn>
        </Menu.Trigger>
        <Menu.Content ariaLabel="More actions">
          {single ? (
            <MenuLink to={`/containers/${single.container.id}?host=${encodeURIComponent(single.hostId)}`}>Open container page</MenuLink>
          ) : (
            app.containers.map((c) => (
              <MenuLink key={c.container.id} to={`/containers/${c.container.id}?host=${encodeURIComponent(c.hostId)}`}>Open {c.name}</MenuLink>
            ))
          )}
          <Menu.Separator />
          <MenuLink to="/workloads">All workloads</MenuLink>
          <MenuLink to="/deployments/new">New deployment</MenuLink>
        </Menu.Content>
      </Menu>
    </>
  );
}

function MenuLink({ to, children }: { to: string; children: ReactNode }) {
  const navigate = useNavigate();
  return <Menu.Item onSelect={() => navigate(to)}>{children}</Menu.Item>;
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

const SRC_COLORS = ["var(--signal)", "var(--tone-green-solid)", "var(--tone-blue-solid)", "var(--tone-amber-solid)", "var(--tone-purple-solid)", "var(--tone-red-solid)"];

function ctrHref(c: AppContainer, tab?: string) {
  return `/containers/${c.container.id}?host=${encodeURIComponent(c.hostId)}${tab ? `&tab=${tab}` : ""}`;
}

function ProblemBanner({ app, onLogs }: { app: App; onLogs: () => void }) {
  const canOperate = useCanOperate();
  const act = useContainerAction();
  const a = app.attention;
  if (!a) return null;
  if (a.tone === "n") {
    return (
      <CalloutBar tone="info" icon={<WifiOff className={ICON} strokeWidth={1.5} aria-hidden />}
        actions={<BtnLink size="sm" to={`/hosts/${encodeURIComponent(app.hostIds[0] ?? "")}`}>Open host</BtnLink>}>
        <b className="font-semibold">The host is offline.</b> Containers can't be listed; what you see is the deployment's configuration. Actions resume when the agent reconnects.
      </CalloutBar>
    );
  }
  const c = app.containers.find((x) => x.name === a.container);
  const others = app.total - 1;
  return (
    <CalloutBar
      tone={a.tone}
      icon={a.tone === "r" ? <CircleAlert className={ICON} strokeWidth={1.5} aria-hidden /> : <HeartPulse className={ICON} strokeWidth={1.5} aria-hidden />}
      actions={
        <>
          {c ? (
            <BtnLink size="sm" to={ctrHref(c, "logs")}><ScrollText className={ICON} strokeWidth={1.5} aria-hidden />Open logs</BtnLink>
          ) : (
            <Btn size="sm" onClick={onLogs}><ScrollText className={ICON} strokeWidth={1.5} aria-hidden />Open logs</Btn>
          )}
          {c && canOperate && (
            <Btn
              size="sm"
              disabled={act.isPending}
              onClick={() => act.mutate({ targets: [c], action: c.container.state === "running" ? "restart" : "start", label: c.name })}
            >
              <RotateCcw className={ICON} strokeWidth={1.5} aria-hidden />
              {c.container.state === "running" ? "Restart container" : "Run again"}
            </Btn>
          )}
        </>
      }
    >
      <b className="font-semibold">{a.text}</b>
      {c && (
        <span className="text-muted-foreground">
          {" "}
          {c.container.status ? `· ${c.container.status}.` : ""}
          {others > 0 ? ` The other ${others} container${others === 1 ? " is" : "s are"} ${app.running - (c.container.state === "running" ? 1 : 0) === others ? "running" : "listed below"}.` : ""}
        </span>
      )}
    </CalloutBar>
  );
}

const TH = "h-[36px] bg-card px-[12px] text-left font-mono text-[12px] leading-none font-semibold tracking-[0.06em] whitespace-nowrap text-muted-foreground uppercase first:pl-[16px] last:pr-[16px]";
const TD = "h-[40px] px-[12px] align-middle text-[13px] leading-[18px] whitespace-nowrap first:pl-[16px] last:pr-[16px]";

function Overview({ app, pc, onLogs }: { app: App; pc: ReturnType<typeof usePolicyControls>; onLogs: () => void }) {
  const navigate = useNavigate();
  const d = app.deployment;
  // A failed or queued deploy says why (the server's last error) instead of the
  // generic container banner.
  const deployProblem = d && !app.stale && (d.status === "error" || d.status === "pending");
  const canOperate = useCanOperate();
  return (
    <>
      {d?.preview && <PreviewBanner deployment={d} />}
      {deployProblem ? <DeploymentHealth deployment={d} onShowLogs={onLogs} /> : <ProblemBanner app={app} onLogs={onLogs} />}
      <Band title="Containers" count={app.total}>
        {app.total === 0 ? (
          <div className="flex max-w-[680px] flex-col items-start gap-[10px] px-[16px] py-[24px]">
            <p className="m-0 text-muted-foreground">
              {app.stale ? "The host is offline, so its containers can't be listed." : app.deployment ? "Nothing of this deployment runs on its host. Deploy it to create its containers." : "No containers."}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className={TH}>Container</th>
                  <th className={TH}>State</th>
                  {/* Image and ports wait for the container page on a phone. */}
                  <th className={cn(TH, "max-sm:hidden")}>Image</th>
                  <th className={cn(TH, "max-sm:hidden")}>Ports</th>
                  <th className={cn(TH, "text-right")}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {app.containers.map((c, i) => {
                  const ports = (c.container.ports ?? [])
                    .map((p) => (p.publicPort ? `${p.publicPort} → ${p.privatePort}` : `${p.privatePort}`))
                    .filter((v, j, s) => s.indexOf(v) === j);
                  const running = c.container.state === "running";
                  return (
                    <tr key={c.container.id} className="cursor-pointer hover:[&>td]:bg-[var(--popover)]" onClick={() => navigate(ctrHref(c))}>
                      <td className={TD}>
                        <span aria-hidden className="mr-[8px] inline-block size-[8px] align-[0px]" style={{ background: SRC_COLORS[i % SRC_COLORS.length] }} />
                        <Link to={ctrHref(c)} onClick={(e) => e.stopPropagation()} className="font-semibold text-foreground no-underline hover:underline">{c.name}</Link>
                      </td>
                      <td className={TD}><Pill tone={c.tone}>{c.label}</Pill>{c.update?.updateAvailable && <Pill tone="a" className="ml-[6px]">Update</Pill>}</td>
                      <td className={cn(TD, "font-mono max-sm:hidden")}>{c.container.image}</td>
                      <td className={cn(TD, "font-mono max-sm:hidden")}>{ports.join(", ") || "—"}</td>
                      <td className={cn(TD, "text-right")}>
                        <span className="inline-flex justify-end gap-[2px]" onClick={(e) => e.stopPropagation()}>
                          <Link to={ctrHref(c, "logs")} className={btnClass({ icon: true, size: "sm", ghost: true })} aria-label={`Logs of ${c.name}`} title="Logs">
                            <ScrollText className={ICON} strokeWidth={1.5} aria-hidden />
                          </Link>
                          {running ? (
                            <Link to={ctrHref(c, "console")} className={btnClass({ icon: true, size: "sm", ghost: true })} aria-label={`Console of ${c.name}`} title="Console">
                              <SquareTerminal className={ICON} strokeWidth={1.5} aria-hidden />
                            </Link>
                          ) : (
                            <span className={cn(btnClass({ icon: true, size: "sm", ghost: true }), "cursor-not-allowed opacity-40")} aria-disabled title="Console needs a running container">
                              <SquareTerminal className={ICON} strokeWidth={1.5} aria-hidden />
                            </span>
                          )}
                          <Link to={ctrHref(c)} className={btnClass({ icon: true, size: "sm", ghost: true })} aria-label={`Open ${c.name}`} title="Open container">
                            <ExternalLink className={ICON} strokeWidth={1.5} aria-hidden />
                          </Link>
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Band>
      <WorkloadResources workload={app.id} />
      <Band title="Policies and wiring" count="turn a knob to change it here" right={<Link to={appHref(app, "policies")} className={btnClass({ size: "sm" })}>All policies</Link>}>
        <PolicyStrip app={app} pc={pc} />
      </Band>
      {d?.git?.repoUrl && <DeploymentGitSource deployment={d} />}
      {d?.git?.repoUrl && !d.preview && d.strategy === "standalone" && <DeploymentPreviews deployment={d} canOperate={canOperate} />}
      {d && <DeploymentPromotion deployment={d} canOperate={canOperate} />}
      {d && <DeploymentUptime deployment={d} />}
      {d && <DeploymentWebhook deployment={d} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// Logs, config and activity for stacks and lone containers
// ---------------------------------------------------------------------------

function AppLogs({ app }: { app: App }) {
  const sources = useMemo<LogSource[]>(
    () =>
      app.containers.map((c) => ({
        key: `${c.hostId}/${c.container.id}`,
        label: app.kind === "stack" ? c.service : c.name,
        url: (opts) => logsWebSocketURL(c.hostId, c.container.id, opts),
      })),
    [app.containers, app.kind],
  );
  return (
    <Band title="Logs" count={`${sources.length} source${sources.length === 1 ? "" : "s"}`}>
      <div className="p-[16px]">
        <LogViewer
          sources={sources}
          height="max(360px, calc(100dvh - 360px))"
          emptyTitle={app.stale ? "Logs paused" : "No containers"}
          emptyDescription={app.stale ? "The host is offline; logs stream again when the agent reconnects." : "This workload has no containers to stream from."}
        />
      </div>
    </Band>
  );
}

function ComposeView({ app }: { app: App }) {
  const labels = app.containers[0]?.container.labels ?? {};
  const files = labels["com.docker.compose.project.config_files"];
  const dir = labels["com.docker.compose.project.working_dir"];
  // Reconstructed from what Docker reports for each container: the service,
  // its image and its published ports. Not the original file.
  const yaml = [
    "services:",
    ...app.containers.flatMap((c) => {
      const ports = (c.container.ports ?? [])
        .filter((p) => p.publicPort)
        .map((p) => `"${p.publicPort}:${p.privatePort}${p.type && p.type !== "tcp" ? `/${p.type}` : ""}"`)
        .filter((v, i, s) => s.indexOf(v) === i);
      return [`  ${c.service}:`, `    image: ${c.container.image}`, ...(ports.length ? [`    ports: [${ports.join(", ")}]`] : [])];
    }),
  ].join("\n");
  return (
    <>
      <CalloutBar tone="info">
        <b className="font-semibold text-foreground">Discovered stack.</b> It was started outside Kowan, so its compose file is not stored here. Below is what Docker reports for each service, read-only. To manage it from Kowan, create a compose deployment with the same file.
      </CalloutBar>
      <div className="grid grid-cols-[minmax(0,1fr)_352px] items-start gap-[12px] max-[1100px]:grid-cols-1">
        <Band title={<h2 className="m-0 font-mono text-[13px] leading-[18px] font-medium">services (reconstructed)</h2>} right={<span className={LBL}>Read-only</span>}>
          <pre className="m-0 overflow-auto bg-muted px-[16px] py-[14px] font-mono text-[13px] leading-[20px] whitespace-pre">
            {yaml.split("\n").map((l, i) => {
              const m = /^(\s*)([\w.-]+):(.*)$/.exec(l);
              return (
                <div key={i}>
                  {m ? (
                    <>
                      {m[1]}
                      <span className="text-[var(--link)]">{m[2]}</span>:{m[3]}
                    </>
                  ) : (
                    l
                  )}
                </div>
              );
            })}
          </pre>
        </Band>
        <div className="flex min-w-0 flex-col gap-[12px]">
          <Band title="Compose file">
            <dl className="m-0 grid grid-cols-[104px_minmax(0,1fr)]">
              {[
                ["Project", app.project ?? app.name],
                ["Files", files || "not reported"],
                ["Directory", dir || "not reported"],
              ].map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className={cn(LBL, "flex min-h-[40px] items-center px-[16px] py-[10px] shadow-[inset_0_-1px_0_var(--border)]")}>{k}</dt>
                  <dd className="m-0 flex min-h-[40px] min-w-0 items-center py-[10px] pr-[16px] font-mono text-[13px] break-all shadow-[inset_0_-1px_0_var(--border)]">{v}</dd>
                </div>
              ))}
            </dl>
            <div className="flex flex-wrap gap-[8px] p-[16px]">
              <BtnLink to="/deployments/new" size="sm">New compose deployment</BtnLink>
            </div>
          </Band>
        </div>
      </div>
    </>
  );
}

function ContainerConfig({ app }: { app: App }) {
  const c = app.containers[0];
  const q = useQuery({
    queryKey: ["container-spec", c?.hostId, c?.container.id],
    queryFn: () => getContainerSpec(c!.hostId, c!.container.id),
    enabled: !!c && !app.stale,
    staleTime: 60_000,
  });
  const s = q.data;
  const rows: [string, ReactNode][] = s
    ? [
        ["Image", s.image],
        ["Ports", s.ports?.length ? s.ports.map((p) => `${p.hostPort || "—"} → ${p.containerPort}/${p.protocol || "tcp"}`).join(", ") : "none"],
        ["Volumes", s.volumes?.length ? s.volumes.map((v) => `${v.source}:${v.target}${v.readOnly ? ":ro" : ""}`).join(", ") : "none"],
        ["Networks", s.networks?.length ? s.networks.join(", ") : "default"],
        ["Restart", s.restartPolicy || "no"],
        ["Command", s.command?.length ? s.command.join(" ") : "image default"],
        ["Environment", `${s.env?.length ?? 0} variable${(s.env?.length ?? 0) === 1 ? "" : "s"}`],
      ]
    : [];
  return (
    <Band title="Container spec" right={c && <BtnLink size="sm" to={ctrHref(c)}>Edit on the container page</BtnLink>}>
      {q.isLoading ? (
        <div className="p-[16px]"><Skeleton height={160} /></div>
      ) : q.isError || !s ? (
        <p className="m-0 px-[16px] py-[24px] text-muted-foreground">{app.stale ? "The host is offline." : errorMessage(q.error, "Could not read the container's spec.")}</p>
      ) : (
        <dl className="m-0 grid grid-cols-[132px_minmax(0,1fr)]">
          {rows.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className={cn(LBL, "flex min-h-[40px] items-center px-[16px] py-[10px] shadow-[inset_0_-1px_0_var(--border)]")}>{k}</dt>
              <dd className="m-0 flex min-h-[40px] min-w-0 items-center py-[10px] pr-[16px] font-mono text-[13px] break-all shadow-[inset_0_-1px_0_var(--border)]">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </Band>
  );
}

const QUIET = /^exec_|^attach$|^resize$|^top$|^archive-path$|^extract-to-dir$|^export$|^commit$|^copy$/;

function eventTone(e: DockerEvent): "g" | "a" | "r" | "n" {
  const a = e.action;
  if (a === "start" || a === "unpause" || a.endsWith(": healthy")) return "g";
  if (a === "die") return e.attributes?.exitCode && e.attributes.exitCode !== "0" ? "r" : "n";
  if (a === "oom" || a === "kill" || a.endsWith(": unhealthy")) return "r";
  if (a === "restart" || a === "pause") return "a";
  return "n";
}

function AppEvents({ app }: { app: App }) {
  const host = app.hostIds[0] ?? "";
  const names = useMemo(() => new Set(app.containers.map((c) => c.name)), [app.containers]);
  const since = useMemo(() => Math.floor(Date.now() / 1000) - 24 * 3600, []);
  const q = useQuery({
    queryKey: ["host-events", host, "container", since],
    queryFn: () => listHostEvents(host, { since, type: "container" }),
    enabled: !!host && !app.stale,
    refetchInterval: 15_000,
  });
  const list = (q.data ?? [])
    .filter((e) => !QUIET.test(e.action))
    .filter((e) => names.has(e.actorName) || (!!app.project && e.attributes?.["com.docker.compose.project"] === app.project))
    .sort((a, b) => b.time.localeCompare(a.time))
    .slice(0, 200);
  return (
    <Band title="Docker events" count={q.data ? `${list.length}` : undefined}>
      {q.isLoading ? (
        <div className="p-[16px]"><Skeleton height={120} /></div>
      ) : q.isError ? (
        <p className="m-0 px-[16px] py-[24px] text-muted-foreground">{errorMessage(q.error, "Could not read the host's events.")}</p>
      ) : list.length === 0 ? (
        <div className="flex max-w-[680px] flex-col items-start gap-[10px] px-[16px] py-[24px]">
          <p className="m-0 text-muted-foreground">
            No starts, stops or health changes for this app in the host's recent events. The host keeps a bounded window, so busy hosts (health checks run as exec events) reach back only minutes.
          </p>
          <BtnLink to="/activity" size="sm">Open the timeline</BtnLink>
        </div>
      ) : (
        <div>
          {list.map((e) => {
            const t = eventTone(e);
            return (
              <div key={e.id} className="grid min-h-[48px] grid-cols-[72px_minmax(0,1fr)_auto] items-center gap-[12px] px-[16px] py-[10px] text-[14px] leading-[20px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
                <span className="font-mono text-[12px] leading-[16px] font-medium text-muted-foreground" title={formatDate(e.time)}>
                  {new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </span>
                <span className="flex min-w-0 flex-wrap items-center gap-[8px]">
                  <Pill tone={t}>{e.action.replace(/^health_status: /, "health ")}</Pill>
                  <b className="font-semibold">{e.actorName}</b>
                  {e.action === "die" && e.attributes?.exitCode && <span className="text-[13px] text-muted-foreground">exit code {e.attributes.exitCode}</span>}
                </span>
                <Tag>{timeAgo(e.time)}</Tag>
              </div>
            );
          })}
        </div>
      )}
    </Band>
  );
}

