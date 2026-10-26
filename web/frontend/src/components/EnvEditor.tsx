// The environment rows of a workload (a container's env, a compose stack's
// variables), each one Config or Secret. A secret row's value goes to the
// deployment's own encrypted secret (kowan/deployment/envsecrets.py): the API
// sends back only its reference, so a saved secret shows as "unchanged" and is
// left as it is unless a new value is typed.
import { Lock, LockOpen } from "lucide-react";
import { Box, Button, Text, TextInput, Tooltip } from "@/ui/kit";
import type { KeyValue } from "../types";
import { cn } from "@/lib/utils";

/** A row in the form; `stored` keeps a saved secret's reference. */
export interface EnvRow extends KeyValue {
  stored?: string;
}

const VAR_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Rows from the API: a saved secret's reference moves out of the input. */
export function envRowsFromApi(rows: KeyValue[] | undefined): EnvRow[] {
  return (rows ?? []).map((r) => (r.secret ? { key: r.key, value: "", secret: true, stored: r.value } : { key: r.key, value: r.value }));
}

/** Rows for the API: unnamed rows dropped, an untouched secret sends its reference. */
export function envRowsToApi(rows: EnvRow[]): KeyValue[] {
  return rows
    .filter((r) => r.key.trim())
    .map((r) => {
      const key = r.key.trim();
      if (!r.secret) return { key, value: r.value };
      return { key, value: r.value === "" && r.stored ? r.stored : r.value, secret: true };
    });
}

/** The first problem with the rows, or null. */
export function envRowsProblem(rows: EnvRow[], { strictNames = false } = {}): string | null {
  const seen = new Set<string>();
  for (const r of rows) {
    const key = r.key.trim();
    if (!key) continue;
    if (strictNames && !VAR_NAME.test(key)) return `${key}: use letters, digits and _ (not starting with a digit)`;
    if (seen.has(key)) return `${key} is set twice`;
    seen.add(key);
    if (r.secret && r.value === "" && !r.stored) return `Enter a value for the secret ${key}`;
  }
  return null;
}

export default function EnvEditor({
  value,
  onChange,
  strictNames = false,
  allowSecrets = true,
  emptyText = "No variables.",
}: {
  value: EnvRow[];
  onChange: (rows: EnvRow[]) => void;
  /** Compose interpolation names: letters, digits and _. */
  strictNames?: boolean;
  /** Offer the Config/Secret switch (only a deployment owns a secret). */
  allowSecrets?: boolean;
  emptyText?: string;
}) {
  const update = (i: number, patch: Partial<EnvRow>) => onChange(value.map((row, idx) => (idx === i ? { ...row, ...patch } : row)));
  return (
    <Box direction="column" gap={2}>
      {value.length === 0 && (
        <Text size="xs" color="muted">
          {emptyText}
        </Text>
      )}
      {value.map((row, i) => {
        const key = row.key.trim();
        const bad = key !== "" && strictNames && !VAR_NAME.test(key);
        const twice = key !== "" && value.findIndex((v) => v.key.trim() === key) !== i;
        const n = i + 1;
        return (
          <Box key={i} direction="row" wrap="nowrap" gap={2} align="start">
            <TextInput
              placeholder="NAME"
              aria-label={`Name of variable ${n}`}
              value={row.key}
              onChange={(e) => update(i, { key: e.target.value })}
              errorText={bad ? "Letters, digits and _" : twice ? "Already set above" : undefined}
              style={{ flex: 1 }}
            />
            <TextInput
              type={row.secret ? "password" : "text"}
              autoComplete="off"
              placeholder={row.secret ? (row.stored ? "•••••• unchanged" : "secret value") : "value or [[secret.field]]"}
              aria-label={`Value of variable ${n}`}
              value={row.value}
              onChange={(e) => update(i, { value: e.target.value })}
              style={{ flex: 2 }}
            />
            {allowSecrets && <Tooltip content={row.secret ? "Secret: stored encrypted, never shown again" : "Config: stored as written"}>
              <Button
                variant={row.secret ? "light" : "clear"}
                color={row.secret ? "warning" : "neutral"}
                aria-pressed={!!row.secret}
                aria-label={`Variable ${n} is a secret`}
                className={cn("shrink-0")}
                onClick={() => update(i, row.secret ? { secret: false, stored: undefined, value: "" } : { secret: true })}
              >
                {row.secret ? <Lock className="size-[14px]" aria-hidden /> : <LockOpen className="size-[14px]" aria-hidden />}
                <span className="ml-[6px] max-sm:hidden">{row.secret ? "Secret" : "Config"}</span>
              </Button>
            </Tooltip>}
            <Button
              variant="clear"
              color="danger"
              aria-label={`Remove variable ${n}`}
              iconLeft="delete-outline-rounded"
              onClick={() => onChange(value.filter((_, idx) => idx !== i))}
            />
          </Box>
        );
      })}
    </Box>
  );
}
