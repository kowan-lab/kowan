import { useState } from "react";
import { Box, Button, Callout, Text } from "@/ui/kit";
import type { GitOpsChange, GitOpsPlan } from "../../types";
import { DiffView } from "./DiffView";
import { ActionPill, shortSha } from "./status";

function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.includes("\n") ? `${v.split("\n").length} lines` : JSON.stringify(v);
  return JSON.stringify(v);
}

function ChangeRow({ c, defaultOpen }: { c: GitOpsChange; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const expandable = c.diff.length > 0 || c.fields.length > 0 || Boolean(c.blocked);
  return (
    <li className="border-b last:border-b-0">
      <button
        type="button"
        disabled={!expandable}
        onClick={() => setOpen((o) => !o)}
        aria-expanded={expandable ? open : undefined}
        className="flex w-full items-center gap-5 px-[var(--page-px,16px)] py-[4px] text-left text-[13px] outline-none enabled:hover:bg-muted/40 focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
      >
        <span aria-hidden className="w-5 shrink-0 text-muted-foreground">{expandable ? (open ? "▾" : "▸") : ""}</span>
        <span className="w-42 shrink-0"><ActionPill action={c.action} adopt={c.adopt} /></span>
        <span className="w-54 shrink-0 truncate font-mono text-[12px] text-muted-foreground">{c.kind}</span>
        <span className="min-w-0 flex-1 truncate font-medium">{c.name}</span>
        {c.blocked && <Text size="xs" color="danger">blocked: {c.blocked}</Text>}
        {c.fields.length > 0 && c.action === "update" && (
          <span className="shrink-0 text-[12px] text-muted-foreground tabular-nums">
            {c.fields.length} field{c.fields.length === 1 ? "" : "s"}
          </span>
        )}
        {c.file && <span className="hidden shrink-0 truncate font-mono text-[12px] text-muted-foreground md:inline">{c.file}</span>}
      </button>
      {open && expandable && (
        <div className="flex flex-col gap-3 px-[var(--page-px,16px)] pb-5 pl-[calc(var(--page-px,16px)+1.5rem)]">
          {c.action === "update" && c.fields.length > 0 && (
            <table className="w-full max-w-3xl border-separate border-spacing-0 text-[13px]">
              <thead>
                <tr className="text-left text-[12px] tracking-[0.06em] text-muted-foreground font-mono uppercase">
                  <th className="py-2 pr-5 font-medium">Field</th>
                  <th className="py-2 pr-5 font-medium">Current</th>
                  <th className="py-2 font-medium">From git</th>
                </tr>
              </thead>
              <tbody>
                {c.fields.map((f) => (
                  <tr key={f.path} className="border-t align-top">
                    <td className="py-2 pr-5 font-mono">{f.path}</td>
                    <td className="py-2 pr-5 font-mono break-all text-[var(--tone-red-text)]">{fmt(f.before)}</td>
                    <td className="py-2 font-mono break-all text-[var(--tone-green-text)]">{fmt(f.after)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <DiffView lines={c.diff} />
        </div>
      )}
    </li>
  );
}

/**
 * A sync plan: errors first (they block apply), then every change in apply
 * order with its readable line diff. Unchanged resources fold away.
 */
export function PlanView({ plan }: { plan: GitOpsPlan }) {
  const [showNoop, setShowNoop] = useState(false);
  const visible = plan.changes.filter((c) => c.action !== "forget");
  const changed = visible.filter((c) => c.action !== "noop" || c.adopt);
  const noop = visible.filter((c) => c.action === "noop" && !c.adopt);
  const s = plan.summary;
  return (
    <Box direction="column" gap={3}>
      <Text size="sm" color="muted">
        Commit <span className="font-mono text-foreground">{shortSha(plan.commit) || "—"}</span>
        {" · "}
        {s.create} to create · {s.update} to update · {s.delete} to delete
        {s.orphan ? ` · ${s.orphan} to release` : ""}
        {s.adopt ? ` · ${s.adopt} to adopt` : ""} · {s.noop} unchanged
      </Text>
      {plan.errors.length > 0 && (
        <Callout color="danger" title={`${plan.errors.length} problem${plan.errors.length === 1 ? "" : "s"} in the repository — fix them before applying`}>
          <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px]">
            {plan.errors.map((e, i) => (
              <li key={i}>
                <span className="font-mono">{e.file || "repository"}{e.doc ? `#${e.doc + 1}` : ""}</span>
                {e.kind ? ` · ${e.kind}` : ""}
                {e.name ? ` ${e.name}` : ""}: {e.message}
              </li>
            ))}
          </ul>
        </Callout>
      )}
      <div className="shadow-[inset_0_0_0_1px_var(--border)] bg-card">
        {changed.length === 0 && plan.errors.length === 0 && (
          <Text size="sm" color="muted" className="block px-[var(--page-px,16px)] py-5">
            Everything matches the repository.
          </Text>
        )}
        <ul className="m-0 list-none p-0">
          {changed.map((c) => (
            <ChangeRow key={`${c.kind}/${c.name}/${c.action}`} c={c} defaultOpen={changed.length <= 3} />
          ))}
          {showNoop && noop.map((c) => <ChangeRow key={`${c.kind}/${c.name}`} c={c} defaultOpen={false} />)}
        </ul>
        {noop.length > 0 && (
          <div className="border-t px-[var(--page-px,16px)] py-[4px]">
            <Button size="xs" variant="clear" onClick={() => setShowNoop((v) => !v)}>
              {showNoop ? "Hide" : "Show"} {noop.length} unchanged
            </Button>
          </div>
        )}
      </div>
    </Box>
  );
}
