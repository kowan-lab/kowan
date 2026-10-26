import { useId, useMemo, useState } from "react";
import {
  Box,
  Button,
  Icon,
  Popover,
  Select,
  Spinner,
  Text,
  TextInput,
  Tooltip,
} from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";

import {
  listContainers,
  listImages,
  listRegistries,
  registryCatalog,
  registryTags,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ALL_HOSTS, useHost } from "../HostContext";
import type { Registry } from "../types";

interface ImageInputProps {
  value: string;
  onChange: (value: string) => void;
  label?: React.ReactNode;
  description?: React.ReactNode;
  placeholder?: string;
  error?: React.ReactNode;
  required?: boolean;
  withAsterisk?: boolean;
  disabled?: boolean;
  /** Override the host whose local images seed the suggestions. Defaults to the
   *  globally selected host. */
  hostId?: string;
}

/**
 * ImageInput is the shared image-reference field used everywhere an image is
 * entered. It is a free-text combobox (the value is always the canonical
 * `repo[:tag][@digest]` string — never split into separate fields) with an
 * autocomplete fed by the images already present / in use on the selected host,
 * plus an optional registry browser (registry → repository → tag) that fills the
 * field with the fully-qualified reference.
 */
export default function ImageInput({
  value,
  onChange,
  label = "Image",
  description,
  placeholder = "nginx:latest",
  error,
  required,
  withAsterisk,
  disabled,
  hostId: hostIdProp,
}: ImageInputProps) {
  const { hostId: selectedHostId } = useHost();
  const hostId = hostIdProp ?? selectedHostId;
  const concreteHost = !!hostId && hostId !== ALL_HOSTS;

  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  // Local images already pulled on the host — the cheapest, most relevant
  // source (offline, no registry calls).
  const imagesQuery = useQuery({
    queryKey: ["images", hostId],
    queryFn: () => listImages(hostId),
    enabled: concreteHost,
    staleTime: 30_000,
  });

  // Images currently in use by containers on the host (a "recently used" proxy).
  const containersQuery = useQuery({
    queryKey: ["containers", hostId],
    queryFn: () => listContainers(hostId),
    enabled: concreteHost,
    staleTime: 30_000,
  });

  const suggestions = useMemo(() => {
    const set = new Set<string>();
    for (const img of imagesQuery.data ?? []) {
      for (const tag of img.repoTags ?? []) {
        if (tag && !tag.includes("<none>")) set.add(tag);
      }
    }
    for (const c of containersQuery.data ?? []) {
      if (c.image && !c.image.includes("<none>")) set.add(c.image);
    }
    return Array.from(set).sort();
  }, [imagesQuery.data, containersQuery.data]);

  const [browserOpen, setBrowserOpen] = useState(false);

  // Free-text suggestions are provided via a native <datalist> so the field
  // stays a free-text combobox (the value is never forced to be one of the
  // suggestions).
  const listId = useId();

  const browseButton = isAdmin ? (
    <Popover
      open={browserOpen}
      onOpenChange={setBrowserOpen}
      placement="bottom-end"
    >
      <Popover.Trigger>
        <Tooltip content="Browse a registry">
          <Button
            variant="clear"
            color="neutral"
            aria-label="Browse a registry"
            onClick={() => setBrowserOpen((o) => !o)}
            disabled={disabled}
            iconLeft="database-search-outline-rounded"
          />
        </Tooltip>
      </Popover.Trigger>
      <Popover.Content noPadding>
        <RegistryBrowser
          onPick={(ref) => {
            onChange(ref);
            setBrowserOpen(false);
          }}
        />
      </Popover.Content>
    </Popover>
  ) : undefined;

  return (
    <>
      <TextInput
        label={label}
        description={description}
        placeholder={placeholder}
        errorText={error}
        required={required || withAsterisk}
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        list={listId}
        fullWidth
        suffix={browseButton}
        suffixType="inline"
      />
      <datalist id={listId}>
        {suggestions.slice(0, 12).map((s) => (
          <option key={s} value={s} />
        ))}
      </datalist>
    </>
  );
}

/** Strip the scheme and trailing slash from a registry URL to get the host
 *  prefix used in an image reference. */
function registryHost(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/+$/, "");
}

/**
 * RegistryBrowser is the registry → repository → tag picker shown in the
 * ImageInput popover. Catalog/tags are fetched on demand (once per
 * registry/repo, filtered client-side) so typing never hits the registry.
 */
function RegistryBrowser({ onPick }: { onPick: (ref: string) => void }) {
  const registriesQuery = useQuery({
    queryKey: ["registries"],
    queryFn: listRegistries,
    staleTime: 60_000,
  });
  const registries = registriesQuery.data ?? [];

  const [registryId, setRegistryId] = useState<string | null>(null);
  const [repoFilter, setRepoFilter] = useState("");
  const [repo, setRepo] = useState<string | null>(null);

  const registry: Registry | undefined = registries.find((r) => r.id === registryId);

  const catalogQuery = useQuery({
    queryKey: ["registry-catalog", registryId],
    queryFn: () => registryCatalog(registryId as string),
    enabled: !!registryId,
  });

  const tagsQuery = useQuery({
    queryKey: ["registry-tags", registryId, repo],
    queryFn: () => registryTags(registryId as string, repo as string),
    enabled: !!registryId && !!repo,
  });

  const filteredRepos = useMemo(() => {
    const repos = catalogQuery.data ?? [];
    const q = repoFilter.trim().toLowerCase();
    return (q ? repos.filter((r) => r.toLowerCase().includes(q)) : repos).slice(0, 50);
  }, [catalogQuery.data, repoFilter]);

  const pick = (tag: string) => {
    if (!registry || !repo) return;
    onPick(`${registryHost(registry.url)}/${repo}:${tag}`);
  };

  // Stage 3: tags of the chosen repository.
  if (registryId && repo) {
    return (
      <Box direction="column" gap={0} style={{ width: 340 }}>
        <Box direction="row" wrap="nowrap" gap={2} align="center" px={3} py={2}>
          <Button
            variant="clear"
            color="neutral"
            size="sm"
            aria-label="Back"
            onClick={() => setRepo(null)}
            iconLeft="arrow-back-rounded"
          />
          <Text variant="body" size="sm" weight="medium" truncate>
            {repo}
          </Text>
        </Box>
        <Box style={{ borderTop: "1px solid var(--border)" }} />
        {tagsQuery.isLoading ? (
          <Box direction="row" justify="center" py={4}>
            <Spinner size="sm" />
          </Box>
        ) : tagsQuery.isError ? (
          <Text variant="body" size="sm" color="danger" style={{ padding: 12 }}>
            Could not list tags.
          </Text>
        ) : (tagsQuery.data ?? []).length === 0 ? (
          <Text variant="body" size="sm" color="muted" style={{ padding: 12 }}>
            No tags found.
          </Text>
        ) : (
          <Box style={{ maxHeight: 240, overflowY: "auto" }}>
            <Box direction="column" gap={0}>
              {(tagsQuery.data ?? []).map((tag) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => pick(tag)}
                  style={rowStyle}
                >
                  <Box direction="row" wrap="nowrap" gap={2} align="center">
                    <Icon
                      name="sell-outline"
                      size="sm"
                      style={{ opacity: 0.6 }}
                    />
                    <Text variant="body" size="sm" truncate>
                      {tag}
                    </Text>
                  </Box>
                </button>
              ))}
            </Box>
          </Box>
        )}
      </Box>
    );
  }

  // Stage 2: repositories of the chosen registry.
  if (registryId) {
    return (
      <Box direction="column" gap={0} style={{ width: 340 }}>
        <Box direction="row" wrap="nowrap" gap={2} align="center" px={3} py={2}>
          <Button
            variant="clear"
            color="neutral"
            size="sm"
            aria-label="Back"
            onClick={() => {
              setRegistryId(null);
              setRepoFilter("");
            }}
            iconLeft="arrow-back-rounded"
          />
          <Text variant="body" size="sm" weight="medium" truncate>
            {registry?.name}
          </Text>
        </Box>
        <Box style={{ borderTop: "1px solid var(--border)" }} />
        <Box padding={2}>
          <TextInput
            size="sm"
            placeholder="Filter repositories…"
            value={repoFilter}
            onChange={(e) => setRepoFilter(e.target.value)}
            fullWidth
          />
        </Box>
        {catalogQuery.isLoading ? (
          <Box direction="row" justify="center" py={4}>
            <Spinner size="sm" />
          </Box>
        ) : catalogQuery.isError ? (
          <Box direction="column" gap={2} padding={3}>
            <Text variant="body" size="sm" color="muted">
              This registry doesn't support listing repositories. Type the
              repository name to load its tags.
            </Text>
            <Box direction="row" wrap="nowrap" gap={2} align="center">
              <TextInput
                size="sm"
                placeholder="org/app"
                value={repoFilter}
                onChange={(e) => setRepoFilter(e.target.value)}
                style={{ flex: 1 }}
              />
              <Button
                size="sm"
                color="info"
                disabled={!repoFilter.trim()}
                onClick={() => setRepo(repoFilter.trim())}
              >
                Tags
              </Button>
            </Box>
          </Box>
        ) : filteredRepos.length === 0 ? (
          <Text variant="body" size="sm" color="muted" style={{ padding: 12 }}>
            No repositories.
          </Text>
        ) : (
          <Box style={{ maxHeight: 240, overflowY: "auto" }}>
            <Box direction="column" gap={0}>
              {filteredRepos.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRepo(r)}
                  style={rowStyle}
                >
                  <Text variant="body" size="sm" truncate>
                    {r}
                  </Text>
                </button>
              ))}
            </Box>
          </Box>
        )}
      </Box>
    );
  }

  // Stage 1: pick a registry.
  return (
    <Box direction="column" gap={2} padding={3} style={{ width: 340 }}>
      <Text variant="body" size="sm" weight="medium">
        Browse a registry
      </Text>
      {registries.length === 0 ? (
        <Text variant="body" size="sm" color="muted">
          No registries configured. Add one on the Registries page.
        </Text>
      ) : (
        <Select
          placeholder="Select a registry"
          options={registries.map((r) => ({ value: r.id, label: r.name }))}
          value={registryId ?? ""}
          onChange={(v: string) => setRegistryId(v || null)}
          searchable
          fullWidth
        />
      )}
    </Box>
  );
}

const rowStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  padding: "6px 12px",
  background: "transparent",
  border: "none",
  textAlign: "left",
  cursor: "pointer",
};
