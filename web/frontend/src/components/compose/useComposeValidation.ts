import { useEffect, useRef, useState } from "react";
import { validateCompose } from "../../api/client";
import type { ComposeDiagnostic, ComposeValidation } from "../../types";

export interface ComposeValidationTarget {
  /** Target host: enables the host-port / container-name / bind-path checks. */
  hostId?: string;
  /** Existing deployment: its own containers don't count as conflicts. */
  deploymentId?: string;
  /** Stack variables: their ${VAR}s count as set. */
  env?: Record<string, string>;
}

export interface ComposeValidationState {
  result: ComposeValidation | null;
  diagnostics: ComposeDiagnostic[];
  loading: boolean;
  error: string | null;
  /** The compose text the current result describes. */
  validatedText: string;
}

const EMPTY: ComposeDiagnostic[] = [];

/**
 * Debounced live validation of compose YAML against POST /compose/validate.
 * Each change aborts the in-flight request; empty text clears the result.
 */
export function useComposeValidation(
  compose: string,
  target: ComposeValidationTarget,
  { enabled = true, delayMs = 600 }: { enabled?: boolean; delayMs?: number } = {},
): ComposeValidationState {
  const [state, setState] = useState<ComposeValidationState>({
    result: null, diagnostics: EMPTY, loading: false, error: null, validatedText: "",
  });
  const ctrl = useRef<AbortController | null>(null);
  const { hostId, deploymentId, env } = target;
  // A stable key, so re-renders with an equal map do not re-validate.
  const envKey = JSON.stringify(env ?? {});

  useEffect(() => {
    if (!enabled || compose.trim() === "") {
      ctrl.current?.abort();
      setState({ result: null, diagnostics: EMPTY, loading: false, error: null, validatedText: compose });
      return;
    }
    setState((s) => ({ ...s, loading: true }));
    const timer = window.setTimeout(() => {
      ctrl.current?.abort();
      const c = new AbortController();
      ctrl.current = c;
      const vars = JSON.parse(envKey) as Record<string, string>;
      validateCompose({ compose, hostId: hostId || undefined, deploymentId: deploymentId || undefined, env: Object.keys(vars).length ? vars : undefined }, c.signal)
        .then((result) => {
          if (c.signal.aborted) return;
          setState({ result, diagnostics: result.diagnostics, loading: false, error: null, validatedText: compose });
        })
        .catch((err: unknown) => {
          if (c.signal.aborted) return;
          const msg = (err as { response?: { data?: { error?: string } }; message?: string })?.response?.data?.error
            ?? (err as { message?: string })?.message ?? "Validation failed";
          // A failed check never blocks the form (the server re-validates on
          // save/deploy): mark this text as checked, with no findings.
          setState({ result: null, diagnostics: EMPTY, loading: false, error: msg, validatedText: compose });
        });
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [compose, hostId, deploymentId, envKey, enabled, delayMs]);

  useEffect(() => () => ctrl.current?.abort(), []);
  return state;
}

/** Stable identity of a warning set, used to scope a "deploy anyway" ack. */
export function warningsSignature(diags: ComposeDiagnostic[]): string {
  return diags
    .filter((d) => d.severity === "warning")
    .map((d) => `${d.code}@${d.path}`)
    .sort()
    .join("|");
}
