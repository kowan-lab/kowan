// Logs tab: streams the deployment's logs websocket from its single host. A
// compose deployment picks one service (pre-selected when the operator came
// from a container row's "Logs" action).

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Select, Text } from "@/ui/kit";
import { deploymentLogsWebSocketURL, listHosts } from "../../api/client";
import LogViewer from "../logs/LogViewer";
import type { LogSource } from "../logs/logStream";
import type { Deployment } from "../../types";
import { errorMessage } from "../../utils";
import { deploymentHost, useDeploymentServices } from "./shared";

export default function DeploymentLogs({
  deployment: d,
  service,
  onServiceChange,
}: {
  deployment: Deployment;
  service: string | null;
  onServiceChange: (s: string | null) => void;
}) {
  const hostId = deploymentHost(d);
  const isCompose = d.type === "compose";
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hostLabel = hostsQuery.data?.find((h) => h.id === hostId)?.name || hostId || "";
  const servicesQuery = useDeploymentServices(d);
  const services = useMemo(() => servicesQuery.data ?? [], [servicesQuery.data]);

  // Keep the selection valid: fall back to the first service with a container.
  useEffect(() => {
    if (!isCompose || services.length === 0) return;
    if (!service || !services.some((s) => s.service === service)) {
      onServiceChange((services.find((s) => s.containerId) ?? services[0]).service);
    }
  }, [isCompose, services, service, onServiceChange]);

  const containerId = isCompose ? services.find((s) => s.service === service)?.containerId : undefined;
  const ready = !!hostId && (!isCompose || !!containerId);

  const sources = useMemo<LogSource[]>(() => {
    if (!ready || !hostId) return [];
    return [
      {
        key: `${d.id}/${hostId}/${containerId ?? ""}`,
        label: isCompose ? service ?? "service" : d.name,
        url: (opts) => deploymentLogsWebSocketURL(d.id, hostId, { ...opts, container: containerId }),
      },
    ];
  }, [ready, d.id, d.name, hostId, containerId, isCompose, service]);

  // Say why nothing streams instead of a dead end.
  let emptyTitle = "No host";
  let emptyDescription = "Pick a host in Configuration and deploy to see logs.";
  if (hostId && isCompose) {
    if (servicesQuery.isLoading) {
      emptyTitle = "Loading services…";
      emptyDescription = "Finding this deployment's containers.";
    } else if (servicesQuery.isError) {
      emptyTitle = "Couldn't list the services";
      emptyDescription = errorMessage(servicesQuery.error, `${hostLabel} did not answer.`);
    } else if (services.length === 0) {
      emptyTitle = "No containers";
      emptyDescription = `Nothing of this deployment exists on ${hostLabel}. Deploy it, then its logs stream here.`;
    } else if (!containerId) {
      emptyTitle = "This service has no container";
      emptyDescription = "Pick another service, or redeploy to create it.";
    }
  }

  const options = services.map((s) => ({
    value: s.service,
    label: s.state && s.state !== "running" ? `${s.service} (${s.state})` : s.service,
  }));

  return (
    <div className="flex flex-col gap-5 shadow-[inset_0_0_0_1px_var(--border)] bg-card px-[var(--page-px,16px)] py-5">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
        {isCompose && (
          <Select
            label="Service"
            width={240}
            options={options}
            value={service ?? undefined}
            onChange={(v) => onServiceChange(v)}
            placeholder={servicesQuery.isLoading ? "Loading…" : "No services"}
            disabled={options.length === 0}
          />
        )}
        <Text size="sm" color="muted" style={{ paddingBottom: 4 }}>
          {hostId ? `Streaming from ${hostLabel}` : "No host"}
        </Text>
      </div>
      <LogViewer sources={sources} height="max(360px, calc(100dvh - 340px))" emptyTitle={emptyTitle} emptyDescription={emptyDescription} />
    </div>
  );
}
