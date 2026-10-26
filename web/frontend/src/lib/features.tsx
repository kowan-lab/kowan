// Platform features an admin can turn off in Settings (kowan/features.py).
// While one is off its navigation, links and pages are hidden; its API answers
// 404. Until the state is loaded everything counts as on.
import type { ReactNode } from "react";
import { Link } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { getFeatures } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import EmptyState from "../components/EmptyState";

export type FeatureId = "templates" | "networkMap" | "gitops";
export type Features = Record<FeatureId, boolean>;

export const FEATURE_INFO: { id: FeatureId; label: string; note: string }[] = [
  { id: "templates", label: "Templates", note: "The catalog of ready apps to deploy." },
  { id: "networkMap", label: "Network map", note: "The graph of containers, networks, ports and domains." },
  { id: "gitops", label: "GitOps", note: "Configuration exported to git and synced back; off also stops its sync and webhooks." },
];

const ALL_ON: Features = { templates: true, networkMap: true, gitops: true };

export function useFeatures(): Features {
  const { user } = useAuth();
  const q = useQuery({ queryKey: ["features"], queryFn: getFeatures, enabled: !!user, staleTime: 60_000 });
  return { ...ALL_ON, ...(q.data ?? {}) };
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
