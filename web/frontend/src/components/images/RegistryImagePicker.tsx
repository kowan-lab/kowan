import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Code, Select, Text, TextInput } from "@/ui/kit";
import { registryCatalog, registryTags } from "../../api/client";
import type { Registry } from "../../types";
import { errorMessage } from "../../utils";

const DOCKER_HUB = new Set(["docker.io", "index.docker.io", "registry-1.docker.io"]);

/** The registry host of an image reference, the way Docker reads it. */
export function imageHost(ref: string): string {
  const name = ref.trim().split("@")[0];
  const first = name.split("/")[0];
  if (name.includes("/") && (first.includes(".") || first.includes(":") || first === "localhost")) {
    return first.toLowerCase();
  }
  return "docker.io";
}

/** A stored registry's host, without scheme or path. */
export function registryHost(r: Registry): string {
  return r.url.replace(/^[a-z]+:\/\//i, "").split("/")[0].toLowerCase();
}

/** The stored registry whose host serves ``ref`` (the one the server logs in with), if any. */
export function detectRegistry(ref: string, registries: Registry[]): Registry | undefined {
  if (!ref.trim()) return undefined;
  const host = imageHost(ref);
  const hub = DOCKER_HUB.has(host);
  return registries.find((r) => {
    const h = registryHost(r);
    return h === host || (hub && DOCKER_HUB.has(h));
  });
}

/**
 * Pick the image to pull. The registry comes first: "Detect from the image
 * name" (default) takes a typed reference and says which stored registry's
 * credentials the pull will use; a stored registry lets you browse its
 * repositories and tags and builds the reference.
 */
export function RegistryImagePicker({
  registries, registryId, onRegistryChange, value, onChange, error, disabled,
}: {
  /** Undefined when the user may not read the registry list (non-admins):
   * the server still picks the credentials, the picker just cannot say which. */
  registries?: Registry[];
  registryId: string;
  onRegistryChange: (id: string) => void;
  value: string;
  onChange: (ref: string) => void;
  error?: string | null;
  disabled?: boolean;
}) {
  const list = registries ?? [];
  const registry = list.find((r) => r.id === registryId);
  const detected = registry ? undefined : detectRegistry(value, list);

  return (
    <>
      {registries && <Select
        label="Registry"
        disabled={disabled}
        value={registryId || "__auto"}
        onChange={(v) => {
          onRegistryChange(v === "__auto" ? "" : v);
          onChange("");
        }}
        options={[
          { value: "__auto", label: "Detect from the image name" },
          ...list.map((r) => ({ value: r.id, label: `${r.name} (${registryHost(r)})` })),
        ]}
      />}
      {registry ? (
        <BrowseRegistry registry={registry} value={value} onChange={onChange} error={error} disabled={disabled} />
      ) : (
        <>
          <TextInput
            label="Image reference"
            placeholder="nginx:latest"
            data-autofocus
            disabled={disabled}
            value={value}
            errorText={error ?? undefined}
            onChange={(e) => onChange(e.target.value)}
          />
          {registries && value.trim() && (
            <Text size="xs" color="muted">
              {detected
                ? <>Pulled with the credentials of <b>{detected.name}</b> ({registryHost(detected)}).</>
                : <>No stored registry for <b>{imageHost(value)}</b>: pulled anonymously.</>}
            </Text>
          )}
        </>
      )}
    </>
  );
}

function BrowseRegistry({ registry, value, onChange, error, disabled }: {
  registry: Registry; value: string; onChange: (ref: string) => void; error?: string | null; disabled?: boolean;
}) {
  const host = registryHost(registry);
  const prefix = `${host}/`;
  const [repo, tag] = useMemo(() => {
    if (!value.startsWith(prefix)) return ["", ""];
    const rest = value.slice(prefix.length);
    const i = rest.lastIndexOf(":");
    return i > 0 ? [rest.slice(0, i), rest.slice(i + 1)] : [rest, ""];
  }, [value, prefix]);
  const set = (r: string, t: string) => onChange(r ? `${prefix}${r}${t ? `:${t}` : ""}` : "");

  const catalog = useQuery({
    queryKey: ["registry-catalog", registry.id],
    queryFn: () => registryCatalog(registry.id),
    staleTime: 5 * 60 * 1000,
    retry: false,
  });
  const tags = useQuery({
    queryKey: ["registry-tags", registry.id, repo],
    queryFn: () => registryTags(registry.id, repo),
    enabled: !!repo,
    staleTime: 60 * 1000,
    retry: false,
  });
  const tagList = useMemo(() => {
    const t = [...(tags.data ?? [])];
    t.sort((a, b) => (a === "latest" ? -1 : b === "latest" ? 1 : b.localeCompare(a, undefined, { numeric: true })));
    return t;
  }, [tags.data]);
  // The newest tag is picked once a repository's tags arrive.
  useEffect(() => {
    if (repo && !tag && tagList.length) set(repo, tagList[0]);
  }, [repo, tagList]); // eslint-disable-line react-hooks/exhaustive-deps

  const noCatalog = catalog.isError || (catalog.isSuccess && catalog.data.length === 0);
  return (
    <>
      {noCatalog ? (
        <TextInput
          label="Repository"
          placeholder="team/app"
          disabled={disabled}
          description={catalog.isError ? `${registry.name} does not list its repositories (${errorMessage(catalog.error, "")}); type one.` : "No repositories listed; type one."}
          value={repo}
          onChange={(e) => set(e.target.value.trim(), "")}
        />
      ) : (
        <Select
          label="Repository"
          searchable
          loading={catalog.isLoading}
          disabled={disabled || catalog.isLoading}
          placeholder={catalog.isLoading ? "Loading…" : "Pick a repository"}
          value={repo || null}
          onChange={(v) => set(v, "")}
          options={(catalog.data ?? []).map((r) => ({ value: r, label: r }))}
        />
      )}
      {repo && (
        tags.isError ? (
          <TextInput label="Tag" placeholder="latest" disabled={disabled} value={tag}
            description={`Could not list the tags: ${errorMessage(tags.error, "")}`}
            onChange={(e) => set(repo, e.target.value.trim())} />
        ) : (
          <Select
            label="Tag"
            searchable
            loading={tags.isLoading}
            disabled={disabled || tags.isLoading}
            value={tag || null}
            onChange={(v) => set(repo, v)}
            options={tagList.map((t) => ({ value: t, label: t }))}
            errorText={error ?? undefined}
          />
        )
      )}
      {value && (
        <Text size="xs" color="muted">Pulls <Code>{value}</Code> with the credentials of <b>{registry.name}</b>.</Text>
      )}
    </>
  );
}
