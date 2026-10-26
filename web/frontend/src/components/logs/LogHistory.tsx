// Logs → History: search the retained logs of every workload that keeps them,
// across hosts and time ranges, newest first, with older pages on demand. The
// side panel lists the workloads that keep logs (a filter) and lets operators
// change or stop their retention.

import { useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, MoreHorizontal } from "lucide-react";
import { Button, Card, Menu, Select, Skeleton, Text, TextInput } from "@/ui/kit";
import { deleteLogRetention, listDeployments, listHosts, listLogRetention, searchLogArchive, setLogRetention } from "../../api/client";
import { appHref } from "../../lib/apps";
import type { ArchivedLogLine, LogRetention } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage, formatDate } from "../../utils";
import ConfirmModal from "../ConfirmModal";
import EmptyState from "../EmptyState";
import { useCanOperate } from "../deployment/shared";
import { RETENTION_DAYS, daysLabel, workloadName } from "./retention";

const RANGES = [
  { value: "1", label: "Last hour" },
  { value: "6", label: "Last 6 hours" },
  { value: "24", label: "Last 24 hours" },
  { value: "168", label: "Last 7 days" },
  { value: "720", label: "Last 30 days" },
  { value: "0", label: "Everything kept" },
];

const PAGE = 200;

/** Words to highlight: the positive terms of the query (quoted phrases whole). */
export function highlightTerms(q: string): string[] {
  return (q.match(/-?"[^"]*"|\S+/g) ?? [])
    .filter((t) => !(t.startsWith("-") && t.length > 1))
    .map((t) => t.replace(/^"|"$/g, ""))
    .filter(Boolean);
}

export function Highlight({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length) return <>{text}</>;
  const esc = terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const parts = text.split(new RegExp(`(${esc.join("|")})`, "gi"));
  const lower = terms.map((t) => t.toLowerCase());
  return (
    <>
      {parts.map((p, i) =>
        lower.includes(p.toLowerCase()) ? (
          <mark key={i} className="bg-[var(--tone-amber-soft)] text-foreground">{p}</mark>
        ) : (
          p
        ),
      )}
    </>
  );
}

export default function LogHistory({ initialWorkload }: { initialWorkload?: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const canOperate = useCanOperate();
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [hours, setHours] = useState("24");
  const [picked, setPicked] = useState<string[]>(initialWorkload ? [initialWorkload] : []);
  const [stopping, setStopping] = useState<LogRetention | null>(null);

  const retQ = useQuery({ queryKey: ["log-retention"], queryFn: listLogRetention, refetchInterval: 15_000 });
  const depsQ = useQuery({ queryKey: ["deployments"], queryFn: listDeployments });
  const hostsQ = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const name = (id: string) => workloadName(id, depsQ.data, hostsQ.data);
  const multiHost = (hostsQ.data?.length ?? 0) > 1;

  const search = useInfiniteQuery({
    queryKey: ["log-archive", q, hours, picked],
    queryFn: ({ pageParam }) =>
      searchLogArchive({ q, hours: Number(hours), workload: picked, before: pageParam, limit: PAGE }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next ?? undefined,
    enabled: (retQ.data?.length ?? 0) > 0,
  });
  const lines: ArchivedLogLine[] = useMemo(() => search.data?.pages.flatMap((p) => p.items) ?? [], [search.data]);
  const terms = useMemo(() => highlightTerms(q), [q]);

  const change = useMutation({
    mutationFn: ({ workload, days }: { workload: string; days: number }) => setLogRetention(workload, days),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["log-retention"] });
      notify({ color: "green", title: "Retention changed", message: `${name(r.workload).name} keeps logs for ${daysLabel(r.days)}.` });
    },
    onError: (e) => notify({ color: "red", title: "Not saved", message: errorMessage(e, "") }),
  });
  const stop = useMutation({
    mutationFn: (workload: string) => deleteLogRetention(workload),
    onSuccess: (res, workload) => {
      setStopping(null);
      setPicked((p) => p.filter((w) => w !== workload));
      qc.invalidateQueries({ queryKey: ["log-retention"] });
      qc.invalidateQueries({ queryKey: ["log-archive"] });
      notify({ color: "green", title: "Logs no longer kept", message: `${res.removed.toLocaleString()} stored lines deleted.` });
    },
    onError: (e) => notify({ color: "red", title: "Not stopped", message: errorMessage(e, "") }),
  });

  const toggle = (w: string) => setPicked((p) => (p.includes(w) ? p.filter((x) => x !== w) : [...p, w]));

  if (retQ.isLoading) return <Skeleton height={320} />;
  if (!retQ.data?.length) {
    return (
      <Card variant="raised">
        <Card.Body>
          <EmptyState
            title="No workload keeps its logs yet"
            description="Docker only holds a container's recent output, and loses it when the container is replaced. Open a workload, go to its Logs tab and turn on Keep logs: its lines are then stored here, searchable across hosts and restarts."
            action={<Link className="text-[13px] font-semibold text-[var(--link)] hover:underline" to="/workloads">Go to workloads</Link>}
          />
        </Card.Body>
      </Card>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-6 lg:flex-row">
      <Card variant="raised" className="lg:w-[300px] lg:shrink-0">
        <Card.Body>
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <span className="text-[13px] font-semibold">Workloads keeping logs</span>
              <button
                type="button"
                onClick={() => setPicked([])}
                className={"text-[13px] font-semibold text-[var(--link)] hover:underline " + (picked.length ? "" : "invisible")}
                tabIndex={picked.length ? 0 : -1}
                aria-hidden={!picked.length}
              >
                All
              </button>
            </div>
            <ul className="m-0 flex list-none flex-col p-0" aria-label="Workloads keeping logs">
              {retQ.data.map((r) => {
                const n = name(r.workload);
                const on = picked.includes(r.workload);
                return (
                  <li key={r.workload} className={"flex items-center gap-3 px-2 py-2 hover:bg-accent " + (on ? "bg-[var(--tone-primary-soft)]" : "")}>
                    <button
                      type="button"
                      role="checkbox"
                      aria-checked={on}
                      aria-label={`Filter by ${n.name}`}
                      onClick={() => toggle(r.workload)}
                      className="flex min-w-0 flex-1 items-center gap-4 text-left outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-signal"
                    >
                      <span aria-hidden className={"flex size-8 shrink-0 items-center justify-center border " + (on ? "border-foreground bg-foreground text-background" : "border-[var(--input)] bg-[var(--popover)]")}>
                        {on && <Check className="size-[12px]" strokeWidth={2.5} />}
                      </span>
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate text-[13px] leading-[18px]">{n.name}</span>
                        <span className="truncate text-[12px] leading-[16px] text-muted-foreground">
                          {n.where} · {daysLabel(r.days)} · {r.lines.toLocaleString()} lines
                        </span>
                      </span>
                    </button>
                    {canOperate && (
                      <Menu>
                        <Menu.Trigger>
                          <button type="button" aria-label={`Retention of ${n.name}`} className="flex size-10 items-center justify-center text-muted-foreground hover:text-foreground">
                            <MoreHorizontal className="size-[16px]" />
                          </button>
                        </Menu.Trigger>
                        <Menu.Content>
                          <Menu.Label>Keep for</Menu.Label>
                          {RETENTION_DAYS.map((d) => (
                            <Menu.Item
                              key={d.value}
                              trailing={Number(d.value) === r.days ? "current" : undefined}
                              onSelect={() => change.mutate({ workload: r.workload, days: Number(d.value) })}
                            >
                              {d.label}
                            </Menu.Item>
                          ))}
                          <Menu.Separator />
                          <Menu.Item onSelect={() => navigate(appHref(r.workload, "logs"))}>Open workload</Menu.Item>
                          <Menu.Item color="danger" onSelect={() => setStopping(r)}>Stop keeping…</Menu.Item>
                        </Menu.Content>
                      </Menu>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        </Card.Body>
      </Card>

      <Card variant="raised" className="flex min-h-0 min-w-0 flex-1 flex-col">
        <Card.Body className="flex min-h-0 flex-1 flex-col gap-4">
          <form
            className="flex flex-wrap items-end gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              setQ(draft.trim());
            }}
          >
            <TextInput
              size="sm"
              className="min-w-[220px] flex-1"
              aria-label="Search logs"
              placeholder='Words, "a phrase", -exclude'
              iconLeft="search-rounded"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
            />
            <Select size="sm" width={170} aria-label="Time range" options={RANGES} value={hours} onChange={(v) => v && setHours(v)} />
            <Button size="sm" type="submit">Search</Button>
          </form>
          <Results
            loading={search.isLoading}
            error={search.isError ? errorMessage(search.error, "Search failed") : ""}
            lines={lines}
            terms={terms}
            multiHost={multiHost}
            name={(id) => name(id).name}
            footer={
              search.hasNextPage ? (
                <Button size="sm" variant="outline" loading={search.isFetchingNextPage} onClick={() => search.fetchNextPage()}>
                  Load older lines
                </Button>
              ) : lines.length ? (
                <Text size="sm" color="muted">Start of the kept logs for this search.</Text>
              ) : null
            }
          />
        </Card.Body>
      </Card>

      <ConfirmModal
        opened={!!stopping}
        onClose={() => setStopping(null)}
        onConfirm={() => stopping && stop.mutate(stopping.workload)}
        loading={stop.isPending}
        title="Stop keeping logs?"
        message={stopping ? `${name(stopping.workload).name} stops keeping logs and its ${stopping.lines.toLocaleString()} stored lines are deleted.` : ""}
        confirmLabel="Stop and delete"
        confirmColor="red"
      />
    </div>
  );
}

function Results({
  loading, error, lines, terms, multiHost, name, footer,
}: {
  loading: boolean;
  error: string;
  lines: ArchivedLogLine[];
  terms: string[];
  multiHost: boolean;
  name: (workload: string) => string;
  footer: ReactNode;
}) {
  if (loading) return <Skeleton height={240} />;
  if (error) return <Text size="sm" color="danger">{error}</Text>;
  if (!lines.length) {
    return (
      <EmptyState
        title="No matching lines"
        description="Try a wider time range, fewer words, or pick no workload to search them all."
      />
    );
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ol className="m-0 min-h-0 flex-1 list-none overflow-auto border bg-[var(--surface-sunken,var(--muted))] p-0 font-mono text-[12px] leading-[18px]" aria-label="Log lines">
        {lines.map((l) => (
          <li key={l.id} className="flex gap-4 border-b border-[var(--border)]/40 px-4 py-[3px] hover:bg-accent/40">
            <time dateTime={l.ts} className="shrink-0 text-muted-foreground" title={l.ts}>{formatDate(l.ts)}</time>
            <span className="w-[140px] shrink-0 truncate text-[var(--link)]" title={`${l.container} (${name(l.workload)})`}>
              {l.container}
              {multiHost && <span className="text-muted-foreground">@{l.hostName}</span>}
            </span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-all"><Highlight text={l.line} terms={terms} /></span>
          </li>
        ))}
      </ol>
      <div className="flex justify-center pt-3">{footer}</div>
    </div>
  );
}
