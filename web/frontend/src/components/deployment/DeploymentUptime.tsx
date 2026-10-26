// Uptime checks linked to this deployment (Uptime > Workload), shown on its
// overview so a workload's page says whether it answers from outside.

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { StatePill, Text } from "@/ui/kit";
import { listUptimeChecks } from "../../api/client";
import { LIVE_REFETCH_MS } from "../../lib/live";
import SectionCard from "../SectionCard";
import type { Deployment } from "../../types";
import { Beats, ms, pct } from "../../pages/Uptime";

const TONE = { up: "green", down: "red", unknown: "gray", paused: "gray" } as const;

export default function DeploymentUptime({ deployment: d }: { deployment: Deployment }) {
  const q = useQuery({ queryKey: ["uptime-checks"], queryFn: listUptimeChecks, refetchInterval: LIVE_REFETCH_MS, retry: false });
  const checks = (q.data ?? []).filter((c) => c.deploymentId === d.id);
  if (checks.length === 0) return null;
  return (
    <SectionCard title="Uptime" description="Checks linked to this workload." actions={<Link to="/uptime" className="text-[13px] text-[var(--link)]">All checks</Link>} noBodyPadding>
      <ul className="m-0 list-none p-0">
        {checks.map((c) => (
          <li key={c.id} className="flex flex-wrap items-center gap-x-[16px] gap-y-[6px] border-b px-[16px] py-[10px] last:border-b-0">
            <StatePill status={c.status} label={c.status === "unknown" ? "Pending" : c.status} color={TONE[c.status]} size="sm" />
            <span className="min-w-0 flex-1">
              <Text size="sm" weight="semibold">{c.name}</Text>{" "}
              <Text as="span" size="xs" color="muted" mono>{c.target}</Text>
            </span>
            <Beats results={c.recent} label={c.name} />
            <Text size="xs" mono>{pct(c.uptime24h)} · {ms(c.latency24h)}</Text>
          </li>
        ))}
      </ul>
    </SectionCard>
  );
}
