import { Fragment, useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { ChevronDown, ChevronRight, CircleAlert, Ellipsis, HeartPulse, LayoutGrid, List, Plus, WifiOff } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import ConfirmModal from "../components/ConfirmModal";
import { prune } from "../api/client";
import { useContainerUpdates } from "../lib/updates";
import { formatBytes } from "../utils";
import { notify } from "../ui/toast";
import { Menu, Skeleton } from "@/ui/kit";
import { useHost, isAllHosts } from "../HostContext";
import { appHref, containerCount, isReplaced, kindShort, liveFirst, servicesOf, useApps, type App, type AppContainer } from "../lib/apps";
import { BtnLink, Btn, Crumbs, HostChip, LBL, Pill, SectHead, StateSquares, Tag } from "../components/apps/ui";
import ContainerActionsMenu from "../components/apps/ContainerActionsMenu";
import { useFeatures } from "../lib/features";
import { cn } from "@/lib/utils";
import { useCanOperate } from "../components/deployment/shared";
import { errorMessage } from "../utils";

// ---------------------------------------------------------------------------
// Apps (/apps): stacks, deployments and lone containers in the current host
// scope, as calm cards (or a table). What needs attention comes first; the
// filter and the view live in the URL (?filter=attn&view=table).
// ---------------------------------------------------------------------------

type Filter = "all" | "attn" | "upd" | "pub";
const FILTERS: { id: Filter; label: string; match: (a: App) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "attn", label: "Needs you", match: (a) => !!a.attention },
  { id: "upd", label: "Updates", match: (a) => a.updates > 0 },
  { id: "pub", label: "Public", match: (a) => a.ports.some((p) => p.public) },
];

const VIEW_KEY = "kowan-workloads-view";
type View = "cards" | "table";
function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "table" ? "table" : "cards";
  } catch {
    return "cards";
  }
}

export default function Workloads() {
  const { hostId } = useHost();
  const features = useFeatures();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  // Cards or table is a per-browser preference, not part of the URL.
  const [view, setViewState] = useState<View>(readView);
  const setView = (v: View) => {
    setViewState(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      /* private mode */
    }
  };
  const rawFilter = params.get("filter") as Filter | null;
  const filter: Filter = FILTERS.some((f) => f.id === rawFilter) ? (rawFilter as Filter) : "all";
  const { apps, hosts, isLoading, error, refetch } = useApps(hostId);
  const canOperate = useCanOperate();
  const all = isAllHosts(hostId);
  const scopeName = all ? "All hosts" : hosts.find((h) => h.id === hostId)?.name || hostId;

  const setParam = (k: string, v: string | null) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      if (v == null) n.delete(k);
      else n.set(k, v);
      return n;
    }, { replace: true });

  const list = useMemo(() => (apps ?? []).filter(FILTERS.find((f) => f.id === filter)!.match), [apps, filter]);
  const nContainers = (apps ?? []).reduce((s, a) => s + a.total, 0);
  const hostOnline = (id: string) => hosts.find((h) => h.id === id)?.online ?? true;
  const hostName = (id: string) => hosts.find((h) => h.id === id)?.name || id;
  const offlineHosts = all ? hosts.filter((h) => !h.online) : hosts.filter((h) => h.id === hostId && !h.online);

  const viewSwitch = (
    <div role="group" aria-label="View" className="inline-flex bg-[var(--popover)] shadow-[inset_0_0_0_1px_var(--border)]">
      {(["cards", "table"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => setView(v)}
          className={cn(
            "inline-flex h-[32px] items-center gap-[6px] px-[12px] text-[13px] leading-none font-semibold outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:outline-[var(--signal)]",
            view === v ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
          )}
        >
          {v === "cards" ? <LayoutGrid className="size-[16px]" strokeWidth={1.5} aria-hidden /> : <List className="size-[16px]" strokeWidth={1.5} aria-hidden />}
          {v === "cards" ? "Cards" : "Table"}
        </button>
      ))}
    </div>
  );

  const newMenu = (
    <Menu placement="bottom-end">
      <Menu.Trigger>
        <Btn solid>
          <Plus className="size-[16px]" strokeWidth={1.5} aria-hidden />
          New workload
          <ChevronDown className="size-[16px]" strokeWidth={1.5} aria-hidden />
        </Btn>
      </Menu.Trigger>
      <Menu.Content ariaLabel="New workload">
        <Menu.Label>New workload on {all ? "a host you pick next" : scopeName}</Menu.Label>
        {[
          ...(features.templates ? [{ t: "From a template", d: "Databases, media, monitoring and more", to: "/templates" }] : []),
          { t: "Compose or container deployment", d: "Paste a compose file or describe one image", to: "/deployments/new" },
          { t: "From a git repository", d: "Choose Git as the compose source in the form", to: "/deployments/new" },
          { t: "Import stacks", d: "From Portainer or a folder of compose stacks", to: "/import" },
        ].map((m) => (
          <Menu.Item key={m.t} onSelect={() => navigate(m.to)}>
            <span className="flex w-[264px] flex-col gap-[2px] py-[2px] whitespace-normal">
              <b className="text-[14px] leading-[20px] font-semibold text-foreground">{m.t}</b>
              <span className="text-[13px] leading-[18px] text-muted-foreground">{m.d}</span>
            </span>
          </Menu.Item>
        ))}
      </Menu.Content>
    </Menu>
  );

  // Fleet-wide container chores that used to live on the Containers page.
  const qc = useQueryClient();
  const updates = useContainerUpdates(hostId);
  const [pruneOpen, setPruneOpen] = useState(false);
  const pruneHosts = hosts.filter((h) => h.online && (all || h.id === hostId));
  const pruneMutation = useMutation({
    mutationFn: async () => {
      let reclaimed = 0;
      for (const h of pruneHosts) reclaimed += (await prune(h.id, "containers")).spaceReclaimed ?? 0;
      return reclaimed;
    },
    onSuccess: (reclaimed) => {
      notify({ color: "green", title: "Pruned stopped containers", message: `Reclaimed ${formatBytes(reclaimed)}` });
      setPruneOpen(false);
      void qc.invalidateQueries({ queryKey: ["containers"] });
    },
    onError: (err) => notify({ color: "red", title: "Prune failed", message: errorMessage(err, "Could not prune containers") }),
  });
  const moreMenu = (
    <Menu placement="bottom-end">
      <Menu.Trigger>
        <Btn aria-label="More actions">
          <Ellipsis className="size-[16px]" strokeWidth={1.5} aria-hidden />
        </Btn>
      </Menu.Trigger>
      <Menu.Content ariaLabel="More actions">
        <Menu.Item onSelect={() => void updates.checkNow()}>
          {updates.checking ? "Checking for updates…" : updates.count > 0 ? `Check for updates (${updates.count} waiting)` : "Check for updates"}
        </Menu.Item>
        <Menu.Item onSelect={() => setPruneOpen(true)} disabled={pruneHosts.length === 0}>
          Prune stopped containers…
        </Menu.Item>
      </Menu.Content>
    </Menu>
  );
  const pruneModal = (
    <ConfirmModal
      opened={pruneOpen}
      onClose={() => setPruneOpen(false)}
      title="Prune stopped containers"
      confirmLabel="Prune"
      loading={pruneMutation.isPending}
      onConfirm={() => pruneMutation.mutate()}
      message={`Remove every stopped container on ${pruneHosts.length === 1 ? pruneHosts[0].name || pruneHosts[0].id : `${pruneHosts.length} online hosts`}. Running containers and volumes are not touched.`}
    />
  );

  const head = (
    <section className="bg-card px-[16px] pt-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
      <Crumbs hostScope={hostId} parts={[{ label: "Workloads" }]} />
      <div className="mt-[6px] flex flex-wrap items-end gap-[16px] pb-[16px]">
        <h1 className="m-0 text-[24px] leading-[32px] font-extrabold tracking-[-0.02em] uppercase">Workloads</h1>
        <div className="flex flex-wrap gap-[16px] pb-[6px]">
          {[
            [apps?.length ?? "–", "workloads"],
            [apps ? nContainers : "–", "containers"],
            [all ? hosts.length : 1, all && hosts.length !== 1 ? "hosts" : "host"],
          ].map(([n, l]) => (
            <span key={String(l)} className={LBL}>
              <b className="font-semibold text-foreground">{n}</b> {l}
            </span>
          ))}
        </div>
        <div className="ml-auto flex flex-wrap gap-[8px] pb-[2px]">
          {viewSwitch}
          {/* Viewers only look: creating and pruning are operator actions. */}
          {canOperate && newMenu}
          {canOperate && moreMenu}
        </div>
      </div>
    </section>
  );

  const toolbar = (
    <div role="group" aria-label="Filter workloads" className="flex flex-wrap items-center gap-[8px] bg-card px-[12px] py-[10px] shadow-[inset_0_0_0_1px_var(--border)]">
      {FILTERS.map((f) => {
        const on = filter === f.id;
        const n = (apps ?? []).filter(f.match).length;
        return (
          <button
            key={f.id}
            type="button"
            aria-pressed={on}
            onClick={() => setParam("filter", f.id === "all" ? null : f.id)}
            className={cn(
              "inline-flex h-[32px] items-center gap-[8px] px-[12px] text-[13px] leading-none font-semibold whitespace-nowrap outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-[var(--signal)]",
              on ? "bg-primary text-primary-foreground" : "bg-card text-foreground shadow-[inset_0_0_0_1px_var(--border)] hover:bg-[var(--popover)]",
            )}
          >
            {f.label}
            <span className={cn("font-mono text-[12px] leading-none font-medium", on ? "text-primary-foreground opacity-75" : "text-muted-foreground")}>{apps ? n : "–"}</span>
          </button>
        );
      })}
      <span className="flex-1" />
      <span className="text-[13px] text-muted-foreground">Stacks, deployments and lone containers · {scopeName}</span>
    </div>
  );

  let body;
  if (isLoading && !apps) {
    body = (
      <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-[12px]" aria-busy="true" aria-label="Loading workloads">
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className="flex h-[176px] flex-col gap-[12px] bg-card p-[16px] shadow-[inset_0_0_0_1px_var(--border)]">
            <Skeleton height={20} width="60%" />
            <Skeleton height={16} width="40%" />
            <Skeleton height={12} width="50%" />
          </div>
        ))}
      </div>
    );
  } else if (error && !apps?.length) {
    body = (
      <Empty title="Couldn't load the workloads" text={errorMessage(error, "The server did not answer.")}>
        <Btn onClick={refetch}>Retry</Btn>
      </Empty>
    );
  } else if (!apps?.length) {
    body = (
      <Empty title={`Nothing runs on ${scopeName} yet`} text="Deploy a compose stack or a single container, or start one from a template. Stacks and containers started outside Kowan show up here on their own.">
        <BtnLink to="/deployments/new" solid>New deployment</BtnLink>
        {features.templates && <BtnLink to="/templates">Browse templates</BtnLink>}
      </Empty>
    );
  } else if (!list.length) {
    body = (
      <Empty title="No workloads match" text={`Nothing on ${scopeName} matches this filter.`}>
        <Btn onClick={() => setParam("filter", null)}>Clear filter</Btn>
      </Empty>
    );
  } else if (view === "table") {
    const managedRows = list.filter((a) => a.kind === "deployment");
    const otherRows = list.filter((a) => a.kind !== "deployment");
    body = (
      <>
        {managedRows.length > 0 && (
          <>
            <SectHead title="Managed" count={`${managedRows.length} by Kowan`} />
            <AppsTable apps={managedRows} showHost={all} hostName={hostName} hostOnline={hostOnline} />
          </>
        )}
        {otherRows.length > 0 && (
          <>
            <SectHead title="Not managed" count={`${otherRows.length} discovered`} />
            <AppsTable apps={otherRows} showHost={all} hostName={hostName} hostOnline={hostOnline} />
          </>
        )}
      </>
    );
  } else {
    const needs = list.filter((a) => a.attention && !a.stale);
    const stale = list.filter((a) => a.stale);
    const fine = list.filter((a) => !a.attention);
    // Kowan deployments first; stacks and lone containers started elsewhere after.
    const managed = fine.filter((a) => a.kind === "deployment");
    const unmanaged = fine.filter((a) => a.kind !== "deployment");
    const card = (a: App) => <AppCard key={a.id} app={a} hostName={hostName(a.hostIds[0] ?? "")} />;
    body = (
      <>
        {(needs.length > 0 || offlineHosts.length > 0) && (
          <SectHead title="Needs you" count={`${needs.length + offlineHosts.length} item${needs.length + offlineHosts.length === 1 ? "" : "s"}`} />
        )}
        {offlineHosts.map((h) => (
          <div key={h.id} className="flex flex-wrap items-center gap-[12px] bg-card px-[16px] py-[12px] shadow-[inset_0_0_0_1px_var(--border)]">
            <WifiOff className="size-[16px] text-muted-foreground" strokeWidth={1.5} aria-hidden />
            <div className="min-w-0">
              <div className="font-semibold">{h.name || h.id} is offline</div>
              <div className="text-[13px] leading-[18px] text-muted-foreground">
                The agent is unreachable. Its containers are not listed; deployments on it show their configuration only.
              </div>
            </div>
            <div className="ml-auto flex gap-[8px]">
              <BtnLink to={`/hosts/${encodeURIComponent(h.id)}`} size="sm">Open host</BtnLink>
            </div>
          </div>
        ))}
        {needs.length > 0 && <Grid>{needs.map(card)}</Grid>}
        {managed.length > 0 && (
          <>
            <SectHead title="Managed" count={`${managed.length} by Kowan`} />
            <Grid>{managed.map(card)}</Grid>
          </>
        )}
        {unmanaged.length > 0 && (
          <>
            <SectHead title="Not managed" count={`${unmanaged.length} discovered`} />
            <Grid>{unmanaged.map(card)}</Grid>
          </>
        )}
        {stale.length > 0 && (
          <>
            <SectHead title="Last known" count={`${stale.length} on offline hosts`} />
            <Grid>{stale.map(card)}</Grid>
          </>
        )}
      </>
    );
  }

  return (
    <>
      {head}
      {toolbar}
      {body}
      {pruneModal}
    </>
  );
}

function Grid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] items-stretch gap-[12px]">{children}</div>;
}

function Empty({ title, text, children }: { title: string; text: string; children?: ReactNode }) {
  return (
    <section className="bg-card shadow-[inset_0_0_0_1px_var(--border)]">
      <div className="flex max-w-[680px] flex-col items-start gap-[10px] px-[16px] py-[32px]">
        <h3 className="m-0 text-[15px] leading-[20px] font-semibold">{title}</h3>
        <p className="m-0 text-[14px] leading-[20px] text-muted-foreground">{text}</p>
        {children && <div className="flex gap-[8px]">{children}</div>}
      </div>
    </section>
  );
}

function AppCard({ app: a, hostName }: { app: App; hostName: string }) {
  const ports = a.ports.filter((p) => p.public);
  const att = a.attention;
  // Old Swarm tasks are history: the squares show what runs now.
  const current = a.containers.filter((c) => !isReplaced(c));
  return (
    <Link
      to={appHref(a)}
      aria-label={`${a.name}, ${a.label}`}
      className="flex min-w-0 flex-col bg-card text-foreground no-underline shadow-[inset_0_0_0_1px_var(--border)] outline-none hover:bg-[var(--popover)] hover:shadow-[inset_0_0_0_1px_var(--input)] focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-offset-2 focus-visible:outline-[var(--signal)]"
    >
      <div className="flex items-center gap-[10px] px-[16px] pt-[14px]">
        {/* A name cut by the ellipsis shows whole on hover; a short one gets no tooltip. */}
        <span
          className="min-w-0 flex-1 truncate text-[16px] leading-[20px] font-[650]"
          onMouseEnter={(e) => {
            const el = e.currentTarget;
            el.title = el.scrollWidth > el.clientWidth ? a.name : "";
          }}
        >
          {a.name}
        </span>
        <Pill tone={a.tone}>{a.label}</Pill>
      </div>
      <div className="truncate px-[16px] pt-[4px] font-mono text-[12px] leading-[16px] font-medium tracking-[0.04em] text-muted-foreground uppercase">
        {kindShort(a)} · {hostName}
      </div>
      <div className="flex flex-wrap items-center gap-[4px] px-[16px] pt-[14px]">
        {a.stale ? (
          <span className="font-mono text-[12px] leading-[16px] font-medium text-muted-foreground">no data</span>
        ) : (
          <>
            <StateSquares tones={current.map((c) => c.tone)} titles={current.map((c) => `${c.name}: ${c.label}`)} />
            <span className="ml-[6px] font-mono text-[12px] leading-[16px] font-medium text-muted-foreground">
              {a.desired ? `${a.up}/${a.desired} up` : "no containers"}
            </span>
          </>
        )}
      </div>
      <div className="flex flex-wrap gap-[6px] px-[16px] pt-[12px]">
        {ports.length ? ports.slice(0, 6).map((p) => <Tag key={`${p.publicPort}/${p.protocol}`}>:{p.publicPort}</Tag>) : <Tag>internal only</Tag>}
        {ports.length > 6 && <Tag>+{ports.length - 6}</Tag>}
      </div>
      <div className="mt-auto flex items-center gap-[8px] p-[14px_16px]">
        <span className={LBL}>{a.deployment?.preview ? `Preview · ${a.deployment.preview.pullRequest ? `PR #${a.deployment.preview.pullRequest}` : a.deployment.preview.ref}` : a.managed ? "Managed" : "Discovered"}</span>
        <span className="ml-auto">
          {a.updates > 0 ? <Pill tone="a">Update · {a.updates}</Pill> : <span className={LBL}>Up to date</span>}
        </span>
      </div>
      {att && (
        <div
          className="mx-[1px] mb-[1px] flex items-center gap-[8px] px-[15px] py-[10px] text-[13px] leading-[18px] shadow-[inset_0_1px_0_var(--border)]"
          style={
            att.tone === "n"
              ? { background: "var(--muted)", color: "var(--muted-foreground)" }
              : {
                  background: `color-mix(in srgb, var(--tone-${att.tone === "r" ? "red" : "amber"}-solid) ${att.tone === "r" ? 7 : 10}%, var(--card))`,
                  color: `var(--tone-${att.tone === "r" ? "red" : "amber"}-text)`,
                }
          }
        >
          {att.tone === "n" ? (
            <WifiOff className="size-[16px] shrink-0" strokeWidth={1.5} aria-hidden />
          ) : att.tone === "r" ? (
            <CircleAlert className="size-[16px] shrink-0" strokeWidth={1.5} aria-hidden />
          ) : (
            <HeartPulse className="size-[16px] shrink-0" strokeWidth={1.5} aria-hidden />
          )}
          <span className="min-w-0 truncate">{att.text}</span>
        </div>
      )}
    </Link>
  );
}

const TH = "h-[36px] px-[12px] text-left font-mono text-[12px] leading-none font-semibold tracking-[0.06em] whitespace-nowrap text-muted-foreground uppercase bg-card first:pl-[16px] last:pr-[16px]";
const TD = "h-[40px] px-[12px] align-middle text-[13px] leading-[18px] whitespace-nowrap first:pl-[16px] last:pr-[16px]";

function AppsTable({ apps, showHost, hostName, hostOnline }: { apps: App[]; showHost: boolean; hostName: (id: string) => string; hostOnline: (id: string) => boolean }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setOpen((o) => ({ ...o, [key]: !o[key] }));
  const go = (to: string) => navigate(to);
  const onRowKey = (e: KeyboardEvent, to: string) => {
    if (e.key === "Enter" && e.target === e.currentTarget) go(to);
  };
  const containerRow = (c: AppContainer, visible: boolean, depth: 1 | 2) => {
    const to = `/containers/${c.container.id}?host=${encodeURIComponent(c.hostId)}`;
    return (
      <tr
        key={c.container.id}
        tabIndex={visible ? 0 : -1}
        aria-hidden={!visible || undefined}
        className={cn("cursor-pointer outline-none [&>td]:h-[36px] [&>td]:bg-muted hover:[&>td]:bg-[var(--popover)] focus-visible:[&>td]:bg-[var(--popover)]", !visible && "[visibility:collapse]")}
        onClick={() => go(to)}
        onKeyDown={(e) => onRowKey(e, to)}
      >
        {/* An old Swarm task is history: its name and image fade so the live ones stand out. */}
        <td className={cn(TD, "font-mono", depth === 1 ? "first:pl-[44px]" : "first:pl-[70px]", isReplaced(c) && "text-muted-foreground opacity-70")}>
          <span aria-hidden className="mr-[8px] inline-block align-[0px]"><StateSquares tones={[c.tone]} size={8} /></span>
          {c.name}
        </td>
        <td className={TD}><Pill tone={c.tone}>{c.label}</Pill></td>
        <td className={TD} />
        {showHost && <td className={TD} />}
        <td className={cn(TD, "font-mono text-muted-foreground", isReplaced(c) && "opacity-70")}>{c.container.image}</td>
        <td className={cn(TD, "font-mono text-muted-foreground")}>{publicPorts([c])}</td>
        <td className={TD}>{c.update?.updateAvailable ? <Pill tone="a">Update</Pill> : null}</td>
        <td className={cn(TD, "text-right")} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          {visible && <ContainerActionsMenu container={c.container} hostId={c.hostId} name={c.name} managed />}
        </td>
      </tr>
    );
  };
  return (
    <section className="min-w-0 overflow-x-auto bg-card shadow-[inset_0_0_0_1px_var(--border)]">
      <table className="w-full">
        <thead>
          <tr>
            <th className={TH}>Workload</th>
            <th className={TH}>State</th>
            <th className={TH}>Containers</th>
            {showHost && <th className={TH}>Host</th>}
            <th className={TH}>Kind</th>
            <th className={TH}>Ports</th>
            <th className={TH}>Updates</th>
            <th className={TH}>Needs you</th>
          </tr>
        </thead>
        <tbody>
          {apps.map((a) => {
            const isOpen = !!open[a.id];
            const host = a.hostIds[0] ?? "";
            return (
              <Fragment key={a.id}>
                <tr
                  tabIndex={0}
                  className="cursor-pointer outline-none hover:[&>td]:bg-[var(--popover)] focus-visible:[&>td]:bg-[var(--popover)]"
                  onClick={() => go(appHref(a))}
                  onKeyDown={(e) => onRowKey(e, appHref(a))}
                >
                  <td className={TD}>
                    <button
                      type="button"
                      aria-expanded={isOpen}
                      aria-label={`${isOpen ? "Hide" : "Show"} containers of ${a.name}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        toggle(a.id);
                      }}
                      className="mr-[6px] inline-flex size-[20px] items-center justify-center align-[-5px] text-muted-foreground outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-[var(--signal)]"
                    >
                      <ChevronRight className={cn("size-[16px] transition-transform duration-[120ms] motion-reduce:transition-none", isOpen && "rotate-90")} strokeWidth={1.5} aria-hidden />
                    </button>
                    <Link to={appHref(a)} className="font-semibold text-foreground no-underline hover:underline" onClick={(e) => e.stopPropagation()}>
                      {a.name}
                    </Link>
                  </td>
                  <td className={TD}><Pill tone={a.tone}>{a.label}</Pill></td>
                  <td className={TD}>
                    <span className="inline-flex flex-nowrap"><StateSquares tones={a.containers.filter((c) => !isReplaced(c)).map((c) => c.tone)} /></span>
                  </td>
                  {showHost && <td className={TD}><HostChip name={hostName(host)} online={hostOnline(host)} /></td>}
                  <td className={cn(TD, "font-mono")}>{kindShort(a)}</td>
                  <td className={cn(TD, "font-mono")}>{a.ports.filter((p) => p.public).map((p) => `:${p.publicPort}`).join(" ") || "—"}</td>
                  <td className={TD}>{a.updates ? <Pill tone="a">Update · {a.updates}</Pill> : <span className="text-muted-foreground">—</span>}</td>
                  <td
                    className={cn(TD, "whitespace-normal")}
                    style={a.attention ? { color: a.attention.tone === "r" ? "var(--tone-red-text)" : a.attention.tone === "a" ? "var(--tone-amber-text)" : "var(--muted-foreground)" } : undefined}
                  >
                    {a.attention?.text ?? ""}
                  </td>
                </tr>
                {/* Child rows stay in the DOM when closed: `visibility: collapse` hides
                    them but keeps them in the column-width pass, so expanding a
                    workload never resizes the table. A stack nests a second level:
                    its services, each opening onto its containers. */}
                {(() => {
                  const services = servicesOf(a);
                  if (!services) return liveFirst(a.containers).map((c) => containerRow(c, isOpen, 1));
                  return services.map((svc) => {
                    const key = `${a.id}::${svc.name}`;
                    const svcOpen = isOpen && !!open[key];
                    return (
                      <Fragment key={key}>
                        <tr
                          tabIndex={isOpen ? 0 : -1}
                          aria-hidden={!isOpen || undefined}
                          className={cn("cursor-pointer outline-none [&>td]:h-[36px] [&>td]:bg-muted hover:[&>td]:bg-[var(--popover)] focus-visible:[&>td]:bg-[var(--popover)]", !isOpen && "[visibility:collapse]")}
                          onClick={() => toggle(key)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && e.target === e.currentTarget) toggle(key);
                          }}
                        >
                          <td className={cn(TD, "font-mono first:pl-[30px]")}>
                            <button
                              type="button"
                              tabIndex={isOpen ? 0 : -1}
                              aria-expanded={svcOpen}
                              aria-label={`${svcOpen ? "Hide" : "Show"} containers of service ${svc.name}`}
                              onClick={(e) => {
                                e.stopPropagation();
                                toggle(key);
                              }}
                              className="mr-[6px] inline-flex size-[20px] items-center justify-center align-[-5px] text-muted-foreground outline-none focus-visible:outline-2 focus-visible:outline-solid focus-visible:outline-[var(--signal)]"
                            >
                              <ChevronRight className={cn("size-[16px] transition-transform duration-[120ms] motion-reduce:transition-none", svcOpen && "rotate-90")} strokeWidth={1.5} aria-hidden />
                            </button>
                            {svc.name}
                            <span className="ml-[10px] text-[12px] text-muted-foreground">{containerCount(svc.containers)}</span>
                          </td>
                          <td className={TD}><Pill tone={svc.tone}>{svc.label}</Pill></td>
                          <td className={TD}>
                            <span className="inline-flex flex-nowrap"><StateSquares tones={svc.containers.filter((c) => !isReplaced(c)).map((c) => c.tone)} /></span>
                          </td>
                          {showHost && <td className={TD} />}
                          <td className={cn(TD, "font-mono text-muted-foreground")}>{(svc.containers.find((c) => !isReplaced(c)) ?? svc.containers[0])?.container.image}</td>
                          <td className={cn(TD, "font-mono text-muted-foreground")}>{publicPorts(svc.containers)}</td>
                          <td className={TD}>{svc.containers.some((c) => c.update?.updateAvailable) ? <Pill tone="a">Update</Pill> : null}</td>
                          <td className={TD} />
                        </tr>
                        {svc.containers.map((c) => containerRow(c, svcOpen, 2))}
                      </Fragment>
                    );
                  });
                })()}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

/** ":80 :443": the distinct public ports of some containers, or "—". */
function publicPorts(cs: AppContainer[]): string {
  const ports = cs.flatMap((c) => (c.container.ports ?? []).filter((p) => p.publicPort).map((p) => p.publicPort));
  return [...new Set(ports)].sort((a, b) => a - b).map((p) => `:${p}`).join(" ") || "—";
}
