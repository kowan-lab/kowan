import { Check } from "lucide-react";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  Box,
  Icon,
  InputToggle,
  Spinner,
  Card,
  Tabs,
  Text,
  TextInput,
} from "@/ui/kit";

import PageHeader from "../components/PageHeader";
import LogHistory from "../components/logs/LogHistory";
import LogViewer from "../components/logs/LogViewer";
import type { LogSource } from "../components/logs/logStream";
import { listContainers, logsWebSocketURL } from "../api/client";
import { isKowanContainer, useFeatures } from "../lib/features";
import { useHost, isAllHosts } from "../HostContext";
import { useHostScopedList, type WithHost } from "../useHostScopedList";
import type { Container } from "../types";

// ---------------------------------------------------------------------------
// Logs: a cross-host log viewer. Pick one or more containers from
// the current host (or every host in the "All hosts" view); their logs are
// merged into one live stream by the shared <LogViewer>, which owns all the
// streaming, filtering and rendering. This page only owns the container picker.
// The History mode (?mode=history) searches the retained logs instead.
// ---------------------------------------------------------------------------

interface Selection {
  host: string;
  hostName: string;
  containerId: string;
  containerName: string;
}

function streamKey(host: string, containerId: string): string {
  return `${host}/${containerId}`;
}

function containerName(c: Container): string {
  const n = (c.names && c.names[0]) || "";
  return n.replace(/^\//, "") || c.id.slice(0, 12);
}

export default function Logs() {
  const [params, setParams] = useSearchParams();
  const history = params.get("mode") === "history";
  const setMode = (m: string) =>
    setParams((p) => {
      const n = new URLSearchParams(p);
      if (m === "history") n.set("mode", "history");
      else {
        n.delete("mode");
        n.delete("workload");
      }
      return n;
    }, { replace: true });

  return (
    // Full-height on wide screens (the viewer scrolls inside); on a phone the
    // page scrolls and the viewer gets a tall box of its own.
    <div className="flex flex-col gap-10 lg:h-[calc(100vh-120px)]">
      <PageHeader
        title="Logs"
        description={history
          ? "Search the logs kept by workloads, across hosts, restarts and redeploys."
          : "Live container logs across hosts. Pick one or more containers to merge their output into a single searchable stream."}
        icon={<Icon name="description-outline-rounded" size="lg" />}
      />
      <Tabs variant="segmented" value={history ? "history" : "live"} onValueChange={setMode}>
        <Tabs.List>
          <Tabs.Trigger value="live">Live</Tabs.Trigger>
          <Tabs.Trigger value="history">History</Tabs.Trigger>
        </Tabs.List>
      </Tabs>
      {history ? <LogHistory initialWorkload={params.get("workload") ?? undefined} /> : <LiveLogs />}
    </div>
  );
}

function LiveLogs() {
  const { hostId } = useHost();
  const aggregated = isAllHosts(hostId);

  const showSystem = useFeatures().systemContainers;
  const containersQuery = useHostScopedList<Container>(
    "containers",
    hostId,
    (h) => listContainers(h, true),
    { refetchInterval: 8000 },
  );
  const containers = useMemo(
    () => (showSystem ? containersQuery.data ?? [] : (containersQuery.data ?? []).filter((c) => !isKowanContainer(c.labels))),
    [containersQuery.data, showSystem],
  );

  // Container picker state.
  const [filter, setFilter] = useState("");
  const [runningOnly, setRunningOnly] = useState(true);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);

  const rowHost = (c: Container & WithHost): { id: string; name: string } => ({
    id: c._hostId ?? c.hostId ?? hostId,
    name: c._hostName ?? c.hostId ?? hostId,
  });

  const visibleContainers = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return containers.filter((c) => {
      if (runningOnly && c.state !== "running") return false;
      if (!q) return true;
      return (
        containerName(c).toLowerCase().includes(q) ||
        c.image.toLowerCase().includes(q)
      );
    });
  }, [containers, filter, runningOnly]);

  // Resolve selected keys back to full Selection objects (drop any that no
  // longer exist). The order of `selected` drives per-container tag colours.
  const selected: Selection[] = useMemo(() => {
    const byKey = new Map<string, Selection>();
    for (const c of containers) {
      const h = rowHost(c);
      byKey.set(streamKey(h.id, c.id), {
        host: h.id,
        hostName: h.name,
        containerId: c.id,
        containerName: containerName(c),
      });
    }
    return selectedKeys
      .map((k) => byKey.get(k))
      .filter((s): s is Selection => Boolean(s));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKeys, containers]);

  // Map the selection to abstract log sources for the shared viewer.
  const sources: LogSource[] = useMemo(
    () =>
      selected.map((s) => ({
        key: streamKey(s.host, s.containerId),
        label: s.containerName,
        hostName: s.hostName,
        url: (opts) => logsWebSocketURL(s.host, s.containerId, opts),
      })),
    [selected],
  );

  const toggle = (key: string) =>
    setSelectedKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );

  return (
      <div className="flex min-h-0 flex-1 flex-col gap-8 lg:flex-row">
        {/* Container picker */}
        <Card
          variant="raised"
          className="flex max-h-[45vh] flex-col lg:max-h-none lg:w-[300px] lg:shrink-0"
        >
          <Card.Body style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
            <Box direction="column" gap={2} style={{ minHeight: 0, flex: 1 }}>
              <TextInput
                size="sm"
                placeholder="Filter containers…"
                iconLeft="search-rounded"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
              <Box direction="row" wrap justify="between">
                <InputToggle
                  size="sm"
                  variant="inline"
                  label="Running only"
                  checked={runningOnly}
                  onChange={(e) => setRunningOnly(e.target.checked)}
                />
                {/* Always laid out (hidden when empty) so selecting never shifts the panel. */}
                <button
                  type="button"
                  onClick={() => setSelectedKeys([])}
                  disabled={selected.length === 0}
                  aria-hidden={selected.length === 0}
                  tabIndex={selected.length === 0 ? -1 : 0}
                  className={
                    "h-14 px-2 text-[13px] font-semibold text-[var(--link)] hover:underline disabled:pointer-events-none " +
                    (selected.length === 0 ? "invisible" : "")
                  }
                >
                  Clear ({selected.length})
                </button>
              </Box>

              {containersQuery.isLoading ? (
                <Box direction="row" wrap gap={2} padding={3}>
                  <Spinner size="sm" />
                  <Text size="sm" color="muted">
                    Loading containers…
                  </Text>
                </Box>
              ) : visibleContainers.length === 0 ? (
                <Text size="sm" color="muted" style={{ padding: "12px" }}>
                  No containers match.
                </Text>
              ) : (
                <Box style={{ flex: 1, minHeight: 0, overflowY: "auto", width: "100%" }}>
                  <Box direction="column" gap={0} style={{ width: "100%" }}>
                    {visibleContainers.map((c) => {
                      const h = rowHost(c);
                      const key = streamKey(h.id, c.id);
                      const checked = selectedKeys.includes(key);
                      return (
                        <div
                          key={key}
                          role="checkbox"
                          aria-checked={checked}
                          tabIndex={0}
                          onClick={() => toggle(key)}
                          onKeyDown={(e) => {
                            if (e.key === " " || e.key === "Enter") {
                              e.preventDefault();
                              toggle(key);
                            }
                          }}
                          className={
                            "flex min-h-20 w-full cursor-pointer items-center gap-4 px-4 py-2 outline-none select-none hover:bg-accent focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-signal " +
                            (checked ? "bg-[var(--tone-primary-soft)] shadow-[inset_2px_0_0_var(--signal)] hover:bg-[var(--tone-primary-soft)]" : "")
                          }
                        >
                          {/* Visual tick only; the row is the control (role=checkbox). */}
                          <span
                            aria-hidden
                            className={
                              "flex size-8 shrink-0 items-center justify-center border " +
                              (checked ? "border-foreground bg-foreground text-background" : "border-[var(--input)] bg-[var(--popover)]")
                            }
                          >
                            {checked && <Check className="size-[12px]" strokeWidth={2.5} />}
                          </span>
                          <span
                            aria-hidden
                            className={"size-4 shrink-0 rounded-full " + (c.state === "running" ? "bg-[var(--tone-green-solid)]" : "bg-[var(--tone-gray-solid)]")}
                          />
                          <span className="flex min-w-0 flex-1 flex-col">
                            <span className="truncate text-[13px] leading-[18px]">{containerName(c)}</span>
                            {aggregated && (
                              <span className="truncate font-mono text-[12px] leading-[16px] text-muted-foreground">{h.name}</span>
                            )}
                          </span>
                        </div>
                      );
                    })}
                  </Box>
                </Box>
              )}
            </Box>
          </Card.Body>
        </Card>

        {/* Log viewer */}
        <Card
          variant="raised"
          className="flex h-[75vh] min-w-0 flex-1 flex-col lg:h-auto"
        >
          <Card.Body style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
            <LogViewer
              sources={sources}
              aggregated={aggregated}
              emptyTitle="Pick a container"
              emptyDescription="Select one or more containers in the list to start streaming their logs here."
            />
          </Card.Body>
        </Card>
      </div>
  );
}
