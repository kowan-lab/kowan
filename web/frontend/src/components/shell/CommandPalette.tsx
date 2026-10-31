import { useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { Command } from "cmdk";
import { Dialog as D } from "radix-ui";
import { CornerDownLeft, Search } from "lucide-react";
import { useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { listRunbooks } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { ALL_HOSTS, useHost } from "@/HostContext";
import { useHostName } from "@/lib/hostsQuery";
import { Icon, StatePill } from "@/ui/kit";
import { useTheme } from "@/ui/theme";
import { cn } from "@/lib/utils";
import { appHref, isReplaced, KIND_LABEL, useApps, type App } from "../../lib/apps";
import { useFeatures } from "../../lib/features";
import { Pill } from "../apps/ui";
import { visibleNav } from "./nav";
import { rank, type PaletteEntry, type PaletteKind } from "./paletteSearch";

const ITEM =
  "flex min-h-[40px] cursor-default items-center gap-[10px] rounded-md px-[10px] py-[6px] text-[13px] outline-none select-none data-[selected=true]:bg-accent";
const GROUP =
  "[&_[cmdk-group-heading]]:flex [&_[cmdk-group-heading]]:items-center [&_[cmdk-group-heading]]:gap-[6px] [&_[cmdk-group-heading]]:px-[10px] [&_[cmdk-group-heading]]:pt-[10px] [&_[cmdk-group-heading]]:pb-[4px] [&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-[11px] [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:tracking-[0.06em] [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group-heading]]:uppercase";
const ICON = "shrink-0 text-muted-foreground";

type Filter = "all" | PaletteKind;
const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "workload", label: "Workloads" },
  { id: "container", label: "Containers" },
  { id: "host", label: "Hosts" },
  { id: "page", label: "Pages" },
  { id: "action", label: "Actions" },
];
const GROUP_LABEL: Record<PaletteKind, string> = {
  workload: "Workloads",
  container: "Containers",
  host: "Hosts",
  page: "Pages",
  action: "Actions",
};
/** Rows per kind in the mixed view; the filter shows the rest. */
const PER_GROUP = 5;
/** Rows in a filtered view. */
const MAX = 50;
const ATTENTION_RANK: Record<string, number> = { r: 0, a: 1 };

interface Entry extends PaletteEntry {
  /** A lone container's own workload: the mixed view shows only one of the two. */
  twin?: string;
  /** Pages only: the nav section, used as the group heading when browsing. */
  section?: string;
  row: (q: string) => ReactNode;
  run: () => void;
}

interface Group {
  key: string;
  heading: ReactNode;
  entries: Entry[];
  /** Matches left out of a mixed view: a "Show all" row switches the filter. */
  more?: { kind: PaletteKind; total: number };
}

/**
 * ⌘K command palette: find a workload (compose stack, deployment or lone
 * container), a container, a host, a page or an action. Filters narrow it to
 * one kind (Tab cycles them); results are ranked, best match first. Data loads
 * only while the palette is open.
 */
export default function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-black/50 anim-fade" />
        <D.Content
          aria-describedby={undefined}
          className="fixed top-[12vh] left-1/2 z-50 w-[calc(100vw-2rem)] max-w-[680px] -translate-x-1/2 overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-[0_24px_64px_-16px_rgb(0_0_0/0.55)] outline-none anim-pop"
        >
          <D.Title className="sr-only">Command palette</D.Title>
          <PaletteBody close={() => onOpenChange(false)} />
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}

/** Mounted only while the palette is open, so its queries run only then. */
function PaletteBody({ close }: { close: () => void }) {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { hostId, setHostId } = useHost();
  const { theme, toggle } = useTheme();
  const features = useFeatures();
  const isAdmin = user?.role === "admin";
  const { apps, hosts, isLoading } = useApps(hostId);
  // Runbooks are actions: "Run <name>" opens its run dialog where it lives.
  const runbooks = useQuery({ queryKey: ["runbooks", ""], queryFn: () => listRunbooks(), staleTime: 60_000 });
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState("");

  const manyHosts = hostId === ALL_HOSTS || hosts.length > 1;
  const hostName = useHostName();

  const go = (to: string) => {
    close();
    navigate(to);
  };

  const entries = useMemo<Entry[]>(() => {
    const out: Entry[] = [];
    const list = apps ?? [];
    const sorted = [...list].sort(
      (a, b) => (a.attention ? ATTENTION_RANK[a.attention.tone] ?? 2 : 3) - (b.attention ? ATTENTION_RANK[b.attention.tone] ?? 2 : 3),
    );
    for (const a of sorted) {
      const hostsText = a.hostIds.map(hostName).join(", ");
      out.push({
        key: `workload:${a.id}`,
        kind: "workload",
        title: a.name,
        terms: [
          KIND_LABEL[a.kind],
          a.kind === "stack" || a.deployment?.type === "compose" ? "compose stack" : "",
          a.managed ? "managed deployment" : "not managed",
          a.project ?? "",
          hostsText,
          ...a.containers.map((c) => `${c.service} ${c.container.image}`),
        ],
        boost: a.attention && a.attention.tone !== "n" ? 2 : 0,
        row: (query) => <WorkloadRow app={a} query={query} host={manyHosts ? hostsText : ""} />,
        run: () => go(appHref(a)),
      });
    }
    for (const a of list) {
      for (const c of a.containers) {
        if (isReplaced(c)) continue;
        const inApp = a.kind !== "container" ? a.name : "";
        out.push({
          key: `container:${c.hostId}:${c.container.id}`,
          kind: "container",
          twin: a.kind === "container" ? `workload:${a.id}` : undefined,
          title: c.name,
          terms: [c.container.image, c.service, inApp, hostName(c.hostId), c.container.id.slice(0, 12)],
          boost: c.tone === "r" || c.tone === "a" ? 2 : 0,
          row: (query) => (
            <>
              <Icon name="docker" size="sm" className={ICON} />
              <Lines
                title={<Highlight text={c.name} query={query} />}
                meta={[shortImage(c.container.image), inApp && `in ${inApp}`, manyHosts && hostName(c.hostId)]}
              />
              <StatePill status={c.container.state} size="sm" />
            </>
          ),
          run: () => go(`/containers/${c.container.id}?host=${encodeURIComponent(c.hostId)}`),
        });
      }
    }
    out.push({
      key: "host:all",
      kind: "host",
      title: "All hosts",
      terms: ["switch host every"],
      row: (query) => (
        <>
          <Icon name="stacks-outline-rounded" size="sm" className={ICON} />
          <Lines title={<Highlight text="All hosts" query={query} />} meta={["Switch to every host at once"]} />
          {hostId === ALL_HOSTS && <Current />}
        </>
      ),
      run: () => {
        setHostId(ALL_HOSTS);
        go("/");
      },
    });
    for (const h of hosts) {
      out.push({
        key: `host:${h.id}`,
        kind: "host",
        title: h.name || h.id,
        terms: [h.id, "switch host", h.online ? "online" : "offline"],
        row: (query) => (
          <>
            <Icon name="dns-outline" size="sm" className={ICON} />
            <Lines title={<Highlight text={h.name || h.id} query={query} />} meta={["Switch host"]} />
            {hostId === h.id && <Current />}
            <StatePill status={h.online ? "online" : "offline"} size="sm" />
          </>
        ),
        run: () => {
          setHostId(h.id);
          go("/");
        },
      });
    }
    for (const s of visibleNav(isAdmin, features)) {
      for (const i of s.items) {
        out.push({
          key: `page:${i.to}`,
          kind: "page",
          title: i.label,
          terms: [i.keywords ?? "", s.label, i.sub ?? ""],
          section: s.label,
          row: (query) => (
            <>
              <Icon name={i.icon} size="sm" className={ICON} />
              <span className="min-w-0 flex-1 truncate"><Highlight text={i.label} query={query} /></span>
              {query && <span className="text-[12px] text-muted-foreground">{s.label}</span>}
            </>
          ),
          run: () => go(i.to),
        });
      }
    }
    const action = (key: string, title: string, icon: string, terms: string, run: () => void): Entry => ({
      key: `action:${key}`,
      kind: "action",
      title,
      terms: [terms],
      row: (query) => (
        <>
          <Icon name={icon} size="sm" className={ICON} />
          <span className="min-w-0 flex-1 truncate"><Highlight text={title} query={query} /></span>
        </>
      ),
      run,
    });
    const appName = new Map((apps ?? []).map((a) => [a.id, a.name]));
    for (const rb of runbooks.data ?? []) {
      const one = rb.workloads.length === 1 ? rb.workloads[0] : "";
      const where = one ? appName.get(one) ?? "" : `${rb.workloads.length} workloads`;
      out.push(action(`runbook:${rb.id}`, `Run ${rb.name}${where ? ` · ${where}` : ""}`, "terminal-rounded",
        `runbook run ${rb.description} ${where}`,
        () => go(one ? `${appHref(one, "runbooks")}&run=${encodeURIComponent(rb.id)}` : `/runbooks?run=${encodeURIComponent(rb.id)}`)));
    }
    out.push(
      action("deploy", "New deployment", "add-rounded", "deploy stack compose create", () => go("/deployments/new")),
      action("theme", `Switch to ${theme === "dark" ? "light" : "dark"} theme`, theme === "dark" ? "light-mode-outline-rounded" : "dark-mode-outline-rounded", "theme dark light toggle", () => {
        toggle();
        close();
      }),
      action("logout", "Log out", "logout-rounded", "sign out", () => {
        close();
        logout();
      }),
    );
    return out;
  }, [apps, hosts, hostId, isAdmin, features, theme, manyHosts, runbooks.data]);

  const query = q.trim();
  const ranked = useMemo(() => (query ? rank(entries, query) : entries.map((entry) => ({ entry, score: 0 }))), [entries, query]);
  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: ranked.length, workload: 0, container: 0, host: 0, page: 0, action: 0 };
    for (const r of ranked) c[r.entry.kind]++;
    return c;
  }, [ranked]);

  const groups = useMemo<Group[]>(() => {
    const of = (kind: PaletteKind) => ranked.filter((r) => r.entry.kind === kind).map((r) => r.entry);
    if (filter !== "all") {
      const list = of(filter).slice(0, MAX);
      if (filter === "page" && !query) return bySection(list);
      return [{ key: filter, heading: <Heading label={GROUP_LABEL[filter]} count={counts[filter]} />, entries: list }];
    }
    if (!query) {
      const workloads = of("workload");
      const attention = workloads.filter((e) => e.boost);
      const rest = workloads.filter((e) => !e.boost);
      const out: Group[] = [];
      const urgent = attention.slice(0, PER_GROUP);
      const others = rest.slice(0, PER_GROUP);
      if (urgent.length) out.push({ key: "attention", heading: <Heading label="Needs attention" count={attention.length} />, entries: urgent });
      if (others.length)
        out.push({
          key: "workload",
          heading: <Heading label={urgent.length ? "Other workloads" : "Workloads"} count={rest.length} />,
          entries: others,
          more: workloads.length > urgent.length + others.length ? { kind: "workload", total: workloads.length } : undefined,
        });
      out.push(...bySection(of("page")));
      const hostEntries = of("host");
      if (hostEntries.length > 1) out.push({ key: "host", heading: <Heading label="Switch host" />, entries: hostEntries });
      out.push({ key: "action", heading: <Heading label="Actions" />, entries: of("action") });
      return out;
    }
    // A query over everything: the best match on its own, then each kind
    // ordered by its own best match.
    const shown = new Set(ranked.map((r) => r.entry.key));
    const [top, ...others] = ranked.map((r) => r.entry).filter((e) => !e.twin || !shown.has(e.twin));
    if (!top) return [];
    const out: Group[] = [{ key: "top", heading: <Heading label="Top result" />, entries: [top] }];
    const order: PaletteKind[] = [];
    for (const e of others) if (!order.includes(e.kind)) order.push(e.kind);
    for (const kind of order) {
      const list = others.filter((e) => e.kind === kind);
      out.push({
        key: kind,
        heading: <Heading label={GROUP_LABEL[kind]} />,
        entries: list.slice(0, PER_GROUP),
        more: list.length > PER_GROUP ? { kind, total: counts[kind] } : undefined,
      });
    }
    return out;
  }, [ranked, filter, query, counts]);

  const keys = useMemo(
    () => groups.flatMap((g) => [...g.entries.map((e) => e.key), ...(g.more && g.more.total > g.entries.length ? [`more:${g.more.kind}`] : [])]),
    [groups],
  );
  // A new query or filter starts from the first row; so does a list that lost the selected row.
  useEffect(() => setSelected(keys[0] ?? ""), [query, filter]);
  useEffect(() => {
    if (!keys.includes(selected)) setSelected(keys[0] ?? "");
  }, [keys, selected]);

  const cycle = (dir: 1 | -1) => {
    const i = FILTERS.findIndex((f) => f.id === filter);
    setFilter(FILTERS[(i + dir + FILTERS.length) % FILTERS.length].id);
  };
  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === "Tab") {
      e.preventDefault();
      cycle(e.shiftKey ? -1 : 1);
    } else if (e.key === "Backspace" && !q && filter !== "all") {
      setFilter("all");
    }
  };

  const loading = isLoading && !apps;
  const scope = hostId === ALL_HOSTS ? "all hosts" : hostName(hostId);
  const filterLabel = FILTERS.find((f) => f.id === filter)!.label.toLowerCase();

  return (
    <Command loop shouldFilter={false} value={selected} onValueChange={setSelected} onKeyDown={onKeyDown}>
      <div className="flex items-center gap-[8px] border-b px-[12px]">
        <Search className="size-[16px] text-muted-foreground" aria-hidden />
        <Command.Input
          value={q}
          onValueChange={setQ}
          placeholder={filter === "all" ? "Search workloads, stacks, containers, hosts, pages…" : `Search ${filterLabel}…`}
          // The global :focus-visible ring would frame the field; the caret already shows focus.
          style={{ outline: "none" }}
          className="h-[48px] w-full bg-transparent text-[14px] placeholder:text-muted-foreground"
        />
        <kbd className="rounded border px-[4px] font-mono text-[12px] text-muted-foreground">esc</kbd>
      </div>
      <div role="tablist" aria-label="Search in" className="flex gap-[4px] overflow-x-auto border-b px-[8px] py-[6px] [scrollbar-width:none]">
        {FILTERS.map((f) => {
          const active = f.id === filter;
          return (
            <button
              key={f.id}
              type="button"
              role="tab"
              aria-selected={active}
              tabIndex={-1}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => setFilter(f.id)}
              className={cn(
                "inline-flex h-[28px] shrink-0 items-center gap-[6px] rounded-md px-[8px] text-[12px] font-medium whitespace-nowrap transition-colors",
                active ? "bg-foreground text-background" : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              {f.label}
              {f.id !== "all" && (
                <span className={cn("font-mono text-[11px] tabular-nums", active ? "opacity-70" : "opacity-60")}>
                  {(f.id === "workload" || f.id === "container") && loading ? "…" : counts[f.id]}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <Command.List className="max-h-[56vh] overflow-y-auto p-[4px]">
        <Command.Empty className="px-5 py-12 text-center text-sm text-muted-foreground">
          {loading && (filter === "workload" || filter === "container") ? "Loading…" : query ? <>No {filter === "all" ? "matches" : filterLabel} for “{query}”.</> : <>No {filterLabel}.</>}
          {filter !== "all" && query && <div className="mt-[4px] text-[12px]">Press Backspace on an empty search or pick All to look everywhere.</div>}
        </Command.Empty>
        {groups.map((g) => (
          <Command.Group key={g.key} heading={g.heading} className={GROUP}>
            {g.entries.map((e) => (
              <Command.Item key={e.key} value={e.key} onSelect={e.run} className={ITEM}>
                {e.row(query)}
                <CornerDownLeft className="size-[12px] shrink-0 text-muted-foreground opacity-0 [[data-selected=true]_&]:opacity-100" aria-hidden />
              </Command.Item>
            ))}
            {g.more && g.more.total > g.entries.length && (
              <Command.Item value={`more:${g.more.kind}`} onSelect={() => setFilter(g.more!.kind)} className={cn(ITEM, "min-h-[32px] text-muted-foreground")}>
                <span className="flex-1 pl-[26px] text-[12px]">
                  Show all {g.more.total} {GROUP_LABEL[g.more.kind].toLowerCase()}
                </span>
                <kbd className="rounded border px-[4px] font-mono text-[11px]">tab</kbd>
              </Command.Item>
            )}
          </Command.Group>
        ))}
      </Command.List>
      <div className="flex items-center gap-[12px] border-t px-[12px] py-[8px] text-[12px] text-muted-foreground">
        <span className="min-w-0 flex-1 truncate">{loading ? "Loading workloads…" : `Workloads and containers on ${scope}`}</span>
        <span className="hidden items-center gap-[12px] sm:flex">
          <Hint k="↑↓">move</Hint>
          <Hint k="↵">open</Hint>
          <Hint k="tab">filter</Hint>
        </span>
      </div>
    </Command>
  );
}

/** An image given by digest reads as its short id. */
function shortImage(image: string): string {
  return image.startsWith("sha256:") ? image.slice(7, 19) : image;
}

function bySection(list: Entry[]): Group[] {
  const out: Group[] = [];
  for (const e of list) {
    const label = e.section ?? "Pages";
    const g = out.find((x) => x.key === `page:${label}`);
    if (g) g.entries.push(e);
    else out.push({ key: `page:${label}`, heading: <Heading label={label} />, entries: [e] });
  }
  return out;
}

function Heading({ label, count }: { label: string; count?: number }) {
  return (
    <>
      <span>{label}</span>
      {count !== undefined && <span className="font-normal tabular-nums opacity-70">{count}</span>}
    </>
  );
}

function Hint({ k, children }: { k: string; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-[4px]">
      <kbd className="rounded border px-[4px] font-mono text-[11px]">{k}</kbd>
      {children}
    </span>
  );
}

function Current() {
  return <span className="font-mono text-[11px] text-muted-foreground uppercase">current</span>;
}

/** Name over a muted meta line; empty meta parts are dropped. */
function Lines({ title, meta }: { title: ReactNode; meta: (string | false | undefined | null)[] }) {
  const parts = meta.filter(Boolean) as string[];
  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <span className="truncate font-medium">{title}</span>
      {parts.length > 0 && <span className="truncate text-[12px] text-muted-foreground">{parts.join(" · ")}</span>}
    </span>
  );
}

function WorkloadRow({ app, query, host }: { app: App; query: string; host: string }) {
  const kind = app.kind === "deployment" ? (app.deployment?.type === "compose" ? "Compose deployment" : "Deployment") : KIND_LABEL[app.kind];
  const icon = app.kind === "container" || app.deployment?.type === "container" ? "docker" : "stacks-outline-rounded";
  const attention = app.attention && app.attention.tone !== "n" ? app.attention.text : "";
  return (
    <>
      <Icon name={icon} size="sm" className={ICON} />
      <Lines
        title={<Highlight text={app.name} query={query} />}
        meta={[attention, kind, host]}
      />
      <Pill tone={app.tone}>{app.label}</Pill>
    </>
  );
}

/** Bold the first stretch of `text` a query word matches. */
function Highlight({ text, query }: { text: string; query: string }) {
  const lower = text.toLowerCase();
  let at = -1;
  let len = 0;
  for (const token of query.toLowerCase().split(/\s+/).filter(Boolean)) {
    const i = lower.indexOf(token);
    if (i >= 0 && (at < 0 || token.length > len)) {
      at = i;
      len = token.length;
    }
  }
  if (at < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, at)}
      <span className="font-semibold text-foreground underline decoration-[var(--signal)] decoration-2 underline-offset-[3px]">{text.slice(at, at + len)}</span>
      {text.slice(at + len)}
    </>
  );
}
