import { Select } from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { listHosts } from "../api/client";
import type { Host } from "../types";
import { ALL_HOSTS } from "../HostContext";

// ---------------------------------------------------------------------------
// HostSelectField: a single, shared "pick the target host" control for the
// create/prune/pull/build modals.
//
// Docker op APIs take an explicit hostId first arg (createContainer(hostId, …),
// prune(hostId, kind), …). Historically the pages fed those the global host
// from useHost(), so the actions had to be hidden in the aggregated "All hosts"
// view (there is no single host to target). We instead always show the actions
// and let the user pick the concrete host here, in the modal.
//
// The query uses the SAME ["hosts"] key the Layout host-switcher uses, so this
// reads straight from the shared react-query cache — no extra fetch in the
// common case.
// ---------------------------------------------------------------------------

interface HostSelectFieldProps {
  /** The currently selected hostId (controlled). */
  value: string;
  /** Called with the chosen hostId. */
  onChange: (hostId: string) => void;
  /** Field label. Defaults to "Host". */
  label?: string;
  /** Field description. Defaults to a generic "target host" hint. */
  description?: string;
  /** Disable the control (e.g. while a mutation is in flight). */
  disabled?: boolean;
}

export default function HostSelectField({
  value,
  onChange,
  label,
  description,
  disabled,
}: HostSelectFieldProps) {
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });

  const options = (hostsQuery.data ?? []).map((h) => ({
    value: h.id,
    // Offline hosts stay selectable — a host may be only temporarily offline —
    // but we flag them so the user knows the action may not go through yet.
    label: h.online ? h.name || h.id : `${h.name || h.id} (offline)`,
  }));

  return (
    <Select
      label={label ?? "Host"}
      description={description ?? "Target host for this action."}
      placeholder="Select a host"
      options={options}
      value={value || undefined}
      onChange={(v) => onChange((v as string) ?? "")}
      disabled={disabled}
      searchable
    />
  );
}

/**
 * Pick a sensible default target host for a create/prune/pull/build modal.
 *
 * Prefers the current global selection when it is a real, single host; when the
 * global view is empty or the aggregated "All hosts" view, falls back to the
 * first online host, then any host, then "" (nothing to pick).
 */
export function defaultTargetHost(globalHostId: string, hosts: Host[]): string {
  if (globalHostId !== "" && globalHostId !== ALL_HOSTS) {
    return globalHostId;
  }
  const online = hosts.find((h) => h.online);
  if (online) return online.id;
  return hosts[0]?.id ?? "";
}
