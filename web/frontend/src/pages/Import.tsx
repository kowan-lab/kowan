// Import: bring stacks that already run elsewhere under Kowan, from a
// Portainer instance (read through its API with a token or a login, used once
// and not kept) or from a folder of compose stacks picked in the browser.
// Every candidate becomes a deployment that is created, not deployed: one
// imported under its compose project name adopts the containers already up.

import { useMemo, useState, type ChangeEvent } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Badge, Button, Callout, Checkbox, Code, Icon, InputToggle, Select, Tabs, Text, TextInput } from "@/ui/kit";
import { applyImport, listHosts, scanComposeFolder, scanPortainer } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { appHref } from "../lib/apps";
import type { ImportCandidate, ImportResult } from "../types";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";

const COMPOSE_FILE = /(^|\/)(docker-)?compose\.ya?ml$|(^|\/)\.env$/;

/** Read the compose files and .env files of a picked folder, by relative path. */
export async function readFolder(files: FileList | File[]): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const f of Array.from(files)) {
    const path = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
    if (!COMPOSE_FILE.test(path) || f.size > 1 << 20) continue;
    out[path] = await f.text();
  }
  return out;
}

interface Row extends ImportCandidate {
  selected: boolean;
  /** The name as found: labels stay put while the name is edited. */
  found: string;
  /** Variables to write into the compose; the rest go to the secret. */
  inline: string[];
}

export default function Import() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState(isAdmin ? "portainer" : "folder");
  const [rows, setRows] = useState<Row[]>([]);
  const [results, setResults] = useState<ImportResult[] | null>(null);
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const qc = useQueryClient();

  const load = (items: ImportCandidate[]) => {
    setResults(null);
    setRows(items.map((i) => ({ ...i, found: i.name, inline: [], selected: i.importable && i.warnings.every((w) => !w.includes("exists already")) })));
    if (!items.length) notify({ color: "yellow", title: "Nothing to import", message: "No stacks were found." });
  };

  const apply = useMutation({
    mutationFn: (items: Row[]) =>
      applyImport(items.map((r) => ({ name: r.name.trim(), host: r.host, strategy: r.strategy, ...(r.git ? { git: r.git } : { compose: r.compose, ...(r.env ? { env: { secret: r.env.secret, values: r.env.values, inline: r.inline } } : {}) }) }))),
    onSuccess: (res) => {
      setResults(res);
      qc.invalidateQueries({ queryKey: ["deployments"] });
      const ok = res.filter((r) => r.ok).length;
      notify({ color: ok === res.length ? "green" : "yellow", title: `Imported ${ok} of ${res.length}`, message: ok ? "They are created, not deployed: deploy each when ready." : undefined });
    },
    onError: (e) => notify({ color: "red", title: "Import failed", message: errorMessage(e, "") }),
  });

  const picked = rows.filter((r) => r.selected);
  const hostOptions = (hosts.data ?? []).map((h) => ({ value: h.id, label: h.name }));
  const set = (key: string, p: Partial<Row>) => setRows((rs) => rs.map((r) => (r.key === key ? { ...r, ...p } : r)));

  return (
    <div className="flex flex-col gap-[16px]">
      <PageHeader
        title="Import stacks"
        description="Bring stacks that already run under Kowan. Each becomes a deployment, created but not deployed; with the same compose project name it takes over the running containers."
        icon={<Icon name="download-rounded" size="lg" />}
      />
      <Tabs variant="segmented" value={tab} onValueChange={(t) => { setTab(t); setRows([]); setResults(null); }}>
        <Tabs.List>
          <Tabs.Trigger value="portainer" disabled={!isAdmin}>From Portainer</Tabs.Trigger>
          <Tabs.Trigger value="folder">From compose folders</Tabs.Trigger>
        </Tabs.List>
      </Tabs>
      {tab === "portainer" ? <PortainerForm onScan={load} /> : <FolderForm onScan={load} />}

      {rows.length > 0 && (
        <SectionCard
          title="Stacks found"
          description="Pick what to import, its name and the host it runs on."
          actions={
            <Button loading={apply.isPending} disabled={!picked.length || picked.some((r) => !r.name.trim() || !r.host)} onClick={() => apply.mutate(picked)}>
              Import {picked.length}
            </Button>
          }
          noBodyPadding
        >
          <ul className="m-0 list-none p-0" aria-label="Stacks found">
            {rows.map((r) => {
              const res = results?.find((x) => x.name === r.name.trim());
              return (
                <li key={r.key} className="flex flex-wrap items-start gap-x-[16px] gap-y-[8px] border-b px-[16px] py-[12px] last:border-b-0">
                  <Checkbox
                    className="mt-[6px]"
                    aria-label={`Import ${r.found}`}
                    checked={r.selected}
                    disabled={!r.importable}
                    onChange={(e) => set(r.key, { selected: e.target.checked })}
                  />
                  <div className="flex min-w-[220px] flex-1 flex-col gap-[4px]">
                    <TextInput size="sm" aria-label={`Name of ${r.found}`} value={r.name} disabled={!r.importable} onChange={(e) => set(r.key, { name: e.target.value })} />
                    <Text size="xs" color="muted">
                      {r.source === "portainer" ? `Portainer · ${r.endpoint}` : r.endpoint} · {r.git ? `git ${r.git.repoUrl} @ ${r.git.ref}` : "compose file"}
                      {r.envCount > 0 && !r.git ? ` · ${r.envCount} variables` : ""}
                    </Text>
                    {r.env && r.importable && <EnvChoice row={r} onChange={(inline) => set(r.key, { inline })} />}
                    <div className="flex flex-wrap gap-[6px]">
                      <Badge color={r.kind === "kubernetes" ? "gray" : "blue"} size="sm">{r.kind}</Badge>
                      {!r.active && <Badge color="gray" size="sm">stopped in Portainer</Badge>}
                    </div>
                    {r.warnings.map((w) => (
                      <Text key={w} size="xs" color="warning">{w}</Text>
                    ))}
                    {res && (res.ok ? (
                      <Link to={appHref(`dep~${res.id}`)} className="text-[13px] font-semibold text-[var(--link)] hover:underline">Imported: open {r.name}</Link>
                    ) : (
                      <Text size="sm" color="danger">{res.error}</Text>
                    ))}
                  </div>
                  <Select size="sm" width={180} aria-label={`Host for ${r.found}`} options={hostOptions} value={r.host} disabled={!r.importable} onChange={(v) => v && set(r.key, { host: v })} />
                </li>
              );
            })}
          </ul>
        </SectionCard>
      )}
    </div>
  );
}

function PortainerForm({ onScan }: { onScan: (items: ImportCandidate[]) => void }) {
  const [url, setUrl] = useState("");
  const [login, setLogin] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [verifyTls, setVerifyTls] = useState(true);
  const [error, setError] = useState("");
  const scan = useMutation({
    mutationFn: () => scanPortainer(login ? { url, username, password, verifyTls } : { url, apiKey, verifyTls }),
    onSuccess: (items) => {
      setError("");
      onScan(items);
    },
    onError: (e) => setError(errorMessage(e, "Scan failed")),
  });
  const ready = url.trim() && (login ? username && password : apiKey);
  return (
    <SectionCard title="Portainer" description="Kowan reads the stacks, their compose files and variables. The token or password is used for this read and not kept.">
      <form className="flex max-w-[560px] flex-col gap-[12px]" onSubmit={(e) => { e.preventDefault(); if (ready) scan.mutate(); }}>
        <TextInput label="Portainer URL" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://portainer.example.com" />
        <InputToggle label="Sign in with a username and password instead of an access token" checked={login} onChange={(e) => setLogin(e.target.checked)} />
        {login ? (
          <>
            <TextInput label="Username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
            <TextInput label="Password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off" />
          </>
        ) : (
          <TextInput label="Access token" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off"
            description="My account → Access tokens in Portainer." />
        )}
        <InputToggle label="Verify the TLS certificate" checked={verifyTls} onChange={(e) => setVerifyTls(e.target.checked)} />
        {error && <Callout color="danger" title="Not scanned">{error}</Callout>}
        <div>
          <Button type="submit" loading={scan.isPending} disabled={!ready}>Find stacks</Button>
        </div>
      </form>
    </SectionCard>
  );
}

function FolderForm({ onScan }: { onScan: (items: ImportCandidate[]) => void }) {
  const [count, setCount] = useState(0);
  const [error, setError] = useState("");
  const scan = useMutation({
    mutationFn: (files: Record<string, string>) => scanComposeFolder(files),
    onSuccess: (items) => {
      setError("");
      onScan(items);
    },
    onError: (e) => setError(errorMessage(e, "Scan failed")),
  });
  const pick = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = await readFolder(e.target.files ?? []);
    setCount(Object.keys(files).length);
    if (!Object.keys(files).length) {
      setError("No compose files in that folder.");
      return;
    }
    scan.mutate(files);
  };
  const dirProps = useMemo(() => ({ webkitdirectory: "", directory: "" }) as Record<string, string>, []);
  return (
    <SectionCard title="Compose folders" description="Pick the folder that holds your stacks, one sub-folder each (like /opt/stacks). Only compose files and .env files are read, in your browser.">
      <div className="flex flex-col gap-[10px]">
        <label className="inline-flex w-fit cursor-pointer items-center gap-[8px] border px-[12px] py-[6px] text-[13px] font-semibold hover:bg-accent">
          <Icon name="folder-open-outline-rounded" size="sm" />
          Choose folder
          <input type="file" multiple className="sr-only" aria-label="Choose folder" onChange={pick} {...dirProps} />
        </label>
        {count > 0 && <Text size="sm" color="muted">{count} files read.</Text>}
        {error && <Callout color="danger" title="Not scanned">{error}</Callout>}
      </div>
    </SectionCard>
  );
}

/**
 * The variables of an imported stack: each one goes to the encrypted secret
 * (the default) or, ticked, into the compose. Values are never shown.
 */
function EnvChoice({ row, onChange }: { row: Row; onChange: (inline: string[]) => void }) {
  const env = row.env!;
  const names = Object.keys(env.values);
  const inline = new Set(row.inline);
  const toSecret = names.filter((n) => !inline.has(n));
  const toggle = (name: string, on: boolean) => onChange(on ? [...row.inline, name] : row.inline.filter((n) => n !== name));
  return (
    <fieldset className="m-0 mt-[4px] flex min-w-0 flex-col gap-[6px] border-0 p-0">
      <legend className="mb-[4px] p-0">
        <Text size="xs" color="muted">
          {toSecret.length
            ? <>Variables go to the encrypted secret <Code>{env.secret}</Code>; tick the ones to write into the compose.</>
            : "Every variable is written into the compose: no secret is created."}
        </Text>
      </legend>
      <div className="flex flex-wrap gap-x-[16px] gap-y-[6px]">
        {names.map((n) => (
          <Checkbox
            key={n}
            size="sm"
            label={
              <span className="font-mono text-[12px]">
                {n}
                {env.inImage.includes(n) && <span className="ml-[6px] text-[var(--tone-amber-text)]">image</span>}
              </span>
            }
            aria-label={`Write ${n} into the compose of ${row.found}`}
            checked={inline.has(n)}
            onChange={(e) => toggle(n, e.target.checked)}
          />
        ))}
      </div>
      {env.inImage.some((n) => !inline.has(n)) && (
        <Text size="xs" color="warning">
          {env.inImage.filter((n) => !inline.has(n)).join(", ")} {env.inImage.filter((n) => !inline.has(n)).length === 1 ? "is" : "are"} part of an image: in the secret, update checks, scans and promotion cannot read which image runs.
        </Text>
      )}
    </fieldset>
  );
}
