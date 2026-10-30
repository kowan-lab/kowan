import { Link, useNavigate } from "react-router";
import { appHref, isReplaced, kindShort, useApps, type App } from "../../lib/apps";
import { isAllHosts } from "../../HostContext";
import { useHostName } from "../../lib/hostsQuery";
import { HostChip, Pill, StateSquares, Tag } from "../apps/ui";
import { cn } from "@/lib/utils";
import { Btn, FRAME, SectHead } from "./ui";

const MAX = 5;
const TH = "h-[36px] px-[12px] text-left font-mono text-[12px] leading-none font-semibold tracking-[0.06em] whitespace-nowrap text-muted-foreground uppercase first:pl-[16px] last:pr-[16px]";
const TD = "h-[40px] px-[12px] align-middle text-[13px] leading-[18px] whitespace-nowrap shadow-[inset_0_1px_0_var(--border)] first:pl-[16px] last:pr-[16px]";
const RANK: Record<string, number> = { r: 0, a: 1 };

/** The first workloads of the scope, what needs attention first; the rest is one click away. */
export function WorkloadsCard({ hostId }: { hostId: string }) {
  const navigate = useNavigate();
  const { apps, hosts, isLoading } = useApps(hostId);
  const all = isAllHosts(hostId);
  const hostName = useHostName();
  const hostOnline = (id: string) => hosts.find((h) => h.id === id)?.online ?? true;
  const list = apps ?? [];
  const rank = (a: App) => (a.attention ? RANK[a.attention.tone] ?? 2 : 3);
  const shown = [...list].sort((a, b) => rank(a) - rank(b)).slice(0, MAX);
  return (
    <section className={FRAME}>
      <SectHead
        title="Workloads"
        label={apps ? (list.length > MAX ? `${MAX} of ${list.length}` : `${list.length}`) : undefined}
        right={<Btn sm to="/workloads">All workloads</Btn>}
      />
      {!shown.length ? (
        <p className="m-0 px-[16px] py-[14px] text-[14px] leading-[20px] text-muted-foreground">
          {isLoading ? "Loading workloads…" : "Nothing runs here yet."}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr>
                <th className={TH}>Workload</th>
                <th className={TH}>State</th>
                <th className={TH}>Containers</th>
                {all && <th className={TH}>Host</th>}
                <th className={TH}>Kind</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((a) => {
                const host = a.hostIds[0] ?? "";
                const managed = a.kind === "deployment";
                return (
                  <tr key={a.id} className="cursor-pointer hover:[&>td]:bg-[var(--popover)]" onClick={() => navigate(appHref(a))}>
                    <td className={TD}>
                      <Link to={appHref(a)} className="font-semibold text-foreground no-underline hover:underline" onClick={(e) => e.stopPropagation()}>{a.name}</Link>
                    </td>
                    <td className={TD}><Pill tone={a.tone}>{a.label}</Pill></td>
                    <td className={TD}>
                      <span className="inline-flex flex-nowrap"><StateSquares tones={a.containers.filter((c) => !isReplaced(c)).map((c) => c.tone)} /></span>
                    </td>
                    {all && <td className={TD}><HostChip name={hostName(host)} online={hostOnline(host)} /></td>}
                    <td className={TD}>
                      <span className="inline-flex items-center gap-[8px]">
                        <span className="font-mono">{kindShort(a)}</span>
                        <Tag className={cn(managed && "text-foreground")}>{managed ? "Managed" : "Not managed"}</Tag>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
