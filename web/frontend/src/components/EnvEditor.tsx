// The environment rows of a workload (a container's env, a compose stack's
// variables), each one Config or Secret. A secret row's value goes to the
// deployment's own encrypted secret (kowan/deployment/envsecrets.py): the API
// sends back only its reference, so a saved secret shows as "unchanged" and is
// left as it is unless a new value is typed.
import { useState } from "react";
import { Lock, LockOpen } from "lucide-react";
import { Box, Button, Popup, Text, TextArea, TextInput, Tooltip } from "@/ui/kit";
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

// ---------------------------------------------------------------------------
// The rows as a .env text, for editing them all at once.
// ---------------------------------------------------------------------------

/** What an unchanged saved secret shows as in the text (its value is never sent back). */
export const SECRET_KEPT = "********";
const SECRET_NAME = /(PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|PRIVATE|SALT|DSN)/i;

/** Whether a name looks like it holds a secret (a new row starts as Secret). */
export function looksSecret(name: string): boolean {
  return SECRET_NAME.test(name);
}

/** The rows as KEY=value lines; a saved secret as KEY=******** (kept as is). */
export function envRowsToText(rows: EnvRow[]): string {
  return rows
    .filter((r) => r.key.trim())
    .map((r) => {
      const v = r.secret && r.value === "" && r.stored ? SECRET_KEPT : r.value;
      return `${r.key.trim()}=${/[\s#"']/.test(v) ? JSON.stringify(v) : v}`;
    })
    .join("\n");
}

/**
 * Rows from a .env text, against the current rows: a name keeps its
 * Config/Secret kind, ******** keeps a saved secret, a new name that looks
 * secret starts as Secret (when secrets are allowed). Blank lines, comments
 * and `export ` are fine; quotes are taken off. A name left out is removed.
 */
export function envRowsFromText(text: string, current: EnvRow[], allowSecrets = true): EnvRow[] {
  const byKey = new Map(current.map((r) => [r.key.trim(), r]));
  const out: EnvRow[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const body = line.replace(/^export\s+/, "");
    const eq = body.indexOf("=");
    const key = (eq < 0 ? body : body.slice(0, eq)).trim();
    if (!key) continue;
    let value = eq < 0 ? "" : body.slice(eq + 1).trim();
    if (value.length >= 2 && ((value[0] === '"' && value.endsWith('"')) || (value[0] === "'" && value.endsWith("'")))) {
      value = value[0] === '"' ? (() => { try { return JSON.parse(value) as string; } catch { return value.slice(1, -1); } })() : value.slice(1, -1);
    }
    const was = byKey.get(key);
    const secret = was ? !!was.secret : allowSecrets && looksSecret(key);
    if (was?.secret && was.stored && value === SECRET_KEPT) out.push({ ...was, value: "" });
    else out.push(secret ? { key, value, secret: true } : { key, value });
  }
  return out;
}

/** Edit every row as one .env text. */
function EnvTextDialog({ rows, allowSecrets, onApply, onClose }: { rows: EnvRow[]; allowSecrets: boolean; onApply: (rows: EnvRow[]) => void; onClose: () => void }) {
  const [text, setText] = useState(() => envRowsToText(rows));
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Edit as .env</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <Box direction="column" gap={2}>
            <TextArea
              label="One NAME=value per line"
              rows={14}
              className="font-mono"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={"DB_HOST=db\nDB_PASSWORD=change-me\n# comments and blank lines are fine"}
            />
            <Text size="xs" color="muted">
              Paste a .env file or edit the list. A name you remove is removed.
              {allowSecrets ? ` Saved secrets show as ${SECRET_KEPT}: leave it to keep them; a new name that looks secret (PASSWORD, TOKEN, KEY…) is stored as a secret.` : ""}
            </Text>
          </Box>
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose}>Cancel</Button>
          <Button onClick={() => { onApply(envRowsFromText(text, rows, allowSecrets)); onClose(); }}>Apply</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
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
  const [asText, setAsText] = useState(false);
  return (
    <Box direction="column" gap={2}>
      <Box direction="row" justify="between" align="center">
        {value.length === 0 ? (
          <Text size="xs" color="muted">
            {emptyText}
          </Text>
        ) : <span />}
        <Button size="sm" variant="clear" iconLeft="edit-outline-rounded" onClick={() => setAsText(true)}>Edit as .env</Button>
      </Box>
      {asText && <EnvTextDialog rows={value} allowSecrets={allowSecrets} onApply={onChange} onClose={() => setAsText(false)} />}
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

// ---------------------------------------------------------------------------
// The ${VAR}s a compose file uses.
// ---------------------------------------------------------------------------

/** Every variable a compose file interpolates, once each, in order, and
 *  whether a default is given (`${A:-x}`, `${A-x}`); `$$` is a literal $. */
export function composeVariables(compose: string): { name: string; hasDefault: boolean }[] {
  const out = new Map<string, boolean>();
  const re = /\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(:?[-?+][^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)/g;
  for (const m of compose.matchAll(re)) {
    if (m[0] === "$$") continue;
    const name = m[1] ?? m[3];
    const hasDefault = !!m[2] && /^:?-/.test(m[2]);
    out.set(name, (out.get(name) ?? true) && hasDefault);
  }
  return [...out].map(([name, hasDefault]) => ({ name, hasDefault }));
}
