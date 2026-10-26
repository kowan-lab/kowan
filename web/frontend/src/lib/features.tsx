// Platform features an admin can turn off in Settings (kowan/features.py).
// While one is off its navigation, links and pages are hidden; its API answers
// 404. "systemContainers" only shows or hides Kowan's own containers.
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { getFeatures } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import EmptyState from "../components/EmptyState";

export type FeatureId = "templates" | "networkMap" | "gitops" | "systemContainers";
export type Features = Record<FeatureId, boolean>;

export const FEATURE_INFO: { id: FeatureId; label: string; note: string }[] = [
  { id: "templates", label: "Templates", note: "The catalog of ready apps to deploy." },
  { id: "networkMap", label: "Network map", note: "The graph of containers, networks, ports and domains." },
  { id: "gitops", label: "GitOps", note: "Configuration exported to git and synced back; off also stops its sync and webhooks." },
  { id: "systemContainers", label: "Kowan's own containers", note: "The server, the agents and their helpers (updates, scans, backups) in Workloads, the dashboard, Logs and search. Off by default." },
];

// Until the state loads: the server's defaults.
const DEFAULTS: Features = { templates: true, networkMap: true, gitops: true, systemContainers: false };

export function useFeatures(): Features {
  const { user } = useAuth();
  const q = useQuery({ queryKey: ["features"], queryFn: getFeatures, enabled: !!user, staleTime: 60_000 });
  return { ...DEFAULTS, ...(q.data ?? {}) };
}

/** Renders the page, or says the feature is off (for a typed or old link). */
export function FeatureGate({ feature, children }: { feature: FeatureId; children: ReactNode }) {
  const features = useFeatures();
  if (features[feature]) return <>{children}</>;
  const label = FEATURE_INFO.find((f) => f.id === feature)?.label ?? feature;
  return (
    <EmptyState
      title={`${label} is turned off`}
      description="An admin can turn it back on in Settings → Features."
      action={<Link to="/settings" className="cell-link">Open Settings</Link>}
    />
  );
}

/**
 * A container Kowan runs for itself: the server and the agents (their images'
 * title label, which Docker copies onto the container, so the copy parked for
 * a rollback counts too) and their helpers (updates, scans, volume browsing,
 * backups).
 */
export function isKowanContainer(labels: Record<string, string> | undefined): boolean {
  const l = labels ?? {};
  const title = l["org.opencontainers.image.title"];
  return title === "kowan" || title === "kowan-agent" || l["kowan.support"] === "true" || !!l["kowan.role"] || !!l["kowan.volbackup.helper"];
}

/** Drop Kowan's own containers unless Settings shows them. */
export function useContainerFilter(): <T extends { labels?: Record<string, string> }>(list: T[]) => T[] {
  const show = useFeatures().systemContainers;
  return (list) => (show ? list : list.filter((c) => !isKowanContainer(c.labels)));
}
