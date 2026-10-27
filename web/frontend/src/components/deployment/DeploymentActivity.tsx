// Activity tab: what people did to this deployment, read from the audit log
// filtered to the deployment's API path. The audit log is admin-only, and it
// records API calls: webhook and auto-heal redeploys do not appear in it.

import { useQuery } from "@tanstack/react-query";
import { Callout, Skeleton, StatePill, Tooltip } from "@/ui/kit";
import { listAudit } from "../../api/client";
import EmptyState from "../EmptyState";
import SectionCard from "../SectionCard";
import type { AuditEntry, Deployment } from "../../types";
import { errorMessage, formatDate, timeAgo } from "../../utils";

/** Turn "POST /api/v1/deployments/{id}/stop" into "Stopped". */
function describe(e: AuditEntry, base: string): string {
  const sub = e.path.slice(base.length).replace(/^\//, "");
  const m = e.action.toUpperCase();
  if (sub === "") return m === "PUT" ? "Configuration saved" : m === "DELETE" ? "Deleted" : `${m} deployment`;
  switch (`${m} ${sub}`) {
    case "POST deploy":
    case "POST redeploy":
      return "Deploy requested";
    case "POST start":
      return "Started";
    case "POST stop":
      return "Stopped";
    case "POST destroy":
      return "Containers removed";
    case "POST sync":
      return "Pulled from git";
    case "PUT protection":
      return "Deletion protection changed";
    case "PUT enabled":
      return "Auto-heal changed";
    case "POST webhook":
      return "Webhook created or rotated";
    case "DELETE webhook":
      return "Webhook removed";
    case "PUT autoupdate":
      return "Auto-update policy saved";
    case "DELETE autoupdate":
      return "Auto-update policy removed";
    default:
      return `${m} ${sub}`;
  }
}

export default function DeploymentActivity({ deployment: d, isAdmin }: { deployment: Deployment; isAdmin: boolean }) {
  const base = `/api/v1/deployments/${d.id}`;
  const query = useQuery({
    queryKey: ["audit", "deployment", d.id],
    queryFn: () => listAudit({ path: base, limit: 100 }),
    enabled: isAdmin,
    refetchInterval: 15_000,
  });

  if (!isAdmin) {
    return (
      <Callout color="info" title="Activity is visible to admins">
        It comes from the audit log, which only admins can read.
      </Callout>
    );
  }

  // The server filters by path; keep only exact matches here as well.
  const items = (query.data?.items ?? []).filter((e) => e.path === base || e.path.startsWith(base + "/"));

  let body;
  if (query.isLoading) {
    body = (
      <div className="flex flex-col gap-3 px-[var(--page-px,16px)] py-6">
        <Skeleton height={18} />
        <Skeleton height={18} width="80%" />
        <Skeleton height={18} width="60%" />
      </div>
    );
  } else if (query.isError) {
    body = (
      <div className="px-[var(--page-px,16px)] py-6">
        <Callout color="danger" title="Couldn't load the activity">
          {errorMessage(query.error, "The audit log did not answer.")}
        </Callout>
      </div>
    );
  } else if (items.length === 0) {
    body = <EmptyState title="No activity yet" description="Deploys, edits and settings changes made through Kowan appear here." />;
  } else {
    body = (
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0 text-[13px]">
          <thead className="border-b bg-sunken/70">
            <tr className="text-left text-[12px] font-medium tracking-normal text-muted-foreground font-sans ">
              <th scope="col" className="px-5 py-3 pl-[var(--page-px,16px)] font-medium">When</th>
              <th scope="col" className="px-5 py-3 font-medium">Action</th>
              <th scope="col" className="px-5 py-3 font-medium">Result</th>
              <th scope="col" className="px-5 py-3 pr-[var(--page-px,16px)] font-medium">By</th>
            </tr>
          </thead>
          <tbody>
            {items.map((e) => {
              const ok = e.status < 400;
              return (
                <tr key={e.id} className="border-b border-border/70 last:border-0">
                  <td className="px-5 py-3 pl-[var(--page-px,16px)] whitespace-nowrap text-muted-foreground">
                    <Tooltip content={formatDate(e.time)}>
                      <span>{timeAgo(e.time)}</span>
                    </Tooltip>
                  </td>
                  <td className="px-5 py-3">{describe(e, base)}</td>
                  <td className="px-5 py-3">
                    <StatePill size="sm" status={ok ? "ok" : "failed"} label={ok ? "OK" : "Failed"} detail={String(e.status)} />
                  </td>
                  <td className="px-5 py-3 pr-[var(--page-px,16px)] whitespace-nowrap">{e.username || <span className="text-muted-foreground">system</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <SectionCard
      title="Activity"
      description="Changes made through the API and this console, newest first. Webhook and auto-heal redeploys are not listed."
      noBodyPadding
    >
      {body}
    </SectionCard>
  );
}
