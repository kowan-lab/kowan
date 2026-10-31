import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Checkbox, NumberInput, Popup, Select, TextArea, TextInput } from "@/ui/kit";
import {
  createRunbook,
  deleteRunbook,
  listRunbookRuns,
  listRunbooks,
  runbookRunWebSocketURL,
  runbookSuggestions,
  runRunbook,
  updateRunbook,
} from "../../api/client";
import { ALL_HOSTS } from "../../HostContext";
import { isReplaced, useApps, type App, type AppTone } from "../../lib/apps";
import { serviceOf } from "../../lib/containerOrigin";
import { notify } from "../../ui/toast";
import { errorMessage, timeAgo } from "../../utils";
import type { Runbook, RunbookEvent, RunbookInput, RunbookParam, RunbookRecord, RunbookRun, RunbookRunStatus, RunbookStep } from "../../types";
import ConfirmModal from "../ConfirmModal";
import { Band, Btn, LBL, Pill, Tag } from "../apps/ui";

/**
 * Runbooks: named, approved actions on workloads (kowan/runbooks). A runbook
 * is a list of steps, each a shell command on a workload's service, run in a
 * running container (exec) or in a throwaway copy of it (run). Parameters
 * reach the command as environment variables. Runs are followed live.
 */

const STATUS: Record<RunbookRunStatus, { tone: AppTone; label: string }> = {
  running: { tone: "a", label: "Running" },
  succeeded: { tone: "g", label: "Succeeded" },
  warning: { tone: "a", label: "Succeeded, with errors" },
  failed: { tone: "r", label: "Failed" },
};

const MONO = "font-mono text-[13px] leading-[20px]";

export function RunStatus({ status }: { status: RunbookRunStatus }) {
  const s = STATUS[status] ?? STATUS.failed;
  return <Pill tone={s.tone} signal={status === "running"}>{s.label}</Pill>;
}

/** "web in shop", "shop" (a whole stack), the container for a lone one. */
function whereLabel(step: Pick<RunbookStep, "workload" | "service">, names: Map<string, string>): string {
  if (step.workload.startsWith("group~")) return `${step.service} · every host in ${step.workload.slice(6)}`;
  const wl = names.get(step.workload) ?? step.workload.split("~").pop() ?? step.workload;
  return step.service ? `${step.service} · ${wl}` : wl;
}

function useWorkloadNames(): { apps: App[]; names: Map<string, string>; groups: string[] } {
  const { apps, hosts } = useApps(ALL_HOSTS);
  return useMemo(() => ({
    apps: apps ?? [],
    names: new Map((apps ?? []).map((a) => [a.id, a.name])),
    groups: [...new Set(hosts.flatMap((h) => h.tags ?? []))].sort(),
  }), [apps, hosts]);
}

/** The services of a workload, as the server names them ("" for a lone container). */
function servicesOf(app: App | undefined): string[] {
  if (!app) return [];
  const out = new Set<string>();
  for (const c of app.containers) if (!isReplaced(c)) out.add(serviceOf(c.container.labels));
  return [...out].sort();
}

// ── Following a run ──────────────────────────────────────────────────────────

/** A run as it happens (or as it went): each step with its output. */
export function RunView({ runId, names, onDone }: { runId: string; names: Map<string, string>; onDone?: (run: RunbookRun) => void }) {
  const [records, setRecords] = useState<RunbookRecord[]>([]);
  const [run, setRun] = useState<RunbookRun | null>(null);
  const [lost, setLost] = useState(false);
  const done = useRef(onDone);
  done.current = onDone;

  useEffect(() => {
    setRecords([]);
    setRun(null);
    setLost(false);
    const ws = new WebSocket(runbookRunWebSocketURL(runId));
    let finished = false;
    ws.onmessage = (m) => {
      let ev: RunbookEvent;
      try {
        ev = JSON.parse(m.data as string) as RunbookEvent;
      } catch {
        return;
      }
      if (ev.type === "step") {
        setRecords((rs) => {
          const next = [...rs];
          next[ev.record] = { ...ev.data, output: rs[ev.record]?.output ?? "" };
          return next;
        });
      } else if (ev.type === "output") {
        setRecords((rs) => {
          const next = [...rs];
          const r = next[ev.record];
          if (r) next[ev.record] = { ...r, output: (r.output ?? "") + ev.text };
          return next;
        });
      } else if (ev.type === "done") {
        finished = true;
        setRun(ev.run);
        done.current?.(ev.run);
      }
    };
    ws.onclose = () => {
      if (!finished) setLost(true);
    };
    return () => ws.close();
  }, [runId]);

  return (
    <div className="flex min-w-0 flex-col gap-[12px]" aria-label="Run output">
      <div className="flex items-center gap-[8px]">
        {run ? <RunStatus status={run.status} /> : <RunStatus status="running" />}
        {lost && <span className={LBL}>Connection lost: the run goes on; open it again from History.</span>}
      </div>
      {records.map((r, i) => (
        <div key={i} className="flex min-w-0 flex-col gap-[6px]">
          <div className="flex flex-wrap items-center gap-[8px]">
            <span className="text-[14px] leading-[20px] font-semibold">
              {r.step + 1}. {r.title || whereLabel(r, names)}
            </span>
            {r.container && <Tag>{r.mode === "run" ? `copy of ${r.container}` : r.container}</Tag>}
            <RunStatus status={r.status} />
            {r.exitCode != null && r.status !== "running" && <span className={LBL}>exit {r.exitCode}</span>}
          </div>
          {r.error && <span className="text-[13px] leading-[18px] text-[var(--tone-red-text)]">{r.error}</span>}
          {(r.output || r.status === "running") && (
            <pre className={`m-0 max-h-[320px] overflow-auto bg-[var(--muted)] p-[10px_12px] whitespace-pre-wrap break-words ${MONO}`}>
              {r.output || "…"}
            </pre>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Running ──────────────────────────────────────────────────────────────────

function ParamField({ p, value, onChange }: { p: RunbookParam; value: string; onChange: (v: string) => void }) {
  const label = p.label || p.name;
  const description = `$${p.name}${p.required ? " · required" : ""}`;
  if (p.type === "choice") {
    return <Select label={label} description={description} value={value} onChange={(v) => onChange(v ?? "")} options={p.choices.map((c) => ({ value: c, label: c }))} />;
  }
  return <TextInput label={label} description={description} value={value} inputMode={p.type === "number" ? "decimal" : undefined} onChange={(e) => onChange(e.target.value)} />;
}

/** Fill the parameters, confirm if asked, run, and follow the run. */
export function RunDialog({ runbook, names, onClose, runId: viewRun }: { runbook: Runbook; names: Map<string, string>; onClose: () => void; runId?: string }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(runbook.params.map((p) => [p.name, p.default])));
  const [confirmed, setConfirmed] = useState(false);
  const [runId, setRunId] = useState<string | null>(viewRun ?? null);
  const [finished, setFinished] = useState(!!viewRun);
  const start = useMutation({
    mutationFn: () => runRunbook(runbook.id, values, confirmed),
    onSuccess: (run) => setRunId(run.id),
    onError: (err) => notify({ color: "red", title: `Could not run ${runbook.name}`, message: errorMessage(err, "Run failed") }),
  });
  const missing = runbook.params.filter((p) => p.required && !values[p.name]);
  const busy = !!runId && !finished;
  return (
    <Popup open onOpenChange={(o) => !o && !busy && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{runbook.name}</Popup.Title>
          {!busy && <Popup.Close />}
        </Popup.Header>
        <Popup.Body>
          <div className="flex flex-col gap-[14px]">
            {runbook.description && <p className="m-0 text-[14px] leading-[20px] text-muted-foreground">{runbook.description}</p>}
            {!runId && (
              <>
                <ol className="m-0 flex list-none flex-col gap-[6px] p-0" aria-label="Steps">
                  {runbook.steps.map((s, i) => (
                    <li key={i} className="flex min-w-0 flex-col gap-[2px]">
                      <span className="text-[13px] leading-[18px] font-semibold">
                        {i + 1}. {s.title || whereLabel(s, names)}{" "}
                        <span className="font-normal text-muted-foreground">
                          · {s.mode === "run" ? "throwaway container" : s.replicas === "all" ? "every replica" : "one replica"}
                          {s.continueOnError ? " · may fail" : ""}
                        </span>
                      </span>
                      <code className={`truncate text-muted-foreground ${MONO}`} title={s.command}>{s.command}</code>
                    </li>
                  ))}
                </ol>
                {runbook.params.map((p) => (
                  <ParamField key={p.name} p={p} value={values[p.name] ?? ""} onChange={(v) => setValues((m) => ({ ...m, [p.name]: v }))} />
                ))}
                {runbook.confirm && (
                  <Callout variant="light" color="yellow" title="This runbook changes things">
                    <Checkbox label="I know what it does: run it" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
                  </Callout>
                )}
              </>
            )}
            {runId && (
              <RunView
                runId={runId}
                names={names}
                onDone={() => {
                  setFinished(true);
                  void qc.invalidateQueries({ queryKey: ["runbooks"] });
                  void qc.invalidateQueries({ queryKey: ["runbook-runs", runbook.id] });
                }}
              />
            )}
          </div>
        </Popup.Body>
        <Popup.Footer>
          {!runId ? (
            <>
              <Button variant="clear" onClick={onClose}>Cancel</Button>
              <Button iconLeft="play-arrow-rounded" loading={start.isPending} disabled={missing.length > 0 || (runbook.confirm && !confirmed)} onClick={() => start.mutate()}>
                Run
              </Button>
            </>
          ) : (
            <Button variant="clear" disabled={busy} onClick={onClose}>{busy ? "Running…" : "Close"}</Button>
          )}
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

// ── Editing ──────────────────────────────────────────────────────────────────

const newStep = (workload: string, service: string): RunbookStep => ({
  title: "", workload, service, mode: "exec", replicas: "one", command: "", continueOnError: false, timeoutSeconds: 600,
});
const newParam = (): RunbookParam => ({ name: "", label: "", type: "text", choices: [], default: "", required: false });

/** Create or edit a runbook: what it asks, then its steps in order. */
export function RunbookEditor({ initial, workload, onClose }: { initial?: Runbook | RunbookInput; workload?: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { apps, names, groups } = useWorkloadNames();
  const id = initial && "id" in initial ? initial.id : null;
  const firstApp = apps.find((a) => a.id === workload);
  const [form, setForm] = useState<RunbookInput>(() => ({
    name: initial?.name ?? "",
    description: initial?.description ?? "",
    confirm: initial?.confirm ?? false,
    params: initial?.params?.map((p) => ({ ...p })) ?? [],
    steps: initial?.steps?.map((s) => ({ ...s })) ?? [newStep(workload ?? "", servicesOf(firstApp)[0] ?? "")],
  }));
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => (id ? updateRunbook(id, form) : createRunbook(form)),
    onSuccess: (rb) => {
      notify({ color: "green", title: id ? "Runbook saved" : "Runbook created", message: rb.name });
      void qc.invalidateQueries({ queryKey: ["runbooks"] });
      onClose();
    },
    onError: (err) => setError(errorMessage(err, "Could not save the runbook")),
  });
  const setStep = (i: number, patch: Partial<RunbookStep>) => setForm((f) => ({ ...f, steps: f.steps.map((s, j) => (j === i ? { ...s, ...patch } : s)) }));
  const setParam = (i: number, patch: Partial<RunbookParam>) => setForm((f) => ({ ...f, params: f.params.map((p, j) => (j === i ? { ...p, ...patch } : p)) }));
  const move = (i: number, d: number) =>
    setForm((f) => {
      const steps = [...f.steps];
      const [s] = steps.splice(i, 1);
      steps.splice(i + d, 0, s);
      return { ...f, steps };
    });
  // Every workload is offered, the one this was opened from first.
  const workloadOptions = [...apps].sort((a, b) => Number(b.id === workload) - Number(a.id === workload) || a.name.localeCompare(b.name))
    .map((a) => ({ value: a.id, label: a.name }));
  // A host group: the same service on every host in it.
  workloadOptions.push(...groups.map((g) => ({ value: `group~${g}`, label: `Group · ${g}` })));
  if (workload && !names.has(workload)) workloadOptions.unshift({ value: workload, label: workload.split("~").pop() ?? workload });

  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{id ? `Edit ${initial?.name}` : "New runbook"}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <form
            id="runbook-form"
            className="flex flex-col gap-[16px]"
            onSubmit={(e) => {
              e.preventDefault();
              setError(null);
              save.mutate();
            }}
          >
            <TextInput label="Name" required value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="Migrate the database" />
            <TextInput label="Description" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} placeholder="What it does, when to use it" />
            <Checkbox label="Ask for confirmation before running" description="For runbooks that delete or change data." checked={form.confirm} onChange={(e) => setForm((f) => ({ ...f, confirm: e.target.checked }))} />

            <section className="flex flex-col gap-[10px]" aria-label="Parameters">
              <div className="flex items-center gap-[8px]">
                <h3 className="m-0 text-[14px] leading-[20px] font-semibold">Parameters</h3>
                <span className={LBL}>Asked when it runs; the command reads them as $NAME.</span>
                <Btn size="sm" className="ml-auto" type="button" onClick={() => setForm((f) => ({ ...f, params: [...f.params, newParam()] }))}>Add parameter</Btn>
              </div>
              {form.params.map((p, i) => (
                <div key={i} className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] items-end gap-[10px] bg-[var(--muted)] p-[10px]">
                  <TextInput label="Name" value={p.name} placeholder="TABLE" onChange={(e) => setParam(i, { name: e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_") })} />
                  <TextInput label="Label" value={p.label} placeholder="Table to vacuum" onChange={(e) => setParam(i, { label: e.target.value })} />
                  <Select label="Type" value={p.type} onChange={(v) => setParam(i, { type: (v || "text") as RunbookParam["type"] })}
                    options={[{ value: "text", label: "Text" }, { value: "number", label: "Number" }, { value: "choice", label: "Choice" }]} />
                  {p.type === "choice" && (
                    <TextInput label="Choices" value={p.choices.join(", ")} placeholder="up, down" onChange={(e) => setParam(i, { choices: e.target.value.split(",").map((c) => c.trim()) })} />
                  )}
                  <TextInput label="Default" value={p.default} onChange={(e) => setParam(i, { default: e.target.value })} />
                  <div className="flex items-center gap-[10px] pb-[8px]">
                    <Checkbox label="Required" checked={p.required} onChange={(e) => setParam(i, { required: e.target.checked })} />
                    <Btn size="sm" type="button" danger aria-label={`Remove parameter ${p.name || i + 1}`} onClick={() => setForm((f) => ({ ...f, params: f.params.filter((_, j) => j !== i) }))}>Remove</Btn>
                  </div>
                </div>
              ))}
            </section>

            <section className="flex flex-col gap-[10px]" aria-label="Steps">
              <div className="flex items-center gap-[8px]">
                <h3 className="m-0 text-[14px] leading-[20px] font-semibold">Steps</h3>
                <span className={LBL}>In order; a failing step stops the run unless it may fail.</span>
                <Btn size="sm" className="ml-auto" type="button" onClick={() => setForm((f) => {
                  const last = f.steps[f.steps.length - 1];
                  return { ...f, steps: [...f.steps, newStep(last?.workload ?? workload ?? "", last?.service ?? "")] };
                })}>Add step</Btn>
              </div>
              {form.steps.map((s, i) => {
                const services = servicesOf(apps.find((a) => a.id === s.workload));
                const serviceOptions = (services.length ? services : [s.service]).map((v) => ({ value: v, label: v || "the container" }));
                return (
                  <div key={i} className="flex flex-col gap-[10px] bg-[var(--muted)] p-[12px]" aria-label={`Step ${i + 1}`}>
                    <div className="flex items-center gap-[8px]">
                      <span className="text-[13px] leading-[18px] font-semibold">Step {i + 1}</span>
                      <div className="ml-auto flex gap-[6px]">
                        <Btn size="sm" type="button" disabled={i === 0} onClick={() => move(i, -1)} aria-label={`Move step ${i + 1} up`}>Up</Btn>
                        <Btn size="sm" type="button" disabled={i === form.steps.length - 1} onClick={() => move(i, 1)} aria-label={`Move step ${i + 1} down`}>Down</Btn>
                        <Btn size="sm" type="button" danger disabled={form.steps.length === 1} onClick={() => setForm((f) => ({ ...f, steps: f.steps.filter((_, j) => j !== i) }))} aria-label={`Remove step ${i + 1}`}>Remove</Btn>
                      </div>
                    </div>
                    <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-[10px]">
                      <TextInput label="Title" value={s.title} placeholder="Optional" onChange={(e) => setStep(i, { title: e.target.value })} />
                      <Select label="Workload" value={s.workload} options={workloadOptions} onChange={(v) => {
                        const svc = servicesOf(apps.find((a) => a.id === v));
                        setStep(i, { workload: v ?? "", service: svc[0] ?? "" });
                      }} />
                      {s.workload.startsWith("group~") ? (
                        <TextInput label="Service or container" required value={s.service} placeholder="nginx"
                          onChange={(e) => setStep(i, { service: e.target.value.trim() })} />
                      ) : (
                        <Select label="Service" value={s.service} options={serviceOptions} onChange={(v) => setStep(i, { service: v ?? "" })} />
                      )}
                      <Select label="Where" value={s.mode} onChange={(v) => setStep(i, { mode: (v || "exec") as RunbookStep["mode"] })}
                        options={[{ value: "exec", label: "In a running container" }, { value: "run", label: "In a throwaway container" }]} />
                      {s.mode === "exec" && !s.workload.startsWith("group~") && (
                        <Select label="Replicas" value={s.replicas} onChange={(v) => setStep(i, { replicas: (v || "one") as RunbookStep["replicas"] })}
                          options={[{ value: "one", label: "One" }, { value: "all", label: "Every replica, in turn" }]} />
                      )}
                      <NumberInput label="Timeout (s)" value={s.timeoutSeconds} min={1} max={3600} onChange={(v) => setStep(i, { timeoutSeconds: typeof v === "number" ? v : 600 })} />
                    </div>
                    <TextArea label="Command" required className={MONO} rows={Math.min(10, Math.max(2, s.command.split("\n").length))} value={s.command}
                      placeholder={"python manage.py migrate\n# runs with sh -c; parameters are $NAME"} onChange={(e) => setStep(i, { command: e.target.value })} />
                    <Checkbox label="May fail" description="A failure is reported and the next steps still run." checked={s.continueOnError} onChange={(e) => setStep(i, { continueOnError: e.target.checked })} />
                  </div>
                );
              })}
            </section>
            {error && <Callout variant="light" color="danger" title="Not saved">{error}</Callout>}
          </form>
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="runbook-form" loading={save.isPending}>{id ? "Save" : "Create"}</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

// ── History ──────────────────────────────────────────────────────────────────

function History({ runbook, onOpen }: { runbook: Runbook; onOpen: (runId: string) => void }) {
  const runs = useQuery({ queryKey: ["runbook-runs", runbook.id], queryFn: () => listRunbookRuns(runbook.id) });
  if (runs.isLoading) return <span className={LBL}>Loading…</span>;
  if (!runs.data?.length) return <span className={LBL}>Not run yet.</span>;
  return (
    <ul className="m-0 flex list-none flex-col p-0" aria-label={`Runs of ${runbook.name}`}>
      {runs.data.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center gap-[10px] py-[6px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
          <RunStatus status={r.status} />
          <span className="text-[13px] leading-[18px]">{timeAgo(r.startedAt)}</span>
          <span className={LBL}>{r.trigger === "manual" ? `by ${r.user || "someone"}` : r.trigger === "schedule" ? "scheduled" : `by ${r.user} (assistant)`}</span>
          {Object.keys(r.params).length > 0 && (
            <code className={`truncate text-muted-foreground ${MONO}`}>{Object.entries(r.params).map(([k, v]) => `${k}=${v}`).join(" ")}</code>
          )}
          <Btn size="sm" className="ml-auto" onClick={() => onOpen(r.id)} aria-label={`Open the run of ${timeAgo(r.startedAt)}`}>Output</Btn>
        </li>
      ))}
    </ul>
  );
}

// ── List ─────────────────────────────────────────────────────────────────────

/**
 * Runbooks of one workload (its Runbooks tab) or of every workload (the
 * Runbooks page): run, see the history, and, with manage rights, define them.
 */
export function RunbookList({ workload, canOperate, openRun }: { workload?: string; canOperate: boolean; openRun?: string | null }) {
  const qc = useQueryClient();
  const { names } = useWorkloadNames();
  const list = useQuery({ queryKey: ["runbooks", workload ?? ""], queryFn: () => listRunbooks(workload) });
  const suggestions = useQuery({ queryKey: ["runbook-suggestions", workload], queryFn: () => runbookSuggestions(workload!), enabled: !!workload && canOperate, staleTime: 300_000 });
  const [editing, setEditing] = useState<Runbook | RunbookInput | "new" | null>(null);
  const [running, setRunning] = useState<{ rb: Runbook; runId?: string } | null>(null);
  const [history, setHistory] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Runbook | null>(null);
  const remove = useMutation({
    mutationFn: (rb: Runbook) => deleteRunbook(rb.id),
    onSuccess: () => {
      setDeleting(null);
      void qc.invalidateQueries({ queryKey: ["runbooks"] });
    },
    onError: (err) => notify({ color: "red", title: "Could not delete", message: errorMessage(err, "") }),
  });
  // ?run=<id> (from the command palette) opens that runbook's run dialog once.
  const opened = useRef(false);
  useEffect(() => {
    if (!openRun || opened.current || !list.data) return;
    const rb = list.data.find((r) => r.id === openRun);
    if (rb) {
      opened.current = true;
      setRunning({ rb });
    }
  }, [openRun, list.data]);

  const items = list.data ?? [];
  const have = new Set(items.map((r) => r.steps.map((s) => s.command).join("\n")));
  const fresh = (suggestions.data ?? []).filter((s) => !have.has(s.steps.map((st) => st.command).join("\n")));

  return (
    <div className="flex min-w-0 flex-col gap-[16px]">
      <Band
        card
        title="Runbooks"
        count={list.data ? items.length : undefined}
        right={canOperate && <Btn solid onClick={() => setEditing("new")}>New runbook</Btn>}
      >
        {list.isLoading && <p className={`m-0 p-[16px] ${LBL}`}>Loading…</p>}
        {list.isError && <p className="m-0 p-[16px] text-[14px] text-[var(--tone-red-text)]">{errorMessage(list.error, "Could not load the runbooks")}</p>}
        {list.data && !items.length && (
          <p className="m-0 p-[16px] text-[14px] leading-[20px] text-muted-foreground">
            No runbooks yet. A runbook is an action you run often: empty a cache, migrate a database, reload a configuration. Define it once, then anyone with
            access runs it with a click, without a console.
          </p>
        )}
        {items.map((rb) => (
          <div key={rb.id} className="flex flex-col gap-[8px] p-[14px_16px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
            <div className="flex flex-wrap items-center gap-[10px]">
              <span className="text-[15px] leading-[20px] font-semibold">{rb.name}</span>
              {rb.source === "labels" && <Tag>From labels</Tag>}
              {rb.confirm && <Tag>Asks to confirm</Tag>}
              {rb.lastRun && (
                <span className="inline-flex items-center gap-[6px]">
                  <RunStatus status={rb.lastRun.status} />
                  <span className={LBL}>{timeAgo(rb.lastRun.startedAt)}</span>
                </span>
              )}
              <div className="ml-auto flex flex-wrap gap-[6px]">
                <Btn size="sm" onClick={() => setHistory(history === rb.id ? null : rb.id)} aria-expanded={history === rb.id} aria-label={`History of ${rb.name}`}>History</Btn>
                {canOperate && rb.source === "kowan" && (
                  <>
                    <Btn size="sm" onClick={() => setEditing(rb)} aria-label={`Edit ${rb.name}`}>Edit</Btn>
                    <Btn size="sm" danger onClick={() => setDeleting(rb)} aria-label={`Delete ${rb.name}`}>Delete</Btn>
                  </>
                )}
                {canOperate && <Btn size="sm" solid onClick={() => setRunning({ rb })} aria-label={`Run ${rb.name}`}>Run</Btn>}
              </div>
            </div>
            {rb.description && <p className="m-0 text-[13px] leading-[18px] text-muted-foreground">{rb.description}</p>}
            <span className={LBL}>
              {rb.steps.length === 1 ? whereLabel(rb.steps[0], names) : `${rb.steps.length} steps on ${rb.workloads.map((w) => names.get(w) ?? w).join(", ")}`}
              {rb.params.length > 0 && ` · asks ${rb.params.map((p) => p.label || p.name).join(", ")}`}
            </span>
            {history === rb.id && <History runbook={rb} onOpen={(runId) => setRunning({ rb, runId })} />}
          </div>
        ))}
      </Band>

      {workload && canOperate && fresh.length > 0 && (
        <Band card title="Suggested for this workload" right={<span className={LBL}>From its images</span>}>
          {fresh.map((s, i) => (
            <div key={i} className="flex flex-wrap items-center gap-[10px] p-[10px_16px] shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
              <span className="text-[14px] leading-[20px] font-semibold">{s.name}</span>
              <span className="text-[13px] leading-[18px] text-muted-foreground">{s.description}</span>
              <Btn size="sm" className="ml-auto" onClick={() => setEditing(s)} aria-label={`Add ${s.name}`}>Add</Btn>
            </div>
          ))}
        </Band>
      )}

      {editing && <RunbookEditor initial={editing === "new" ? undefined : editing} workload={workload} onClose={() => setEditing(null)} />}
      {running && <RunDialog runbook={running.rb} runId={running.runId} names={names} onClose={() => setRunning(null)} />}
      <ConfirmModal
        opened={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete runbook"
        confirmLabel="Delete"
        loading={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting)}
        message={`Delete ${deleting?.name ?? ""} and its history? Schedules that run it will fail.`}
      />
    </div>
  );
}
