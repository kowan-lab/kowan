// Disk cleanup of one host: what can go, grouped and sized, with safe
// defaults picked (a deployment's stopped containers and named volumes are
// listed but left alone), one confirmed run, and the same cleanup on a
// schedule with the safe categories only.

import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Checkbox, Icon, InputToggle, NumberInput, Select, Skeleton, Text } from "@/ui/kit";
import { getCleanupPlan, listHosts, putCleanupSchedule, runCleanup } from "../api/client";
import ConfirmModal from "../components/ConfirmModal";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { useCanOperate } from "../components/deployment/shared";
import type { CleanupCategory, CleanupPlan, CleanupPolicy } from "../types";
import { notify } from "../ui/toast";
import { errorMessage, formatBytes, formatDate } from "../utils";

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const HOURS = Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: `${String(h).padStart(2, "0")}:00` }));

/** Ids picked per category. */
type Picks = Record<string, Set<string>>;

export function defaultPicks(plan: CleanupPlan): Picks {
  return Object.fromEntries(plan.categories.map((c) => [c.id, new Set(c.items.filter((i) => i.selected).map((i) => i.id))]));
}

export function pickedSize(plan: CleanupPlan, picks: Picks): number {
  return plan.categories.reduce((n, c) => n + c.items.filter((i) => picks[c.id]?.has(i.id)).reduce((m, i) => m + i.size, 0), 0);
}

export default function DiskCleanup() {
  const { hostId = "" } = useParams<{ hostId: string }>();
  const canOperate = useCanOperate();
  const qc = useQueryClient();
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hostName = hosts.data?.find((h) => h.id === hostId)?.name ?? hostId;
  const q = useQuery({ queryKey: ["cleanup", hostId], queryFn: () => getCleanupPlan(hostId), retry: false });
  const [picks, setPicks] = useState<Picks>({});
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (q.data) setPicks(defaultPicks(q.data));
  }, [q.data]);

  const run = useMutation({
    mutationFn: () =>
      runCleanup(hostId, {
        containers: [...(picks.containers ?? [])],
        images: [...(picks.dangling ?? []), ...(picks.images ?? [])],
        volumes: [...(picks.volumes ?? [])],
        buildCache: (picks.build?.size ?? 0) > 0,
      }),
    onSuccess: (r) => {
      setConfirm(false);
      const n = Object.values(r.removed).reduce((a, b) => a + b, 0);
      notify(r.problems.length
        ? { color: "yellow", title: `Freed ${formatBytes(r.freed)}`, message: `${n} removed; ${r.problems.length} skipped: ${r.problems[0].error}` }
        : { color: "green", title: `Freed ${formatBytes(r.freed)}`, message: `${n} objects removed from ${hostName}.` });
      void qc.invalidateQueries({ predicate: (x) => ["cleanup", "disk-usage", "containers", "images", "volumes"].some((k) => String(x.queryKey[0]).startsWith(k)) });
    },
    onError: (e) => {
      setConfirm(false);
      notify({ color: "red", title: "Cleanup failed", message: errorMessage(e, "") });
    },
  });

  const plan = q.data;
  const size = plan ? pickedSize(plan, picks) : 0;
  const count = Object.values(picks).reduce((n, s) => n + s.size, 0);
  const namedVolumes = plan?.categories.find((c) => c.id === "volumes")?.items.filter((i) => picks.volumes?.has(i.id) && i.detail === "named") ?? [];

  const toggle = (cat: string, id: string, on: boolean) =>
    setPicks((p) => {
      const next = new Set(p[cat]);
      if (on) next.add(id);
      else next.delete(id);
      return { ...p, [cat]: next };
    });
  const setAll = (c: CleanupCategory, on: boolean) => setPicks((p) => ({ ...p, [c.id]: new Set(on ? c.items.map((i) => i.id) : []) }));

  return (
    <div className="flex flex-col gap-[16px]">
      <PageHeader
        title="Disk cleanup"
        description={`What Docker keeps on ${hostName} that nothing uses, and how much space it takes. Pick what goes; everything is checked again before it is removed.`}
        icon={<Icon name="mop-outline-rounded" size="lg" />}
      />
      {q.isLoading ? (
        <Skeleton height={240} />
      ) : q.isError ? (
        <Callout color="danger" title="Cannot read the disk usage">{errorMessage(q.error, "")}</Callout>
      ) : plan && (
        <>
          <section className="flex flex-wrap items-center gap-x-[24px] gap-y-[8px] bg-card px-[16px] py-[14px] shadow-[inset_0_0_0_1px_var(--border)]">
            <div className="flex flex-col">
              <span className="text-[24px] leading-[30px] font-bold">{formatBytes(size)}</span>
              <Text size="sm" color="muted">picked of {formatBytes(plan.total)} that can go · {count} objects</Text>
            </div>
            <Text size="sm" color="muted" className="max-w-[460px]">
              Docker data on this host: images {formatBytes(plan.disk.images)}, containers {formatBytes(plan.disk.containers)}, volumes{" "}
              {formatBytes(plan.disk.volumes)}, build cache {formatBytes(plan.disk.buildCache)}.
            </Text>
            {canOperate && (
              <Button className="ml-auto" color="danger" disabled={!count} onClick={() => setConfirm(true)}>
                Clean up {formatBytes(size)}
              </Button>
            )}
          </section>
          {plan.total === 0 && <Callout color="success" title="Nothing to clean">Every image, volume and container on {hostName} is in use.</Callout>}
          {plan.categories.filter((c) => c.items.length).map((c) => (
            <SectionCard
              key={c.id}
              title={`${c.title} · ${formatBytes(c.total)}`}
              description={c.help}
              actions={canOperate && (
                <div className="flex gap-[6px]">
                  <Button size="sm" variant="clear" onClick={() => setAll(c, true)}>All</Button>
                  <Button size="sm" variant="clear" onClick={() => setAll(c, false)}>None</Button>
                </div>
              )}
              noBodyPadding
            >
              <ul className="m-0 list-none p-0" aria-label={c.title}>
                {c.items.map((i) => (
                  <li key={i.id} className="flex flex-wrap items-center gap-x-[12px] gap-y-[2px] border-b px-[16px] py-[8px] last:border-b-0">
                    <Checkbox
                      aria-label={`Remove ${i.name}`}
                      checked={picks[c.id]?.has(i.id) ?? false}
                      disabled={!canOperate}
                      onChange={(e) => toggle(c.id, i.id, e.target.checked)}
                    />
                    <span className="min-w-0 flex-1 truncate font-mono text-[13px]" title={i.id}>{i.name}</span>
                    {i.detail && <Text as="span" size="xs" color="muted">{i.detail}</Text>}
                    <span className="w-[80px] text-right font-mono text-[13px]">{formatBytes(i.size)}</span>
                    {i.note && <Text size="xs" color="warning" className="basis-full pl-[28px]">{i.note}</Text>}
                  </li>
                ))}
              </ul>
            </SectionCard>
          ))}
          <ScheduleCard hostId={hostId} plan={plan} canOperate={canOperate} />
        </>
      )}
      <ConfirmModal
        opened={confirm}
        onClose={() => setConfirm(false)}
        onConfirm={() => run.mutate()}
        loading={run.isPending}
        lockWhileLoading
        title={`Clean up ${hostName}?`}
        message={
          <>
            {count} objects are removed, about {formatBytes(size)}. Anything that is in use again by then is skipped.
            {namedVolumes.length > 0 && (
              <strong className="mt-[8px] block">
                The data in {namedVolumes.map((v) => v.name).join(", ")} is deleted for good.
              </strong>
            )}
          </>
        }
        confirmLabel="Remove"
        confirmColor="red"
      />
    </div>
  );
}

function ScheduleCard({ hostId, plan, canOperate }: { hostId: string; plan: CleanupPlan; canOperate: boolean }) {
  const qc = useQueryClient();
  const [p, setP] = useState<CleanupPolicy>(plan.schedule);
  const dirty = useMemo(() => JSON.stringify(p) !== JSON.stringify(plan.schedule), [p, plan.schedule]);
  const set = (x: Partial<CleanupPolicy>) => setP((v) => ({ ...v, ...x }));
  const save = useMutation({
    mutationFn: () => putCleanupSchedule(hostId, p),
    onSuccess: (saved) => {
      qc.setQueryData<CleanupPlan>(["cleanup", hostId], (old) => (old ? { ...old, schedule: saved } : old));
      notify({ color: "green", title: saved.enabled ? "Cleanup scheduled" : "Scheduled cleanup off" });
    },
    onError: (e) => notify({ color: "red", title: "Not saved", message: errorMessage(e, "") }),
  });
  const last = plan.lastRun;
  return (
    <SectionCard
      title="On a schedule"
      description="Runs the safe part on its own: never named volumes, never a deployment's containers."
      actions={canOperate && <Button size="sm" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>Save</Button>}
    >
      <div className="flex flex-col gap-[12px]">
        <InputToggle label="Clean up this host on a schedule" checked={p.enabled} disabled={!canOperate} onChange={(e) => set({ enabled: e.target.checked })} />
        <div className="flex flex-wrap gap-[12px]">
          <Select size="sm" width={140} label="Every" disabled={!canOperate || !p.enabled} value={p.every}
            options={[{ value: "daily", label: "Day" }, { value: "weekly", label: "Week" }]} onChange={(v) => v && set({ every: v as CleanupPolicy["every"] })} />
          {p.every === "weekly" && (
            <Select size="sm" width={150} label="On" disabled={!canOperate || !p.enabled} value={String(p.weekday)}
              options={WEEKDAYS.map((d, i) => ({ value: String(i), label: d }))} onChange={(v) => v && set({ weekday: Number(v) })} />
          )}
          <Select size="sm" width={110} label="At (UTC)" disabled={!canOperate || !p.enabled} value={String(p.hour)} options={HOURS} onChange={(v) => v && set({ hour: Number(v) })} />
        </div>
        <div className="grid gap-[6px] sm:grid-cols-2">
          <InputToggle label="Stopped containers no deployment owns" checked={p.containers} disabled={!canOperate || !p.enabled} onChange={(e) => set({ containers: e.target.checked })} />
          <InputToggle label="Untagged images" checked={p.dangling} disabled={!canOperate || !p.enabled} onChange={(e) => set({ dangling: e.target.checked })} />
          <InputToggle label="Anonymous volumes nothing mounts" checked={p.anonymousVolumes} disabled={!canOperate || !p.enabled} onChange={(e) => set({ anonymousVolumes: e.target.checked })} />
          <InputToggle label="Build cache" checked={p.buildCache} disabled={!canOperate || !p.enabled} onChange={(e) => set({ buildCache: e.target.checked })} />
        </div>
        <NumberInput label="Unused images older than (days)" description="0 keeps unused tagged images." min={0} max={3650}
          value={p.imagesOlderThanDays} disabled={!canOperate || !p.enabled} onChange={(v) => set({ imagesOlderThanDays: Number(v) || 0 })} style={{ maxWidth: 260 }} />
        <Text size="sm" color="muted">
          {last
            ? last.error
              ? `Last run ${formatDate(last.at)} failed: ${last.error}`
              : `Last run ${formatDate(last.at)} freed ${formatBytes(last.freed)}.`
            : "Has not run yet."}{" "}
          Each run reports in the notifications bell.
        </Text>
      </div>
    </SectionCard>
  );
}
