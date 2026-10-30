import { useEffect, useMemo, useState } from "react";
import { errorMessage } from "../utils";
import {
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Code,
  Icon,
  Select,
  Skeleton,
  Table,
  Text,
  TextInput,
  Tooltip,
} from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";
import { listRegistries, registryCatalog, registryTags } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import type { Registry } from "../types";
import { copyText } from "../lib/clipboard";


/** Strip a scheme and trailing slash so a ref reads like "host/repo:tag". */
function registryHost(url: string): string {
  return url.replace(/^https?:\/\//, "").replace(/\/+$/, "");
}

/** Icon-only copy-to-clipboard button. */
function CopyRefButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    void copyText(value).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    });
  };
  return (
    <Tooltip content={copied ? "Copied" : "Copy pull reference"}>
      <Button
        variant="clear"
        color={copied ? "teal" : "neutral"}
        aria-label="Copy pull reference"
        onClick={copy}
        iconLeft={
          copied
            ? "check-rounded"
            : "content-copy-outline-rounded"
        }
      />
    </Tooltip>
  );
}

export default function RegistryExplorer() {
  const { user: currentUser } = useAuth();
  const isAdmin = currentUser?.role === "admin";
  const [params, setParams] = useSearchParams();
  const registriesQuery = useQuery({
    queryKey: ["registries"],
    queryFn: listRegistries,
    enabled: isAdmin,
  });

  const paramId = params.get("id") ?? "";
  const [registryId, setRegistryId] = useState<string>(paramId);
  useEffect(() => {
    if (!registryId && registriesQuery.data && registriesQuery.data.length > 0) {
      setRegistryId(registriesQuery.data[0].id);
    }
  }, [registryId, registriesQuery.data]);

  const selectRegistry = (id: string) => {
    setRegistryId(id);
    const next = new URLSearchParams(params);
    if (id) next.set("id", id);
    else next.delete("id");
    setParams(next, { replace: true });
  };

  const registry: Registry | undefined = useMemo(
    () => registriesQuery.data?.find((r) => r.id === registryId),
    [registriesQuery.data, registryId],
  );

  const options = useMemo(
    () =>
      (registriesQuery.data ?? []).map((r) => ({
        value: r.id,
        label: `${r.name} (${registryHost(r.url)})`,
      })),
    [registriesQuery.data],
  );

  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Registry Explorer"
        description="Browse the repositories and tags published in a container registry."
        icon={<Icon name="visibility-outline-rounded" size="lg" />}
        actions={
          <Select
            placeholder="Registry"
            options={options}
            value={registryId || undefined}
            onChange={(v) => selectRegistry(v ?? "")}
            searchable
            width={320}
          />
        }
      />

      {!isAdmin ? (
        <Callout color="danger" title="Access denied">
          You need administrator privileges to browse registries.
        </Callout>
      ) : registriesQuery.isLoading ? (
        <Skeleton height={200} />
      ) : registriesQuery.isError ? (
        <Callout color="danger" title="Failed to load registries">
          {errorMessage(registriesQuery.error, "Could not load the registry list.")}
        </Callout>
      ) : (registriesQuery.data ?? []).length === 0 ? (
        <Card variant="raised">
          <Card.Body>
            <EmptyState
              icon={<Icon name="visibility-outline-rounded" size="xl" />}
              title="No registries configured"
              description="Add a registry under Settings → Registries to browse its repositories and tags."
            />
          </Card.Body>
        </Card>
      ) : !registry ? (
        <Card variant="raised">
          <Card.Body>
            <EmptyState
              icon={<Icon name="visibility-outline-rounded" size="xl" />}
              title="Pick a registry"
              description="Choose a registry above to list its repositories."
            />
          </Card.Body>
        </Card>
      ) : (
        <ExplorerBody key={registry.id} registry={registry} />
      )}
    </Box>
  );
}

function ExplorerBody({ registry }: { registry: Registry }) {
  const [search, setSearch] = useState("");
  const [selectedRepo, setSelectedRepo] = useState<string | null>(null);
  const [manualRepos, setManualRepos] = useState<string[]>([]);
  const [manualInput, setManualInput] = useState("");

  const catalogQuery = useQuery({
    queryKey: ["registry-catalog", registry.id],
    queryFn: () => registryCatalog(registry.id),
    retry: false,
  });

  // The catalog endpoint is optional (Docker Hub has none; a registry may also be
  // empty or scope-limited). Offer the manual repository entry whenever the catalog
  // yields nothing — on error OR on a successful-but-empty list — so the operator
  // can always type a name and browse its tags.
  const catalogUnsupported = catalogQuery.isError;
  const catalogEmpty =
    !catalogQuery.isLoading && (catalogQuery.data?.length ?? 0) === 0;
  const showManualAdd = catalogUnsupported || catalogEmpty;
  const repos = useMemo(() => {
    const fromCatalog = catalogQuery.data ?? [];
    const merged = [...new Set([...manualRepos, ...fromCatalog])];
    const needle = search.trim().toLowerCase();
    const filtered = needle
      ? merged.filter((r) => r.toLowerCase().includes(needle))
      : merged;
    return filtered.sort((a, b) => a.localeCompare(b));
  }, [catalogQuery.data, manualRepos, search]);

  const addManualRepo = () => {
    const r = manualInput.trim().replace(/^\/+|\/+$/g, "");
    if (!r) return;
    setManualRepos((prev) => (prev.includes(r) ? prev : [...prev, r]));
    setManualInput("");
    setSelectedRepo(r);
  };

  return (
    <Card variant="raised" style={{ overflow: "hidden" }}>
      <Box
        direction="row"
        align="stretch"
        gap={0}
        wrap="nowrap"
        style={{ minHeight: "calc(100vh - 260px)" }}
      >
        {/* Repository list */}
        <Box
          style={{
            flex: "0 0 42%",
            borderRight: "1px solid var(--border)",
            minWidth: 0,
          }}
        >
          <Box
            direction="column"
            gap={2}
            padding={3}
            style={{ height: "calc(100vh - 260px)" }}
          >
            <Box direction="row" wrap justify="between" align="center">
              <Text weight="semibold" size="sm">
                Repositories
              </Text>
              <Box direction="row" wrap gap={2} align="center">
                <Badge variant="light" color="neutral">
                  {repos.length}
                </Badge>
                <Tooltip content="Refresh">
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label="Refresh catalog"
                    loading={catalogQuery.isFetching}
                    onClick={() => void catalogQuery.refetch()}
                    iconLeft="refresh-rounded"
                  />
                </Tooltip>
              </Box>
            </Box>

            <TextInput
              placeholder="Filter repositories"
              iconLeft="search-rounded"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              size="sm"
            />

            {showManualAdd && (
              <Callout color="warning">
                <Text size="xs">
                  {catalogUnsupported
                    ? "This registry does not expose a repository catalog. Enter a repository name to browse its tags."
                    : "No repositories were listed for this registry. Enter a repository name to browse its tags."}
                </Text>
                <Box direction="row" gap={2} wrap="nowrap" style={{ marginTop: 8 }}>
                  <TextInput
                    placeholder="library/nginx"
                    value={manualInput}
                    onChange={(e) => setManualInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addManualRepo();
                    }}
                    size="sm"
                    style={{ flex: 1 }}
                  />
                  <Button size="sm" variant="light" onClick={addManualRepo}>
                    Add
                  </Button>
                </Box>
              </Callout>
            )}

            <Box style={{ height: "100%", overflow: "auto" }}>
              {catalogQuery.isLoading ? (
                <Box direction="column" gap={2}>
                  {Array.from({ length: 8 }).map((_, i) => (
                    <Skeleton key={i} height={26} />
                  ))}
                </Box>
              ) : repos.length === 0 ? (
                <Text size="sm" color="muted" style={{ padding: 8 }}>
                  {showManualAdd
                    ? "No repositories added yet."
                    : "No repositories found."}
                </Text>
              ) : (
                <Table hoverable>
                  <Table.Body>
                    {repos.map((repo) => {
                      const isSelected = repo === selectedRepo;
                      return (
                        <Table.Row
                          key={repo}
                          selected={isSelected}
                          color={isSelected ? "blue" : undefined}
                          style={{ cursor: "pointer" }}
                          onClick={() => setSelectedRepo(repo)}
                        >
                          <Table.Cell shrink>
                            <Icon
                              name="package-2-outline"
                              color="blue"
                            />
                          </Table.Cell>
                          <Table.Cell>
                            <Text size="sm">{repo}</Text>
                          </Table.Cell>
                        </Table.Row>
                      );
                    })}
                  </Table.Body>
                </Table>
              )}
            </Box>
          </Box>
        </Box>

        {/* Tags pane */}
        <Box style={{ flex: "1 1 58%", minWidth: 0 }}>
          {selectedRepo ? (
            <TagList registry={registry} repo={selectedRepo} />
          ) : (
            <Box padding={6}>
              <EmptyState
                icon={<Icon name="sell-outline" size="xl" />}
                title="Pick a repository"
                description="Select a repository on the left to list its tags."
              />
            </Box>
          )}
        </Box>
      </Box>
    </Card>
  );
}

function TagList({ registry, repo }: { registry: Registry; repo: string }) {
  const tagsQuery = useQuery({
    queryKey: ["registry-tags", registry.id, repo],
    queryFn: () => registryTags(registry.id, repo),
    retry: false,
  });

  const host = registryHost(registry.url);
  const tags = tagsQuery.data ?? [];

  return (
    <Box
      direction="column"
      gap={2}
      padding={3}
      style={{ height: "calc(100vh - 260px)" }}
    >
      <Box direction="row" wrap justify="between" align="center">
        <Box direction="row" wrap gap={2} align="center" style={{ minWidth: 0 }}>
          <Icon name="package-2-outline" />
          <Text weight="semibold" size="sm" truncate>
            {repo}
          </Text>
        </Box>
        <Box direction="row" wrap gap={2} align="center">
          <Badge variant="light" color="neutral">
            {tags.length} tags
          </Badge>
          <Tooltip content="Refresh">
            <Button
              variant="outline"
              size="sm"
              aria-label="Refresh tags"
              loading={tagsQuery.isFetching}
              onClick={() => void tagsQuery.refetch()}
              iconLeft="refresh-rounded"
            />
          </Tooltip>
        </Box>
      </Box>

      <Box style={{ height: "100%", overflow: "auto" }}>
        {tagsQuery.isLoading ? (
          <Box direction="column" gap={2}>
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} height={28} />
            ))}
          </Box>
        ) : tagsQuery.isError ? (
          <Callout color="danger">
            {errorMessage(tagsQuery.error, "Failed to list tags.")}
          </Callout>
        ) : tags.length === 0 ? (
          <Text size="sm" color="muted" style={{ padding: 8 }}>
            No tags in this repository.
          </Text>
        ) : (
          <Table hoverable>
            <Table.Body>
              {tags.map((tag) => {
                const ref = `${host}/${repo}:${tag}`;
                return (
                  <Table.Row key={tag}>
                    <Table.Cell shrink>
                      <Icon name="sell-outline" color="teal" />
                    </Table.Cell>
                    <Table.Cell>
                      <Text size="sm" weight="medium">
                        {tag}
                      </Text>
                    </Table.Cell>
                    <Table.Cell>
                      <Code>{ref}</Code>
                    </Table.Cell>
                    <Table.Cell shrink>
                      <CopyRefButton value={ref} />
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </Table>
        )}
      </Box>
    </Box>
  );
}
