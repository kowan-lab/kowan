import {
  Suspense,
  lazy,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Box,
  Button,
  Callout,
  Checkbox,
  Code,
  CopyButton,
  Icon,
  LineChart,
  Menu,
  Popup,
  Progress,
  Skeleton,
  Spinner,
  StatePill,
  Table,
  Text,
  Tooltip,
  toneVars,
  useDisclosure,
  type Color,
} from "@/ui/kit";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  containerAction,
  createContainer,
  getContainerSpec,
  inspectContainer,
  listContainers,
  listDeployments,
  listHosts,
  logsWebSocketURL,
  pauseContainer,
  removeContainer,
  statsStreamWebSocketURL,
  unpauseContainer,
} from "../api/client";
import {
  ContainerSpecForm,
  containerSpecFormFromSpec,
  containerSpecFormToSpec,
  emptyContainerSpecForm,
  validateContainerSpecForm,
  type ContainerSpecFormValue,
} from "../components/ContainerSpecForm";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import ScheduleSection from "../components/schedules/ScheduleSection";
import ImageScanChips from "../components/security/ImageScanChips";
import ImageVulnerabilities from "../components/security/ImageVulnerabilities";
import PolicyEditor from "../components/autoupdate/PolicyEditor";
import { ExposeDialog } from "../components/exposure/ExposeDialog";
import LogViewer from "../components/logs/LogViewer";
import { type LogSource } from "../components/logs/logStream";
import { useAuth } from "../auth/AuthContext";
import { notify } from "../ui/toast";
import { useHost, isAllHosts } from "../HostContext";
import { LIVE_REFETCH_MS, errorStatus, liveUnlessGone } from "../lib/live";
import { containerOrigin, isManaged, stackOf, type ContainerOrigin } from "../lib/containerOrigin";
import { appHref, appIdForContainer } from "../lib/apps";
import { Crumbs, LBL } from "../components/apps/ui";
import { cn } from "@/lib/utils";
import { describeUpdate, useContainerUpdates, useUpdateContainer } from "../lib/updates";
import { errorMessage, formatBytes, formatDate, formatPercent, shortId, timeAgo } from "../utils";
import type { Container, ContainerActionType, ContainerInspect, ContainerStats, ContainerUpdateStatus } from "../types";

// xterm and the file browser are heavy: they load only when their tab opens.
const ContainerConsole = lazy(() => import("../components/ContainerConsole"));
const ContainerFiles = lazy(() => import("../components/containerfiles/ContainerFiles"));

// ===========================================================================
// Container detail (/containers/:id?host=<hostId>&tab=<tab>).
//
// Reading order, top to bottom: the header says what it is and whether it is
// healthy (state pill with duration and exit code, health pill) and holds the
// actions; a problem band explains a failure when there is one; the tabs hold
// everything else. Overview answers "what is it, how is it wired"; Logs,
// Console and Files fill the rest of the viewport. The host is carried in the
// query string so aggregated ("All hosts") rows link to the right daemon, and
// the tab is in the URL so a link can open straight on the logs.
// ===========================================================================

const TABS = ["overview", "logs", "console", "stats", "files", "image", "inspect"] as const;
type TabKey = (typeof TABS)[number];
/** Tabs whose content fills the remaining viewport height. */
const FILL_TABS: TabKey[] = ["logs", "console", "files", "inspect"];

export default function ContainerDetail() {
  const { id = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const { hostId: ctxHost } = useHost();
  // The host comes from the link (?host=); fall back to the globally selected
  // host so a hand-typed URL still resolves against something sensible.
  const host = searchParams.get("host") || ctxHost;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const canOperate = user?.role === "admin" || user?.role === "operator";

  const rawTab = searchParams.get("tab") ?? "";
  const tab: TabKey = (TABS as readonly string[]).includes(rawTab) ? (rawTab as TabKey) : "overview";
  const setTab = (next: string) =>
    setSearchParams(
      (prev) => {
        const p = new URLSearchParams(prev);
        if (next === "overview") p.delete("tab");
        else p.set("tab", next);
        return p;
      },
      { replace: true },
    );

  // The console mounts on first open and then stays alive across tab switches,
  // so a shell session survives a look at the logs.
  const [consoleOpened, setConsoleOpened] = useState(false);
  useEffect(() => {
    if (tab === "console") setConsoleOpened(true);
  }, [tab]);

  const editHandlers = useDisclosure(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [forceRemove, setForceRemove] = useState(false);
  const [exposeOpen, setExposeOpen] = useState(false);
  const [updateOpen, setUpdateOpen] = useState(false);

  // A container is scoped to one daemon; "All hosts" can't resolve it. This only
  // happens on a hand-typed URL without ?host= while the switcher is aggregated.
  const hostResolved = !isAllHosts(host);

  // Live inspect drives the whole page and re-polls so header and tabs stay current.
  const inspectQuery = useQuery({
    queryKey: ["container-inspect", host, id],
    queryFn: () => inspectContainer(host, id),
    enabled: Boolean(id) && hostResolved,
    refetchInterval: liveUnlessGone,
  });
  const info = useMemo(() => parseInspect(inspectQuery.data), [inspectQuery.data]);
  const name = info.name || shortId(id);
  const origin = containerOrigin(info.labels);
  const managed = isManaged(origin);
  const state = info.status.toLowerCase();
  const running = state === "running";
  const paused = state === "paused";

  // Host name for the header and facts (cached list, shared with the shell).
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: hostResolved });
  const hostName = hostsQuery.data?.find((h) => h.id === host)?.name || host;

  // A minimal Container shaped from inspect, for components written against the list model.
  const container: Container = {
    id,
    names: [name],
    image: info.image,
    state,
    status: state,
    created: 0,
    ports: [],
    labels: info.labels,
    hostId: host,
  };
  // The app this container belongs to (deployment, stack, or itself).
  const deploymentsQuery = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, enabled: hostResolved });
  const appTo = appHref(appIdForContainer(container, host, deploymentsQuery.data));

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["container-inspect", host, id] });
    queryClient.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("containers") });
  };

  const actionMutation = useMutation({
    mutationFn: (action: ContainerActionType) => containerAction(host, id, action),
    onSuccess: (_d, action) => {
      notify({ color: "green", title: name, message: `${ACTION_DONE[action]}` });
      invalidate();
    },
    onError: (err, action) =>
      notify({ color: "red", title: `Could not ${action} ${name}`, message: errorMessage(err, "The Docker daemon rejected the request") }),
  });

  const pauseMutation = useMutation({
    mutationFn: () => (paused ? unpauseContainer(host, id) : pauseContainer(host, id)),
    onSuccess: () => {
      notify({ color: "green", title: name, message: paused ? "Unpaused" : "Paused" });
      invalidate();
    },
    onError: (err) =>
      notify({ color: "red", title: `Could not ${paused ? "unpause" : "pause"} ${name}`, message: errorMessage(err, "The Docker daemon rejected the request") }),
  });

  const removeMutation = useMutation({
    mutationFn: () => removeContainer(host, id, forceRemove),
    onSuccess: () => {
      notify({ color: "green", title: "Container removed", message: `${name} was removed` });
      queryClient.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("containers") });
      navigate("/workloads");
    },
    onError: (err) =>
      notify({ color: "red", title: `Could not remove ${name}`, message: errorMessage(err, "The Docker daemon rejected the request") }),
  });

  // Image update: same report as the list (shared cache), pull + recreate action.
  // The recreated container gets a new id, so follow it to its new page.
  const updates = useContainerUpdates(host, { enabled: hostResolved });
  const update = updates.get(host, id);
  const followTo = (newId: string) => {
    const p = new URLSearchParams(searchParams);
    p.set("host", host);
    navigate(`/containers/${newId}?${p.toString()}`, { replace: true });
  };
  const updateMutation = useUpdateContainer((res) => {
    setUpdateOpen(false);
    // A deployment's container is recreated by its redeploy: open the workload.
    if (res.deploymentId) navigate(`/workloads/dep~${res.deploymentId}`);
    else followTo(res.id);
  });

  const acting = actionMutation.isPending ? actionMutation.variables : undefined;
  const busy = actionMutation.isPending || pauseMutation.isPending || updateMutation.isPending;
  const problem = describeProblem(info);
  // For a crash, the last few log lines usually name the cause: show them in the band.
  const lastLines = useLastLogLines(host, id, Boolean(problem) && !problem?.output && !running, info.finishedAt);
  const fillTab = FILL_TABS.includes(tab);
  // Re-measure when the tab, the problem band or the loaded state changes what sits above.
  const fill = useFillHeight(fillTab && inspectQuery.isSuccess, `${tab}|${problem?.title ?? ""}`);

  const crumbs = [
    { label: "Workloads", to: "/workloads" },
    ...(origin.kind !== "plain" && origin.name ? [{ label: origin.name, to: appTo }] : []),
    { label: name },
  ];

  if (!hostResolved) {
    return (
      <>
        <PageHeader title="Container" breadcrumbs={[{ label: "Workloads", to: "/workloads" }, { label: shortId(id) }]} />
        <Band>
          <EmptyState
            icon={<Icon name="dns-outline-rounded" size="lg" />}
            title="Pick a host to open this container"
            description="A container lives on one Docker host, and “All hosts” is selected. Choose a host in the switcher, or open the container from the containers list."
            action={<Link to="/workloads"><Button variant="outline" color="neutral">Go to workloads</Button></Link>}
          />
        </Band>
      </>
    );
  }

  if (inspectQuery.isLoading) return <DetailSkeleton id={id} />;

  if (inspectQuery.isError) {
    const gone = errorStatus(inspectQuery.error) === 404;
    return (
      <>
        <PageHeader title={shortId(id)} breadcrumbs={[{ label: "Workloads", to: "/workloads" }, { label: shortId(id) }]} />
        <Band>
          <EmptyState
            icon={<Icon name={gone ? "deployed-code-outline" : "error-outline-rounded"} size="lg" />}
            title={gone ? `No container ${shortId(id)} on ${hostName}` : "Could not load this container"}
            description={
              gone
                ? "It was removed, or recreated with a new ID (an update or a redeploy does that). Find it by name in the containers list."
                : errorMessage(inspectQuery.error, "The host did not answer. Check that its agent is online, then retry.")
            }
            action={
              <Box direction="row" gap={2} justify="center">
                {!gone && (
                  <Button variant="outline" color="neutral" iconLeft="refresh-rounded" loading={inspectQuery.isFetching} onClick={() => void inspectQuery.refetch()}>
                    Retry
                  </Button>
                )}
                <Link to="/workloads"><Button variant="outline" color="neutral">Go to workloads</Button></Link>
              </Box>
            }
          />
        </Band>
      </>
    );
  }

  const lifecycle = canOperate && (
    <>
      {running && (
        <>
          <Button variant="outline" color="neutral" iconLeft="stop-rounded" loading={acting === "stop"} disabled={busy} onClick={() => actionMutation.mutate("stop")}>
            Stop
          </Button>
          <Button variant="outline" color="neutral" iconLeft="refresh-rounded" loading={acting === "restart"} disabled={busy} onClick={() => actionMutation.mutate("restart")}>
            Restart
          </Button>
        </>
      )}
      {paused && (
        <>
          <Button variant="outline" color="neutral" iconLeft="play-arrow-rounded" loading={pauseMutation.isPending} disabled={busy} onClick={() => pauseMutation.mutate()}>
            Unpause
          </Button>
          <Button variant="outline" color="neutral" iconLeft="stop-rounded" loading={acting === "stop"} disabled={busy} onClick={() => actionMutation.mutate("stop")}>
            Stop
          </Button>
        </>
      )}
      {state === "restarting" && (
        <Button variant="outline" color="neutral" iconLeft="stop-rounded" loading={acting === "stop"} disabled={busy} onClick={() => actionMutation.mutate("stop")}>
          Stop
        </Button>
      )}
      {!running && !paused && state !== "restarting" && state !== "removing" && (
        <Button color="green" iconLeft="play-arrow-rounded" loading={acting === "start"} disabled={busy} onClick={() => actionMutation.mutate("start")}>
          Start
        </Button>
      )}
      {update?.updateAvailable && (
        <Tooltip content={describeUpdate(update)}>
          <Button variant="outline" color="blue" iconLeft="upgrade-rounded" loading={updateMutation.isPending} disabled={busy} onClick={() => setUpdateOpen(true)}>
            Update
          </Button>
        </Tooltip>
      )}
      <Menu placement="bottom-end">
        <Menu.Trigger>
          <Button variant="outline" color="neutral" iconRight="arrow-drop-down-rounded" aria-label="More actions">
            More
          </Button>
        </Menu.Trigger>
        <Menu.Content ariaLabel="More container actions">
          {running && (
            <Menu.Item iconLeft="pause-rounded" onSelect={() => pauseMutation.mutate()}>
              Pause
            </Menu.Item>
          )}
          <Menu.Item iconLeft="upgrade-rounded" onSelect={() => void updates.checkNow()}>
            Check for image update
          </Menu.Item>
          <Menu.Item
            iconLeft="edit-outline-rounded"
            disabled={managed}
            trailing={managed ? (origin.kind === "stack" ? "compose" : "deployment") : undefined}
            onSelect={editHandlers.open}
          >
            Edit &amp; recreate…
          </Menu.Item>
          <Menu.Item iconLeft="http-rounded" onSelect={() => setExposeOpen(true)}>
            Expose on a domain…
          </Menu.Item>
          <Menu.Separator />
          {(running || paused || state === "restarting") && (
            <Menu.Item iconLeft="cancel-outline-rounded" color="danger" onSelect={() => setKillOpen(true)}>
              Kill…
            </Menu.Item>
          )}
          <Menu.Item
            iconLeft="delete-outline-rounded"
            color="danger"
            onSelect={() => {
              setForceRemove(running);
              setRemoveOpen(true);
            }}
          >
            Remove…
          </Menu.Item>
        </Menu.Content>
      </Menu>
    </>
  );

  const since = running || paused ? info.startedAt : info.finishedAt;
  const facts: [string, ReactNode][] = [
    [
      "Image",
      <span key="img" className="inline-flex flex-wrap items-center gap-[8px]">
        {info.imageId ? (
          <Link to={`/images/${encodeURIComponent(info.imageId)}?host=${encodeURIComponent(host)}`} className="break-all text-foreground no-underline hover:underline">{info.image}</Link>
        ) : (
          info.image || "unknown image"
        )}
        <UpdatePill update={update} />
        {info.imageId && <ImageScanChips hostId={host} imageId={info.imageId} />}
      </span>,
    ],
    ["Origin", <OriginText key="o" origin={origin} appTo={appTo} />],
    ["Host", <Link key="h" to={`/hosts/${encodeURIComponent(host)}`} className="text-foreground no-underline hover:underline">{hostName}</Link>],
    ["Health", info.health ? `${info.health}${info.healthFailingStreak > 0 ? ` · ${info.healthFailingStreak} failing` : ""}` : "no health check"],
    ["Restarts", `${info.restartCount} · policy ${info.restartPolicy}`],
    [
      running || paused ? "Up since" : "Last stopped",
      since ? (
        <span key="t" title={formatDate(since)}>
          {timeAgo(since)}
          {!running && !paused && info.exitCode !== undefined ? ` · exit ${info.exitCode}` : ""}
        </span>
      ) : running || paused ? "—" : "never started",
    ],
  ];
  const tabDefs: { id: TabKey; label: ReactNode }[] = [
    { id: "overview", label: "Overview" },
    { id: "logs", label: "Logs" },
    { id: "console", label: "Console" },
    { id: "stats", label: "Stats" },
    { id: "files", label: "Files" },
    {
      id: "image",
      label: (
        <>
          Image
          {update?.updateAvailable && <span aria-label="update available" className="size-[4px] rounded-full" style={{ background: "var(--tone-blue-solid)" }} />}
        </>
      ),
    },
    { id: "inspect", label: "Inspect" },
  ];
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    let n: number | null = null;
    if (e.key === "ArrowRight") n = (i + 1) % tabDefs.length;
    if (e.key === "ArrowLeft") n = (i - 1 + tabDefs.length) % tabDefs.length;
    if (e.key === "Home") n = 0;
    if (e.key === "End") n = tabDefs.length - 1;
    if (n === null) return;
    e.preventDefault();
    setTab(tabDefs[n].id);
    document.getElementById(`ctr-tab-${tabDefs[n].id}`)?.focus();
  };

  return (
    <>
      <section className="bg-card px-[16px] pt-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
        <Crumbs hostScope={host} parts={crumbs} />
        <div className="mt-[6px] flex flex-wrap items-center gap-[16px]">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-[12px]">
            <h1 title={name} className="m-0 max-w-full min-w-0 truncate text-[24px] leading-[32px] font-extrabold tracking-[-0.02em] uppercase">{name}</h1>
            <StatePill status={state || "unknown"} label={stateLabel(info)} detail={stateDuration(info)} />
            {info.health && <StatePill status={info.health} label={info.health === "starting" ? "Health starting" : info.health} />}
            {update?.updateAvailable && <StatePill status="update" label="Update available" color="blue" size="sm" />}
          </div>
          {lifecycle && <div className="flex flex-wrap gap-[8px]">{lifecycle}</div>}
        </div>
        <div className="flex flex-wrap gap-x-[24px] gap-y-[4px] pt-[10px] pb-[16px]">
          {facts.map(([k, v]) => (
            <span key={k} className={LBL}>
              {k} <b className="font-semibold text-foreground">{v}</b>
            </span>
          ))}
        </div>
      </section>

      {problem && (
        <ProblemBand
          problem={problem.output || !lastLines ? problem : { ...problem, output: lastLines, outputLabel: "Last log lines" }}
          onLogs={tab === "logs" ? undefined : () => setTab("logs")}
        />
      )}

      <div role="tablist" aria-label={`${name} sections`} className="flex items-stretch gap-[4px] overflow-x-auto overflow-y-hidden bg-card px-[12px] shadow-[inset_0_0_0_1px_var(--border)]">
        {tabDefs.map((t, i) => {
          const on = t.id === tab;
          return (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`ctr-tab-${t.id}`}
              aria-selected={on}
              aria-controls="ctr-panel"
              tabIndex={on ? 0 : -1}
              onClick={() => setTab(t.id)}
              onKeyDown={(e) => onTabKey(e, i)}
              className={cn(
                "inline-flex h-[52px] items-center gap-[8px] border-b-2 px-[12px] text-[14px] leading-none font-semibold whitespace-nowrap outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:outline-[var(--signal)]",
                on ? "border-[var(--foreground)] text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id="ctr-panel" aria-labelledby={`ctr-tab-${tab}`} className="flex min-w-0 flex-col gap-[20px]">
        {tab === "overview" && (<>
          <OverviewTab
            hostId={host}
            id={id}
            info={info}
            origin={origin}
            appTo={appTo}
            active={tab === "overview"}
            onStats={() => setTab("stats")}
          />
        </>)}

        {tab === "logs" && (<>
          <FillBand fill={fill} padded>
            <LogsTab hostId={host} id={id} name={name} active={tab === "logs"} />
          </FillBand>
        </>)}

        <div hidden={tab !== "console"} className="min-w-0">
          <FillBand fill={tab === "console" ? fill : undefined} padded>
            {running ? (
              <Suspense fallback={<Skeleton height="100%" />}>
                {consoleOpened && <ContainerConsole hostId={host} container={container} active={tab === "console"} running={running} />}
              </Suspense>
            ) : (
              <EmptyState
                icon={<Icon name="terminal-rounded" size="lg" />}
                title={`${name} is not running`}
                description="A shell runs inside the container, so it has to be running first."
                action={
                  canOperate && !paused ? (
                    <Button color="green" iconLeft="play-arrow-rounded" loading={acting === "start"} onClick={() => actionMutation.mutate("start")}>
                      Start container
                    </Button>
                  ) : undefined
                }
              />
            )}
          </FillBand>
        </div>

        {tab === "stats" && (<>
          <StatsTab hostId={host} id={id} active={tab === "stats"} running={running} memoryLimit={info.memory} />
        </>)}

        {tab === "files" && (<>
          <FillBand fill={fill}>
            <Suspense fallback={<Skeleton height="100%" />}>
              <ContainerFiles hostId={host} containerId={id} active={tab === "files"} />
            </Suspense>
          </FillBand>
        </>)}

        {tab === "image" && (<>
          <ImageTab
            hostId={host}
            info={info}
            update={update}
            checking={updates.checking}
            canOperate={canOperate}
            onCheck={() => void updates.checkNow()}
            onUpdate={() => setUpdateOpen(true)}
            containerId={id}
          />
        </>)}

        {tab === "inspect" && (<>
          <FillBand fill={fill}>
            <InspectTab data={inspectQuery.data} />
          </FillBand>
        </>)}
      </div>

      <ConfirmModal
        opened={killOpen}
        onClose={() => setKillOpen(false)}
        title={`Kill ${name}`}
        confirmLabel="Kill container"
        loading={acting === "kill"}
        onConfirm={() => actionMutation.mutate("kill", { onSettled: () => setKillOpen(false) })}
        message={
          <>
            <Text as="span" weight="semibold">{name}</Text> is stopped at once with SIGKILL, without the grace period Stop gives it to shut down cleanly. Use it when a container does not respond to Stop.
          </>
        }
      />

      <ConfirmModal
        opened={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title={`Remove ${name}`}
        confirmLabel="Remove container"
        loading={removeMutation.isPending}
        onConfirm={() => removeMutation.mutate()}
        message={
          <>
            <Text as="span" weight="semibold">{name}</Text> and its writable layer are deleted. Named volumes and bind mounts are kept.
            {managed && (
              <>
                {" "}It belongs to {origin.kind === "stack" ? "the compose stack" : "the deployment"}{" "}
                <Text as="span" weight="semibold">{origin.name}</Text>; the next {origin.kind === "stack" ? "compose up" : "deploy"} creates it again.
              </>
            )}
          </>
        }
      >
        <Checkbox
          label={running ? "Force: stop it first (required while it is running)" : "Force remove"}
          checked={forceRemove}
          onChange={(e) => setForceRemove(e.currentTarget.checked)}
        />
      </ConfirmModal>

      <ConfirmModal
        opened={updateOpen}
        onClose={() => setUpdateOpen(false)}
        title={`Update ${name}`}
        confirmLabel="Pull and recreate"
        confirmColor="neutral"
        loading={updateMutation.isPending}
        lockWhileLoading
        onConfirm={() => updateMutation.mutate({ host, id })}
        message={
          <>
            Pull the latest <Text as="span" mono weight="semibold">{info.image}</Text> and recreate{" "}
            <Text as="span" weight="semibold">{name}</Text> with the same configuration. The container is
            stopped and replaced, and gets a new ID; this page follows it.
          </>
        }
      />

      {exposeOpen && (
        <ExposeDialog
          open={exposeOpen}
          onClose={() => setExposeOpen(false)}
          hostId={host}
          name={name}
          target={{ kind: "container", id }}
          defaultPort={info.ports.map((p) => parseInt(p.containerPort, 10)).find((n) => Number.isFinite(n))}
        />
      )}

      {!managed && (
        <EditRecreateModal
          opened={editHandlers.opened}
          onClose={editHandlers.close}
          hostId={host}
          container={container}
          onRecreated={(newId) => {
            editHandlers.close();
            invalidate();
            // Recreate replaces the container under the same name with a NEW id:
            // follow it, or this page would 404.
            if (newId && newId !== id) followTo(newId);
          }}
        />
      )}
    </>
  );
}

const ACTION_DONE: Record<ContainerActionType, string> = {
  start: "Start requested",
  stop: "Stop requested",
  restart: "Restart requested",
  pause: "Paused",
  unpause: "Unpaused",
  kill: "Killed",
};

// ---------------------------------------------------------------------------
// Layout pieces: full-bleed solid bands (never transparent over the canvas).
// ---------------------------------------------------------------------------

function Band({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={`min-w-0 bg-card shadow-[inset_0_0_0_1px_var(--border)] ${className ?? ""}`}>{children}</section>;
}

interface Fill {
  ref: React.RefObject<HTMLDivElement | null>;
  height?: number;
}

/**
 * Height that takes an element from where it starts down to the bottom of the
 * scrolling <main>, so logs, console and files use the whole viewport.
 */
function useFillHeight(active: boolean, key: string): Fill {
  const ref = useRef<HTMLDivElement | null>(null);
  const [height, setHeight] = useState<number>();
  useLayoutEffect(() => {
    if (!active) return;
    const el = ref.current;
    const main = el?.closest("main");
    if (!el || !main) return;
    const calc = () => {
      const top = el.getBoundingClientRect().top - main.getBoundingClientRect().top + main.scrollTop;
      setHeight(Math.max(380, Math.floor(main.clientHeight - top)));
    };
    calc();
    const ro = new ResizeObserver(calc);
    ro.observe(main);
    window.addEventListener("resize", calc);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", calc);
    };
  }, [active, key]);
  return { ref, height };
}

function FillBand({ fill, padded, children }: { fill?: Fill; padded?: boolean; children: ReactNode }) {
  return (
    <div
      ref={fill?.ref}
      className={`flex min-h-0 min-w-0 flex-col bg-card shadow-[inset_0_0_0_1px_var(--border)] ${padded ? "p-[12px]" : ""}`}
      style={{ height: fill?.height ?? 520 }}
    >
      {children}
    </div>
  );
}

/** A titled card: its heading band, then content. `grouped` drops the frame for a section of a shared card. */
function Panel({
  title, meta, actions, flush, grouped, children,
}: { title: string; meta?: ReactNode; actions?: ReactNode; flush?: boolean; grouped?: boolean; children: ReactNode }) {
  return (
    <section className={grouped ? "flex min-w-0 flex-col" : "flex min-w-0 flex-col bg-card shadow-[inset_0_0_0_1px_var(--border)]"}>
      <header className="flex min-h-[52px] items-center justify-between gap-[12px] px-[16px] py-[8px] shadow-[inset_0_-1px_0_var(--border)]">
        <h2 className="m-0 flex items-baseline gap-[12px] text-[16px] leading-[24px] font-semibold tracking-normal">
          {title}
          {meta != null && <span className="text-[13px] font-normal text-muted-foreground">{meta}</span>}
        </h2>
        {actions && <div className="flex items-center gap-[4px]">{actions}</div>}
      </header>
      <div className={flush ? "min-w-0" : "min-w-0 p-[16px]"}>{children}</div>
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <p className="m-0 px-[16px] py-5 text-[13px] text-muted-foreground">{children}</p>;
}

/** Definition rows (label · value) for config facts. */
function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="m-0 grid grid-cols-[minmax(110px,max-content)_1fr] gap-x-9 gap-y-[4px] text-[13px]">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="m-0 min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function DetailSkeleton({ id }: { id: string }) {
  return (
    <>
      <PageHeader
        title={<span className="flex flex-col gap-3"><Skeleton width={260} height={24} /><Skeleton width={340} height={12} /></span>}
        breadcrumbs={[{ label: "Workloads", to: "/workloads" }, { label: shortId(id) }]}
      />
      <Band>
        <div className="grid grid-cols-2 gap-px bg-border md:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => (
            <div key={i} className="flex flex-col gap-3 bg-card px-[16px] py-5">
              <Skeleton width={70} height={10} />
              <Skeleton width="70%" height={14} />
            </div>
          ))}
        </div>
      </Band>
      <Band>
        <div className="flex flex-col gap-3 px-[16px] py-6">
          {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} height={14} width={`${80 - i * 7}%`} />)}
        </div>
      </Band>
    </>
  );
}

// ---------------------------------------------------------------------------
// Inspect JSON parsing.
// ---------------------------------------------------------------------------

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : {};
}
function asString(v: unknown): string {
  return typeof v === "string" ? v : "";
}
function asNumber(v: unknown): number {
  return typeof v === "number" ? v : 0;
}
function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}
/** Docker writes "0001-01-01T00:00:00Z" for "never". */
function realTime(iso: string): string {
  return iso && !iso.startsWith("0001-") ? iso : "";
}

interface MountInfo { type: string; name: string; source: string; destination: string; rw: boolean }
interface NetworkInfo { name: string; id: string; ip: string; gateway: string; mac: string; aliases: string[] }
interface PublishedPort { containerPort: string; bindings: string[] }

type Info = ReturnType<typeof parseInspect>;

function parseInspect(insp: ContainerInspect | undefined) {
  const root = asRecord(insp);
  const state = asRecord(root.State);
  const config = asRecord(root.Config);
  const hostConfig = asRecord(root.HostConfig);
  const networkSettings = asRecord(root.NetworkSettings);
  const health = asRecord(state.Health);
  const healthLog = Array.isArray(health.Log) ? health.Log.map(asRecord) : [];
  const lastProbe = healthLog[healthLog.length - 1];

  const mounts: MountInfo[] = Array.isArray(root.Mounts)
    ? root.Mounts.map((m) => {
        const r = asRecord(m);
        return {
          type: asString(r.Type),
          name: asString(r.Name),
          source: asString(r.Source),
          destination: asString(r.Destination),
          rw: r.RW !== false,
        };
      }).sort((a, b) => a.destination.localeCompare(b.destination))
    : [];

  const networks: NetworkInfo[] = Object.entries(asRecord(networkSettings.Networks)).map(([netName, v]) => {
    const r = asRecord(v);
    return {
      name: netName,
      id: asString(r.NetworkID),
      ip: asString(r.IPAddress),
      gateway: asString(r.Gateway),
      mac: asString(r.MacAddress),
      aliases: [...asStringArray(r.Aliases)].sort(),
    };
  }).sort((a, b) => a.name.localeCompare(b.name));

  const ports: PublishedPort[] = Object.entries(asRecord(networkSettings.Ports))
    .map(([containerPort, v]) => ({
      containerPort,
      bindings: Array.isArray(v)
        ? v.map((b) => {
            const r = asRecord(b);
            const ip = asString(r.HostIp);
            const port = asString(r.HostPort);
            return ip && ip !== "0.0.0.0" && ip !== "::" ? `${ip.includes(":") ? `[${ip}]` : ip}:${port}` : port;
          })
          // IPv4 and IPv6 bind the same port twice: show it once.
          .filter((b, i, all) => all.indexOf(b) === i)
          .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
        : [],
    }))
    // Docker keys ports as "443/tcp": by number, then protocol.
    .sort((a, b) => parseInt(a.containerPort, 10) - parseInt(b.containerPort, 10) || a.containerPort.localeCompare(b.containerPort));

  const labels: Record<string, string> = {};
  for (const [k, v] of Object.entries(asRecord(config.Labels))) if (typeof v === "string") labels[k] = v;

  const restart = asRecord(hostConfig.RestartPolicy);
  const restartName = asString(restart.Name) || "no";
  const restartMax = asNumber(restart.MaximumRetryCount);
  const healthcheck = asStringArray(asRecord(config.Healthcheck).Test);

  return {
    name: asString(root.Name).replace(/^\//, ""),
    imageId: asString(root.Image),
    status: asString(state.Status),
    oomKilled: state.OOMKilled === true,
    error: asString(state.Error),
    health: asString(health.Status),
    healthFailingStreak: asNumber(health.FailingStreak),
    healthLastOutput: asString(lastProbe?.Output).trim(),
    healthLastExit: typeof lastProbe?.ExitCode === "number" ? (lastProbe.ExitCode as number) : undefined,
    healthcheck: healthcheck[0] === "CMD-SHELL" ? healthcheck.slice(1).join(" ") : healthcheck[0] === "CMD" ? healthcheck.slice(1).join(" ") : "",
    startedAt: realTime(asString(state.StartedAt)),
    finishedAt: realTime(asString(state.FinishedAt)),
    exitCode: typeof state.ExitCode === "number" ? state.ExitCode : undefined,
    restartCount: asNumber(root.RestartCount),
    image: asString(config.Image),
    created: asString(root.Created),
    entrypoint: asStringArray(config.Entrypoint).join(" "),
    cmd: asStringArray(config.Cmd).join(" "),
    workingDir: asString(config.WorkingDir),
    user: asString(config.User),
    hostname: asString(config.Hostname),
    stopSignal: asString(config.StopSignal),
    restartPolicy: restartName === "on-failure" && restartMax > 0 ? `on-failure (max ${restartMax})` : restartName,
    networkMode: asString(hostConfig.NetworkMode),
    logDriver: asString(asRecord(hostConfig.LogConfig).Type),
    env: asStringArray(config.Env),
    labels,
    mounts,
    networks,
    ports,
    nanoCpus: asNumber(hostConfig.NanoCpus),
    cpuShares: asNumber(hostConfig.CpuShares),
    memory: asNumber(hostConfig.Memory),
    memoryReservation: asNumber(hostConfig.MemoryReservation),
    pidsLimit: asNumber(hostConfig.PidsLimit),
    privileged: hostConfig.Privileged === true,
    readonlyRootfs: hostConfig.ReadonlyRootfs === true,
    capAdd: asStringArray(hostConfig.CapAdd),
    autoRemove: hostConfig.AutoRemove === true,
  };
}

// ---------------------------------------------------------------------------
// State wording: one place, so the header, facts and problem band agree.
// ---------------------------------------------------------------------------

/** Compact duration for pills: 42s, 7m, 3h, 9d. */
function shortSince(iso: string): string {
  if (!iso) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function stateLabel(info: Info): string {
  const st = info.status.toLowerCase();
  if ((st === "exited" || st === "dead") && info.exitCode !== undefined) return `${st} ${info.exitCode}`;
  return st || "unknown";
}

function stateDuration(info: Info): string | undefined {
  const st = info.status.toLowerCase();
  if (st === "running" || st === "paused" || st === "restarting") return shortSince(info.startedAt) || undefined;
  if (st === "exited" || st === "dead") return shortSince(info.finishedAt) || undefined;
  return undefined;
}

interface Problem {
  color: Color;
  title: string;
  detail?: string;
  output?: string;
  outputLabel?: string;
}

const ANSI = new RegExp(String.raw`\u001b\[[0-9;?]*[A-Za-z]`, "g");
const LEADING_TS = /^\d{4}-\d{2}-\d{2}T\S+\s/;

/** One-shot read of the last lines of a stopped container's log (tail, no follow). */
function useLastLogLines(hostId: string, id: string, enabled: boolean, key: string, n = 6): string {
  const [text, setText] = useState("");
  useEffect(() => {
    setText("");
    if (!enabled) return;
    const lines: string[] = [];
    let ws: WebSocket;
    try {
      ws = new WebSocket(logsWebSocketURL(hostId, id, { tail: n, follow: false }));
    } catch {
      return;
    }
    const flush = () => setText(lines.slice(-n).join("\n"));
    ws.onmessage = (ev) => {
      const t = typeof ev.data === "string" ? ev.data : "";
      for (const l of t.split(/\r?\n/)) if (l.trim()) lines.push(l.replace(LEADING_TS, "").replace(ANSI, ""));
      flush();
    };
    const timer = window.setTimeout(() => ws.close(), 4000);
    return () => {
      window.clearTimeout(timer);
      ws.onmessage = null;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) ws.close();
    };
  }, [hostId, id, enabled, key, n]);
  return text;
}

function describeProblem(info: Info): Problem | null {
  const st = info.status.toLowerCase();
  if (st === "exited" || st === "dead") {
    if (info.oomKilled) {
      return { color: "red", title: `Killed for running out of memory, ${timeAgo(info.finishedAt)}`, detail: info.memory ? `Memory limit ${formatBytes(info.memory)}. Raise it or find the leak in the logs.` : "No memory limit is set, so the host itself ran out of memory." };
    }
    if (info.exitCode !== undefined && info.exitCode !== 0) {
      return {
        color: "red",
        title: `Exited with code ${info.exitCode}${info.finishedAt ? `, ${timeAgo(info.finishedAt)}` : ""}`,
        detail: info.error || exitHint(info.exitCode),
      };
    }
    return null;
  }
  if (st === "restarting") {
    return { color: "amber", title: `Restarting: ${info.restartCount} restart${info.restartCount === 1 ? "" : "s"} so far`, detail: `Restart policy ${info.restartPolicy}. The logs show why the process keeps exiting.` };
  }
  if (info.health === "unhealthy") {
    return {
      color: "red",
      title: `Health check failing (${info.healthFailingStreak} in a row)`,
      detail: info.healthcheck ? `Check: ${info.healthcheck}${info.healthLastExit !== undefined ? ` · last exit ${info.healthLastExit}` : ""}` : undefined,
      output: info.healthLastOutput || undefined,
      outputLabel: info.healthLastOutput ? "Last probe output" : undefined,
    };
  }
  return null;
}

function exitHint(code: number): string | undefined {
  switch (code) {
    case 1: return "The process reported an error. The last log lines usually say which.";
    case 125: return "Docker could not run the container (bad option or daemon error).";
    case 126: return "The command was found but could not be executed (permissions?).";
    case 127: return "The command was not found in the image.";
    case 137: return "Killed with SIGKILL: out of memory, docker kill, or a stop that timed out.";
    case 139: return "Segmentation fault inside the process.";
    case 143: return "Stopped with SIGTERM.";
    default: return undefined;
  }
}

function ProblemBand({ problem, onLogs }: { problem: Problem; onLogs?: () => void }) {
  return (
    <section className="min-w-0" role="status">
      <div
        className="flex flex-wrap items-start justify-between gap-x-9 gap-y-3 border-y border-[var(--t-line)] bg-[var(--t-soft)] px-[16px] py-[8px]"
        style={{ ...toneVars(problem.color), marginBlock: -1 }}
      >
        <div className="flex min-w-0 flex-1 items-start gap-[8px]">
          <span className="pt-[2px] text-[var(--t-text)]">
            <Icon name={problem.color === "red" ? "error-outline-rounded" : "warning-outline-rounded"} size="md" />
          </span>
          <div className="flex min-w-0 flex-col gap-[2px]">
            <p className="m-0 text-[13px] font-semibold text-[var(--t-text)]">{problem.title}</p>
            {problem.detail && <p className="m-0 text-[13px] text-foreground/85">{problem.detail}</p>}
            {problem.output && problem.outputLabel && (
              <span className="pt-2 text-[12px] text-muted-foreground">{problem.outputLabel}</span>
            )}
            {problem.output && (
              <pre className="m-0 mt-2 max-h-36 overflow-auto bg-card px-3 py-[4px] font-mono text-[12px] leading-6 whitespace-pre-wrap break-all text-foreground/90">
                {problem.output}
              </pre>
            )}
          </div>
        </div>
        {onLogs && (
          <Button size="sm" variant="outline" color="neutral" iconLeft="description-outline-rounded" onClick={onLogs}>
            Open logs
          </Button>
        )}
      </div>
    </section>
  );
}

function OriginText({ origin, appTo }: { origin: ContainerOrigin; appTo: string }) {
  if (origin.kind === "deployment") {
    return (
      <span>
        workload <Link to={appTo} className="cell-link">{origin.name}</Link>
      </span>
    );
  }
  if (origin.kind === "stack") {
    return (
      <span>
        workload <Link to={appTo} className="cell-link">{origin.name}</Link>
        {origin.service && <> / service <span className="text-foreground">{origin.service}</span></>}
      </span>
    );
  }
  return <span>standalone</span>;
}

// ---------------------------------------------------------------------------
// Overview (the facts sit in the header): runtime, then wiring (ports, networks, mounts) beside the
// runtime config and live resources, then environment, labels and schedules.
// ---------------------------------------------------------------------------

function OverviewTab({
  hostId, id, info, origin, appTo, active, onStats,
}: {
  hostId: string;
  id: string;
  info: Info;
  origin: ContainerOrigin;
  appTo: string;
  active: boolean;
  onStats: () => void;
}) {
  const st = info.status.toLowerCase();
  const running = st === "running";
  const hostQ = `?host=${encodeURIComponent(hostId)}`;

  return (
    <div className="flex flex-col gap-[20px]">
      {/* How it runs, how it is wired and its config in one card on the left; live usage, the stack and schedules beside. */}
      <section className="grid items-start gap-[20px] lg:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col bg-card shadow-[inset_0_0_0_1px_var(--border)] [&>section+section]:shadow-[inset_0_1px_0_var(--border)]">
          <Panel grouped title="Runtime">
            <Facts
              rows={[
                ["Command", <Code key="c">{[info.entrypoint, info.cmd].filter(Boolean).join(" ") || "—"}</Code>],
                ...(info.workingDir ? [["Working dir", <span key="w" className="font-mono">{info.workingDir}</span>] as [string, ReactNode]] : []),
                ["User", info.user ? <span className="font-mono">{info.user}</span> : <span className="text-muted-foreground">image default</span>],
                ["Hostname", <span key="h" className="font-mono">{info.hostname || "—"}</span>],
                ...(info.healthcheck ? [["Health check", <Code key="hc">{info.healthcheck}</Code>] as [string, ReactNode]] : []),
                ["Logging", info.logDriver || "—"],
                ["Created", <Tooltip key="cr" content={formatDate(info.created)}><span tabIndex={0}>{timeAgo(info.created)}</span></Tooltip>],
                ["ID", <span key="id" className="inline-flex items-center gap-2 font-mono">{shortId(id)}<CopyButton value={id} label="Copy container ID" /></span>],
              ]}
            />
          </Panel>
          <Panel grouped title="Ports" meta={info.ports.length || undefined} flush>
            {info.ports.length === 0 ? (
              <Empty>{info.networkMode === "host" ? "Host networking: the container uses the host's ports directly." : "No exposed ports."}</Empty>
            ) : (
              <Table bordered={false} className="[&_td:first-child]:pl-[16px] [&_th:first-child]:pl-[16px] [&_td:last-child]:pr-[16px] [&_th:last-child]:pr-[16px]">
                <Table.Header>
                  <Table.Row>
                    <Table.Head>Container</Table.Head>
                    <Table.Head>Published on host</Table.Head>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {info.ports.map((p) => (
                    <Table.Row key={p.containerPort}>
                      <Table.Cell shrink className="font-mono">{p.containerPort}</Table.Cell>
                      <Table.Cell className="font-mono">
                        {p.bindings.length > 0 ? [...new Set(p.bindings)].join(", ") : <span className="text-muted-foreground">not published</span>}
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            )}
          </Panel>
          <Panel grouped title="Mounts" meta={info.mounts.length || undefined} flush>
            {info.mounts.length === 0 ? (
              <Empty>No volumes or bind mounts: everything written inside is lost when the container is recreated.</Empty>
            ) : (
              <Table bordered={false} className="[&_td:first-child]:pl-[16px] [&_th:first-child]:pl-[16px] [&_td:last-child]:pr-[16px] [&_th:last-child]:pr-[16px]">
                <Table.Header>
                  <Table.Row>
                    <Table.Head>Source</Table.Head>
                    <Table.Head>Mounted at</Table.Head>
                    <Table.Head align="right">Mode</Table.Head>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {info.mounts.map((m, i) => (
                    <Table.Row key={`${m.destination}-${i}`}>
                      <Table.Cell className="font-mono break-all">
                        <span className="mr-[4px] font-sans text-[12px] text-muted-foreground ">{m.type}</span>
                        {m.type === "volume" && m.name ? (
                          <Link to={`/volumes/${encodeURIComponent(m.name)}${hostQ}`} className="cell-link">{m.name.length > 40 ? shortId(m.name) : m.name}</Link>
                        ) : (
                          m.source || "(anonymous)"
                        )}
                      </Table.Cell>
                      <Table.Cell className="font-mono break-all">{m.destination}</Table.Cell>
                      <Table.Cell align="right" shrink>
                        <span className={m.rw ? undefined : "text-[var(--tone-amber-text)]"}>{m.rw ? "rw" : "read-only"}</span>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            )}
          </Panel>
          <Panel grouped title="Networks" meta={info.networks.length || undefined} flush>
            {info.networks.length === 0 ? (
              <Empty>{info.networkMode ? `Network mode ${info.networkMode}.` : "Not attached to any network."}</Empty>
            ) : (
              <Table bordered={false} className="[&_td:first-child]:pl-[16px] [&_th:first-child]:pl-[16px] [&_td:last-child]:pr-[16px] [&_th:last-child]:pr-[16px]">
                <Table.Header>
                  <Table.Row>
                    <Table.Head>Network</Table.Head>
                    <Table.Head>IP</Table.Head>
                    <Table.Head>Aliases</Table.Head>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {info.networks.map((n) => (
                    <Table.Row key={n.name}>
                      <Table.Cell shrink>
                        {n.id ? <Link to={`/networks/${encodeURIComponent(n.id)}${hostQ}`} className="cell-link">{n.name}</Link> : n.name}
                      </Table.Cell>
                      <Table.Cell shrink className="font-mono">{n.ip || <span className="text-muted-foreground">—</span>}</Table.Cell>
                      <Table.Cell className="text-muted-foreground">
                        {n.aliases.filter((a) => !id.startsWith(a)).join(", ") || "—"}
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table.Body>
              </Table>
            )}
          </Panel>
          <EnvPanel grouped env={info.env} />
          <LabelsPanel grouped labels={info.labels} />
        </div>
        <div className="flex min-w-0 flex-col gap-[20px]">
          <ResourcesPanel hostId={hostId} id={id} info={info} active={active && running} running={running} onStats={onStats} />
          {origin.kind === "stack" && <StackPanel origin={origin} hostId={hostId} selfId={id} appTo={appTo} />}
          <ScheduleSection targetType="container" targetId={info.name} hostId={hostId} />
        </div>
      </section>
    </div>
  );
}

function UpdatePill({ update }: { update?: ContainerUpdateStatus }) {
  if (!update) return null;
  if (update.updateAvailable) {
    return (
      <Tooltip content={describeUpdate(update)}>
        <span tabIndex={0} className="inline-flex"><StatePill status="update" label="Update available" color="blue" size="sm" /></span>
      </Tooltip>
    );
  }
  if (update.error) {
    return (
      <Tooltip content={update.error}>
        <span tabIndex={0} className="inline-flex"><StatePill status="unknown" label="Update check failed" color="gray" size="sm" /></span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={describeUpdate(update)}>
      <span tabIndex={0} className="inline-flex"><StatePill status="ok" label="Up to date" color="gray" size="sm" /></span>
    </Tooltip>
  );
}

/** The other containers of the same compose stack, this one outlined. */
function StackPanel({ origin, hostId, selfId, appTo }: { origin: ContainerOrigin; hostId: string; selfId: string; appTo: string }) {
  const siblingsQuery = useQuery({
    queryKey: ["containers", hostId, "all"],
    queryFn: () => listContainers(hostId, true),
    refetchInterval: LIVE_REFETCH_MS,
  });
  const siblings = (siblingsQuery.data ?? [])
    .filter((c) => stackOf(c.labels) === origin.name)
    .sort((a, b) => (a.names[0] ?? "").localeCompare(b.names[0] ?? ""));
  if (siblings.length < 2) return null;
  return (
    <Panel
      title="Stack"
      meta={<>{siblings.length} containers in <Link to={appTo} className="cell-link">{origin.name}</Link>{origin.service ? ` · service ${origin.service}` : ""}</>}
    >
      <span className="flex flex-wrap gap-[4px]">
        {siblings.map((c) => {
          const n = (c.names[0] ?? c.id).replace(/^\//, "");
          const self = c.id === selfId || selfId.startsWith(c.id) || c.id.startsWith(selfId);
          const pill = <StatePill status={c.state} label={n} size="sm" className="normal-case tracking-normal" />;
          return self ? (
            <span key={c.id} aria-current="page" className="inline-flex outline outline-1 outline-offset-1 outline-[var(--ring)]">{pill}</span>
          ) : (
            <Link key={c.id} to={`/containers/${c.id}?host=${encodeURIComponent(hostId)}`} className="inline-flex no-underline hover:opacity-80" title={`${n}: ${c.status}`}>
              {pill}
            </Link>
          );
        })}
      </span>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Environment (secrets masked) and labels.
// ---------------------------------------------------------------------------

const SECRET_KEY = /(pass(word)?|secret|token|key|credential|auth|private|dsn|salt|cookie)/i;

function EnvPanel({ env, grouped }: { env: string[]; grouped?: boolean }) {
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const rows = env.map((e) => {
    const eq = e.indexOf("=");
    const key = eq >= 0 ? e.slice(0, eq) : e;
    const value = eq >= 0 ? e.slice(eq + 1) : "";
    return { key, value, secret: SECRET_KEY.test(key) && value !== "" };
  });
  const secrets = rows.filter((r) => r.secret).length;
  const toggle = (k: string) =>
    setRevealed((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  return (
    <Panel
        grouped={grouped}
        title="Environment"
        meta={rows.length ? `${rows.length}${secrets ? ` · ${secrets} masked` : ""}` : undefined}
        flush
      >
        {rows.length === 0 ? (
          <Empty>No environment variables.</Empty>
        ) : (
          <Table bordered={false} className="[&_td:first-child]:pl-[16px] [&_th:first-child]:pl-[16px] [&_td:last-child]:pr-[16px] [&_th:last-child]:pr-[16px]">
            <Table.Header>
              <Table.Row>
                <Table.Head>Variable</Table.Head>
                <Table.Head>Value</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((r, i) => {
                const show = !r.secret || revealed.has(r.key);
                return (
                  <Table.Row key={`${r.key}-${i}`}>
                    <Table.Cell shrink className="font-mono font-medium">{r.key}</Table.Cell>
                    <Table.Cell className="font-mono break-all">
                      <span className="inline-flex max-w-full items-center gap-[4px]">
                        {show ? (
                          r.value || <span className="text-muted-foreground">(empty)</span>
                        ) : (
                          <span className="tracking-[0.2em] text-muted-foreground" aria-label="masked value">••••••••</span>
                        )}
                        {r.secret && (
                          <Button
                            variant="clear"
                            size="xs"
                            iconLeft={show ? "visibility-off-outline-rounded" : "visibility-outline-rounded"}
                            aria-label={show ? `Hide ${r.key}` : `Reveal ${r.key}`}
                            onClick={() => toggle(r.key)}
                          />
                        )}
                      </span>
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </Table>
        )}
    </Panel>
  );
}

function LabelsPanel({ labels, grouped }: { labels: Record<string, string>; grouped?: boolean }) {
  const [open, setOpen] = useState(false);
  const entries = Object.entries(labels).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Panel
        grouped={grouped}
        title="Labels"
        meta={entries.length || undefined}
        flush
        actions={
          entries.length > 0 && (
            <Button size="xs" variant="clear" color="neutral" iconRight="arrow-drop-down-rounded" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {open ? "Hide" : "Show"}
            </Button>
          )
        }
      >
        {entries.length === 0 ? (
          <Empty>No labels.</Empty>
        ) : open ? (
          <Table bordered={false} className="[&_td:first-child]:pl-[16px] [&_th:first-child]:pl-[16px] [&_td:last-child]:pr-[16px] [&_th:last-child]:pr-[16px]">
            <Table.Header>
              <Table.Row>
                <Table.Head>Label</Table.Head>
                <Table.Head>Value</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {entries.map(([k, v]) => (
                <Table.Row key={k}>
                  <Table.Cell shrink className="font-mono">{k}</Table.Cell>
                  <Table.Cell className="font-mono break-all text-muted-foreground">{v || "(empty)"}</Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table>
        ) : (
          <Empty>{entries.slice(0, 4).map(([k]) => k).join(", ")}{entries.length > 4 ? `, and ${entries.length - 4} more` : ""}</Empty>
        )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Live stats over a WebSocket (~1 sample/s), shared by Overview and Stats.
// ---------------------------------------------------------------------------

type StatsStatus = "idle" | "connecting" | "live" | "closed" | "error";
const STATS_HISTORY = 90;

function useStatsStream(hostId: string, containerId: string, active: boolean) {
  const [history, setHistory] = useState<{ t: number; s: ContainerStats }[]>([]);
  const [status, setStatus] = useState<StatsStatus>("idle");

  useEffect(() => {
    if (!active) {
      setStatus("idle");
      return;
    }
    setHistory([]);
    setStatus("connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(statsStreamWebSocketURL(hostId, containerId));
    } catch {
      setStatus("error");
      return;
    }
    socket.onopen = () => setStatus("live");
    socket.onmessage = (ev) => {
      const text = typeof ev.data === "string" ? ev.data : "";
      for (const line of text.split(/\r?\n/)) {
        if (!line.trim()) continue;
        try {
          const s = JSON.parse(line) as ContainerStats;
          setHistory((prev) => [...prev, { t: Date.now(), s }].slice(-STATS_HISTORY));
        } catch {
          // Ignore non-JSON frames (e.g. a server "error: …" notice).
        }
      }
    };
    socket.onerror = () => setStatus("error");
    socket.onclose = () => setStatus((st) => (st === "live" ? "closed" : st));
    return () => {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) socket.close();
    };
  }, [active, hostId, containerId]);

  return { stats: history[history.length - 1]?.s ?? null, history, status };
}

function usageColor(pct: number): Color {
  return pct >= 90 ? "red" : pct >= 75 ? "amber" : "primary";
}

function ResourcesPanel({
  hostId, id, info, active, running, onStats,
}: { hostId: string; id: string; info: Info; active: boolean; running: boolean; onStats: () => void }) {
  const { stats, status } = useStatsStream(hostId, id, active);
  const cpuLimit = info.nanoCpus > 0 ? `${+(info.nanoCpus / 1e9).toFixed(2)} CPU` : "no CPU limit";
  const memLimited = info.memory > 0;
  return (
    <Panel
      title="Resources"
      meta={running ? (status === "live" ? "live" : status === "error" ? "stream unavailable" : "connecting…") : "not running"}
      actions={
        <Button size="xs" variant="clear" color="neutral" iconLeft="show-chart-rounded" onClick={onStats}>
          Stats
        </Button>
      }
    >
      <div className="flex flex-col gap-5">
        <Meter
          label="CPU"
          value={running && stats ? formatPercent(stats.cpuPercent) : "—"}
          limit={cpuLimit}
          pct={running && stats ? stats.cpuPercent : undefined}
        />
        <Meter
          label="Memory"
          value={running && stats ? formatBytes(stats.memUsage) : "—"}
          limit={memLimited ? `of ${formatBytes(info.memory)} limit` : "no memory limit"}
          pct={running && stats ? stats.memPercent : undefined}
        />
        <Facts
          rows={[
            ["PIDs", running && stats ? `${stats.pids}${info.pidsLimit > 0 ? ` of ${info.pidsLimit}` : ""}` : info.pidsLimit > 0 ? `limit ${info.pidsLimit}` : "—"],
            ["Privileged", info.privileged ? <span className="text-[var(--tone-amber-text)]">yes</span> : "no"],
            ...(info.readonlyRootfs ? [["Root FS", "read-only"] as [string, ReactNode]] : []),
            ...(info.capAdd.length ? [["Capabilities", <span key="cap" className="font-mono">+{info.capAdd.join(" +")}</span>] as [string, ReactNode]] : []),
          ]}
        />
      </div>
    </Panel>
  );
}

function Meter({ label, value, limit, pct }: { label: string; value: string; limit: string; pct?: number }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-5 text-[13px]">
        <span className="text-muted-foreground">{label}</span>
        <span>
          <span className="font-medium">{value}</span>
          <span className="text-muted-foreground"> {limit}</span>
        </span>
      </div>
      <Progress value={pct === undefined ? 0 : Math.min(pct, 100)} color={pct === undefined ? "gray" : usageColor(pct)} />
    </div>
  );
}

function StatsTab({
  hostId, id, active, running, memoryLimit,
}: { hostId: string; id: string; active: boolean; running: boolean; memoryLimit: number }) {
  const { stats, history, status } = useStatsStream(hostId, id, active && running);

  if (!running) {
    return (
      <Band className="border-t-0">
        <EmptyState
          icon={<Icon name="show-chart-rounded" size="lg" />}
          title="No live stats while the container is stopped"
          description="CPU, memory, network and disk figures stream only from a running container."
        />
      </Band>
    );
  }
  if (!stats) {
    return (
      <Band className="border-t-0">
        {status === "error" ? (
          <EmptyState
            icon={<Icon name="error-outline-rounded" size="lg" />}
            title="Could not stream stats"
            description="The stats socket closed. Check that the host's agent is online, then reopen this tab."
          />
        ) : (
          <div className="flex items-center gap-3 px-[16px] py-9">
            <Spinner size="sm" />
            <Text size="sm" color="muted">Connecting to live stats…</Text>
          </div>
        )}
      </Band>
    );
  }

  const labels = history.map((h) => new Date(h.t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  return (
    <div className="flex flex-col gap-[20px]">
      <section className="min-w-0 bg-card shadow-[inset_0_0_0_1px_var(--border)]">
        <dl className="m-0 grid grid-cols-2 gap-px bg-border p-px md:grid-cols-3 xl:grid-cols-5">
          <Stat label="CPU" value={formatPercent(stats.cpuPercent)} tone={usageColor(stats.cpuPercent)} />
          <Stat
            label="Memory"
            value={formatBytes(stats.memUsage)}
            sub={`${formatPercent(stats.memPercent)} of ${formatBytes(stats.memLimit)}${memoryLimit > 0 ? " limit" : " (host)"}`}
            tone={usageColor(stats.memPercent)}
          />
          <Stat label="Network rx / tx" value={`${formatBytes(stats.netRx)} / ${formatBytes(stats.netTx)}`} sub="since start" />
          <Stat label="Block read / write" value={`${formatBytes(stats.blkRead)} / ${formatBytes(stats.blkWrite)}`} sub="since start" />
          <Stat label="Processes" value={String(stats.pids)} sub={status === "live" ? `live · ${history.length} samples` : status} />
        </dl>
      </section>
      <section className="grid gap-[20px] lg:grid-cols-2">
        <div className="min-w-0">
          <Panel title="CPU" meta="% of one core per 100">
            <LineChart labels={labels} series={[{ name: "CPU", data: history.map((h) => h.s.cpuPercent), color: "primary" }]} unit="%" area height={200} />
          </Panel>
        </div>
        <div className="min-w-0">
          <Panel title="Memory" meta="% of limit">
            <LineChart labels={labels} series={[{ name: "Memory", data: history.map((h) => h.s.memPercent), color: "green" }]} unit="%" yMax={100} area height={200} />
          </Panel>
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: Color }) {
  return (
    <div className="flex min-w-0 flex-col gap-[2px] bg-card px-[16px] py-[8px]">
      <dt className="text-[12px] tracking-normal text-muted-foreground font-sans ">{label}</dt>
      <dd className="m-0 truncate text-[20px] font-medium tracking-[-0.02em]" style={tone && tone !== "primary" ? { ...toneVars(tone), color: "var(--t-text)" } : undefined}>
        {value}
      </dd>
      {sub && <span className="truncate text-[12px] text-muted-foreground">{sub}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Logs, Image, Inspect.
// ---------------------------------------------------------------------------

function LogsTab({ hostId, id, name, active }: { hostId: string; id: string; name: string; active: boolean }) {
  const sources = useMemo<LogSource[]>(
    () => (active ? [{ key: `${hostId}/${id}`, label: name, url: (opts) => logsWebSocketURL(hostId, id, opts) }] : []),
    [active, hostId, id, name],
  );
  return <LogViewer sources={sources} height="100%" emptyTitle="Logs paused" emptyDescription="Streaming resumes when this tab is open." />;
}

function ImageTab({
  hostId, containerId, info, update, checking, canOperate, onCheck, onUpdate,
}: {
  hostId: string;
  containerId: string;
  info: Info;
  update?: ContainerUpdateStatus;
  checking: boolean;
  canOperate: boolean;
  onCheck: () => void;
  onUpdate: () => void;
}) {
  const digest = (d?: string) => (d ? d.replace(/^sha256:/, "").slice(0, 12) : "—");
  return (
    <div className="flex flex-col gap-[20px]">
      <section>
        <Panel
          title="Image"
          actions={
            <>
              <Button size="sm" variant="outline" color="neutral" iconLeft="refresh-rounded" loading={checking} onClick={onCheck}>
                Check registry
              </Button>
              {canOperate && update?.updateAvailable && (
                <Button size="sm" variant="outline" color="blue" iconLeft="upgrade-rounded" onClick={onUpdate}>
                  Pull and recreate
                </Button>
              )}
            </>
          }
        >
          <Facts
            rows={[
              ["Reference", <span key="r" className="font-mono break-all">{info.image}</span>],
              [
                "Image ID",
                info.imageId ? (
                  <Link key="i" to={`/images/${encodeURIComponent(info.imageId)}?host=${encodeURIComponent(hostId)}`} className="cell-link font-mono">
                    {shortId(info.imageId)}
                  </Link>
                ) : (
                  "—"
                ),
              ],
              ["Status", update ? <span key="s" className="inline-flex items-center gap-3"><UpdatePill update={update} /><span className="text-muted-foreground">checked {timeAgo(update.checkedAt)}</span></span> : <span className="text-muted-foreground">Not checked yet</span>],
              ["Running digest", <span key="ld" className="font-mono">{digest(update?.localDigest)}</span>],
              ["Registry digest", <span key="rd" className="font-mono">{digest(update?.remoteDigest)}</span>],
            ]}
          />
          {update?.error && (
            <Callout color="warning" title="The registry could not be checked">
              {update.error}
            </Callout>
          )}
        </Panel>
      </section>
      <PolicyEditor kind="container" hostId={hostId} containerId={containerId} />
      {info.imageId && <ImageVulnerabilities hostId={hostId} imageId={info.imageId} />}
    </div>
  );
}

function InspectTab({ data }: { data: ContainerInspect | undefined }) {
  const pretty = useMemo(() => (data ? JSON.stringify(data, null, 2) : ""), [data]);
  return (
    <>
      <div className="flex items-center justify-between gap-5 border-b bg-muted/50 px-[16px] py-[4px]">
        <Text size="xs" color="muted">docker inspect · refreshes with the page</Text>
        <span className="inline-flex items-center gap-2 text-[12px] text-muted-foreground">
          Copy JSON <CopyButton value={pretty} label="Copy inspect JSON" />
        </span>
      </div>
      <pre className="m-0 min-h-0 flex-1 overflow-auto bg-card px-[16px] py-5 font-mono text-[13px] leading-[1.55] whitespace-pre">
        {pretty}
      </pre>
    </>
  );
}

// ---------------------------------------------------------------------------
// Edit & recreate modal: prefills the spec form from getContainerSpec and
// recreates the container in place with the SAME name.
// ---------------------------------------------------------------------------

interface EditRecreateModalProps {
  opened: boolean;
  onClose: () => void;
  hostId: string;
  container: Container | null;
  onRecreated: (newId: string) => void;
}

function EditRecreateModal({ opened, onClose, hostId, container, onRecreated }: EditRecreateModalProps) {
  const name = container ? container.names[0] ?? "" : "";
  const [form, setForm] = useState<ContainerSpecFormValue>(emptyContainerSpecForm);
  const [error, setError] = useState<string | null>(null);
  // Seed the form from the fetched spec only ONCE per open: a background refetch
  // (window refocus) would otherwise overwrite the user's in-progress edits.
  const seededRef = useRef(false);

  const specQuery = useQuery({
    queryKey: ["container-spec", hostId, container?.id],
    queryFn: () => getContainerSpec(hostId, container!.id),
    enabled: opened && container !== null,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  useEffect(() => {
    if (!opened) {
      seededRef.current = false;
      return;
    }
    if (specQuery.data && !seededRef.current) {
      seededRef.current = true;
      setError(null);
      setForm(containerSpecFormFromSpec(specQuery.data, name));
    }
  }, [opened, specQuery.data, name]);

  const recreateMutation = useMutation({
    mutationFn: () => createContainer(hostId, name, containerSpecFormToSpec(form), form.registryId || undefined),
    onSuccess: (res) => {
      notify({ color: "green", title: "Container recreated", message: `Recreated ${name}` });
      onRecreated(res.id);
    },
    onError: (err) => notify({ color: "red", title: `Could not recreate ${name}`, message: errorMessage(err, "The Docker daemon rejected the new configuration") }),
  });

  const submit = () => {
    const validationError = validateContainerSpecForm(form);
    if (validationError) {
      setError(validationError);
      return;
    }
    setError(null);
    recreateMutation.mutate();
  };

  return (
    <Popup open={opened} onOpenChange={(o) => { if (!o) onClose(); }} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>
            Edit and recreate <Text as="span" color="muted" mono>{name}</Text>
          </Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {specQuery.isLoading ? (
            <Box direction="column" gap={2}>
              {Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} height={24} />)}
            </Box>
          ) : specQuery.isError ? (
            <Callout color="danger" title="Could not load the current configuration">
              {errorMessage(specQuery.error, "The host did not return the container spec.")}
            </Callout>
          ) : (
            <Box direction="column" gap={4}>
              <Callout color="warning" title="The container is replaced">
                Saving stops and removes <b>{name}</b>, then creates a new one with this configuration and the same name.
                Data outside named volumes and bind mounts (anonymous volumes, files written in the container) is lost.
              </Callout>
              <ContainerSpecForm value={form} onChange={setForm} hideName />
              {error && <Text color="danger" size="sm">{error}</Text>}
            </Box>
          )}
        </Popup.Body>
        {!specQuery.isLoading && !specQuery.isError && (
          <Popup.Footer>
            <Button variant="clear" color="neutral" onClick={onClose} disabled={recreateMutation.isPending}>
              Cancel
            </Button>
            <Button color="orange" iconLeft="refresh-rounded" onClick={submit} loading={recreateMutation.isPending}>
              Recreate container
            </Button>
          </Popup.Footer>
        )}
      </Popup.Content>
    </Popup>
  );
}

