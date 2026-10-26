// The public status page (/status): no sign-in, only the checks an admin
// marked public, 30 days of daily uptime and the recent outages.

import { useQuery } from "@tanstack/react-query";
import { Callout, Skeleton } from "@/ui/kit";
import { getPublicStatus } from "../api/client";
import type { PublicStatus } from "../types";
import { formatDate } from "../utils";
import { duration, ms, pct } from "./Uptime";

const OVERALL: Record<PublicStatus["overall"], { text: string; tone: string }> = {
  up: { text: "All systems operational", tone: "green" },
  degraded: { text: "Some systems are down", tone: "amber" },
  down: { text: "All systems are down", tone: "red" },
  unknown: { text: "No checks to show yet", tone: "gray" },
};

function DayBars({ days }: { days: PublicStatus["checks"][number]["days"] }) {
  // Always 30 slots, oldest first; days without probes stay empty.
  const byDate = new Map(days.map((d) => [d.date, d]));
  const slots = Array.from({ length: 30 }, (_, i) => {
    const d = new Date(Date.now() - (29 - i) * 86_400_000).toISOString().slice(0, 10);
    return { date: d, day: byDate.get(d) };
  });
  return (
    <div className="flex h-[28px] items-stretch gap-[2px]" role="img" aria-label="Daily uptime over the last 30 days">
      {slots.map(({ date, day }) => {
        const bg = !day ? "var(--muted)" : day.uptime >= 99.9 ? "var(--tone-green-solid)" : day.uptime >= 95 ? "var(--tone-amber-solid)" : "var(--tone-red-solid)";
        return <span key={date} title={day ? `${date}: ${pct(day.uptime)}` : `${date}: no data`} className="flex-1" style={{ background: bg }} />;
      })}
    </div>
  );
}

export default function StatusPage() {
  const q = useQuery({ queryKey: ["public-status"], queryFn: getPublicStatus, refetchInterval: 60_000, retry: false });
  const data = q.data;
  return (
    <div className="min-h-screen bg-background text-foreground">
      <main className="mx-auto flex max-w-[860px] flex-col gap-[24px] px-[16px] py-[40px]">
        {q.isLoading ? (
          <Skeleton height={240} />
        ) : q.isError || !data ? (
          <Callout color="gray" title="Status page unavailable">This server does not publish a status page.</Callout>
        ) : (
          <>
            <header className="flex flex-col gap-[8px]">
              <h1 className="m-0 text-[28px] leading-[36px] font-bold tracking-[-0.015em]">{data.title}</h1>
              {data.description && <p className="m-0 text-[15px] text-muted-foreground">{data.description}</p>}
            </header>
            <section
              className="px-[16px] py-[14px] text-[16px] font-semibold"
              style={{ background: `var(--tone-${OVERALL[data.overall].tone}-soft)`, color: `var(--tone-${OVERALL[data.overall].tone}-text)` }}
            >
              {OVERALL[data.overall].text}
            </section>
            <section aria-label="Services" className="flex flex-col bg-card shadow-[inset_0_0_0_1px_var(--border)]">
              {data.checks.map((c) => (
                <div key={c.id} className="flex flex-col gap-[8px] border-b px-[16px] py-[14px] last:border-b-0">
                  <div className="flex flex-wrap items-baseline gap-x-[12px]">
                    <span className="text-[15px] font-semibold">{c.name}</span>
                    <span className="text-[13px] font-semibold" style={{ color: `var(--tone-${c.status === "up" ? "green" : c.status === "down" ? "red" : "gray"}-text)` }}>
                      {c.status === "up" ? "Operational" : c.status === "down" ? "Down" : "Pending"}
                    </span>
                    <span className="ml-auto font-mono text-[12px] text-muted-foreground">
                      {pct(c.uptime30d)} over 30 days{c.latencyMs ? ` · ${ms(c.latencyMs)}` : ""}
                    </span>
                  </div>
                  <DayBars days={c.days} />
                </div>
              ))}
            </section>
            <section aria-label="Recent incidents" className="flex flex-col gap-[8px]">
              <h2 className="m-0 text-[16px] font-semibold">Recent incidents</h2>
              {data.incidents.length === 0 ? (
                <p className="m-0 text-[14px] text-muted-foreground">No outages recently.</p>
              ) : (
                <ul className="m-0 flex list-none flex-col gap-[6px] p-0 text-[14px]">
                  {data.incidents.map((i) => (
                    <li key={i.id} className="flex flex-wrap gap-x-[12px]">
                      <span className="font-semibold">{i.name}</span>
                      <span>{formatDate(i.startedAt)}</span>
                      <span className="text-muted-foreground">
                        {i.endedAt ? `down for ${duration(i.startedAt, i.endedAt)}` : "ongoing"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <footer className="font-mono text-[12px] text-muted-foreground">Updated {formatDate(data.generatedAt)} · Kowan</footer>
          </>
        )}
      </main>
    </div>
  );
}
