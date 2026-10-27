import { Button, Spinner, StatePill, Text, Tooltip } from "@/ui/kit";
import type { ComposeDiagnostic, ComposeValidation, DiagnosticSeverity } from "../../types";

const TONE: Record<DiagnosticSeverity, "red" | "amber" | "gray"> = {
  error: "red",
  warning: "amber",
  info: "gray",
};
const LABEL: Record<DiagnosticSeverity, string> = { error: "Error", warning: "Warn", info: "Info" };

interface Props {
  diagnostics: ComposeDiagnostic[];
  result: ComposeValidation | null;
  loading: boolean;
  error: string | null;
  /** Jump the editor to a diagnostic's position. */
  onJump?: (d: ComposeDiagnostic) => void;
  /** Apply a diagnostic's fix patch. */
  onFix?: (d: ComposeDiagnostic) => void;
}

/**
 * The diagnostics list shown under the compose editor: one hairline row per
 * finding, severity as a labelled pill (never colour alone), the position as a
 * button that jumps the YAML editor there, and "Apply fix" when a safe
 * automatic change exists.
 */
export function ComposeDiagnostics({ diagnostics, result, loading, error, onJump, onFix }: Props) {
  const counts = { error: 0, warning: 0, info: 0 };
  for (const d of diagnostics) counts[d.severity] += 1;
  const engine = result?.engine === "docker" ? "docker compose config" : result ? "built-in checks" : "";
  const hostChecked = result?.checks?.hostPorts;

  return (
    <div className="flex flex-col border bg-card" aria-live="polite">
      <div className="flex min-h-12 flex-wrap items-center gap-x-5 gap-y-2 shadow-[inset_0_-1px_0_var(--border)] bg-muted/50 px-5 py-2 text-[12px] text-muted-foreground">
        <span className="font-medium tracking-normal font-sans ">Validation</span>
        {loading && <Spinner size="xs" label="Validating" />}
        {!loading && result && diagnostics.length === 0 && <span className="text-foreground">No problems found</span>}
        {(["error", "warning", "info"] as const).map((s) =>
          counts[s] > 0 ? (
            <span key={s} className="tabular-nums">
              {counts[s]} {s === "info" ? "info" : `${s}${counts[s] === 1 ? "" : "s"}`}
            </span>
          ) : null,
        )}
        <span className="ml-auto">
          {engine}
          {result && !hostChecked ? " · host checks skipped" : ""}
        </span>
      </div>
      {error && (
        <Text size="sm" color="danger" className="px-5 py-3">
          {error}
        </Text>
      )}
      {diagnostics.length > 0 && (
        <ul className="m-0 max-h-96 list-none overflow-y-auto p-0">
          {diagnostics.map((d, i) => (
            <li
              key={`${d.code}-${d.path}-${d.line}-${i}`}
              className="flex items-start gap-3 border-b px-5 py-[4px] text-[13px] last:border-b-0"
            >
              <StatePill status={d.severity} label={LABEL[d.severity]} color={TONE[d.severity]} size="sm" className="mt-px shrink-0" />
              <div className="flex min-w-0 flex-1 flex-col gap-[2px]">
                <span className="break-words text-foreground">{d.message}</span>
                <span className="font-mono text-[12px] text-muted-foreground">
                  {d.code}
                  {d.path ? ` · ${d.path}` : ""}
                </span>
              </div>
              {d.line > 0 && (
                <Tooltip content="Show in the YAML editor">
                  <button
                    type="button"
                    onClick={() => onJump?.(d)}
                    className="shrink-0 font-mono text-[12px] text-[var(--tone-primary-text)] tabular-nums underline-offset-2 outline-none hover:underline focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
                    aria-label={`Go to line ${d.line}, column ${d.column}`}
                  >
                    L{d.line}:{d.column}
                  </button>
                </Tooltip>
              )}
              {d.fix && onFix && (
                <Tooltip content={d.fix.description}>
                  <Button size="xs" variant="outline" onClick={() => onFix(d)} aria-label={`Apply fix: ${d.fix.description}`}>
                    Apply fix
                  </Button>
                </Tooltip>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
