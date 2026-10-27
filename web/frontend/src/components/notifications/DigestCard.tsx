// The periodic digest on the Notifications page: when it goes out, to which
// channels, which sections it carries, a preview of the text and "send now".

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Checkbox, Code, Icon, InputToggle, Popup, Select, Skeleton, Text } from "@/ui/kit";
import { getDigest, previewDigest, putDigest, sendDigest } from "../../api/client";
import type { DigestSection, DigestSettings, NotificationChannel } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage, formatDate } from "../../utils";
import SectionCard from "../SectionCard";

const SECTION_LABELS: Record<DigestSection, string> = {
  updates: "Container updates waiting",
  cves: "Images with critical or high vulnerabilities",
  backups: "Backups that failed",
  certificates: "Certificates expiring within 21 days",
  alerts: "Alerts still open",
};
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const HOURS = Array.from({ length: 24 }, (_, h) => ({ value: String(h), label: `${String(h).padStart(2, "0")}:00` }));

export default function DigestCard({ channels }: { channels: NotificationChannel[] }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["digest"], queryFn: getDigest });
  const [s, setS] = useState<DigestSettings | null>(null);
  const [preview, setPreview] = useState(false);
  useEffect(() => {
    if (q.data) setS(q.data.settings);
  }, [q.data]);
  const set = (p: Partial<DigestSettings>) => setS((v) => (v ? { ...v, ...p } : v));

  const save = useMutation({
    mutationFn: (v: DigestSettings) => putDigest(v),
    onSuccess: (saved) => {
      qc.setQueryData(["digest"], (old: { last: unknown } | undefined) => ({ ...(old ?? { last: null }), settings: saved }));
      notify({ color: "green", title: saved.enabled ? "Digest scheduled" : "Digest off" });
    },
    onError: (e) => notify({ color: "red", title: "Not saved", message: errorMessage(e, "") }),
  });
  const send = useMutation({
    mutationFn: sendDigest,
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["digest"] });
      if (!r.sent.length && !r.failed.length) notify({ color: "yellow", title: "Nobody to send it to", message: "Turn on a channel first." });
      else if (r.failed.length) notify({ color: "yellow", title: `Sent to ${r.sent.length}`, message: `Failed: ${r.failed.join("; ")}` });
      else notify({ color: "green", title: "Digest sent", message: `To ${r.sent.join(", ")}.` });
    },
    onError: (e) => notify({ color: "red", title: "Not sent", message: errorMessage(e, "") }),
  });

  if (q.isError) return <Callout color="danger" title="Cannot load the digest">{errorMessage(q.error, "")}</Callout>;
  if (q.isLoading || !s) return <Skeleton height={200} />;
  const dirty = JSON.stringify(s) !== JSON.stringify(q.data!.settings);
  const enabledChannels = channels.filter((c) => c.enabled);
  const last = q.data!.last;
  const toggleChannel = (id: string, on: boolean) =>
    set({ channels: on ? [...s.channels, id] : s.channels.filter((c) => c !== id) });

  return (
    <SectionCard
      title="Digest"
      icon={<Icon name="mail-outline-rounded" />}
      description="One message a day or a week with what needs attention but was not worth an alert of its own."
      actions={
        <div className="flex flex-wrap gap-[8px]">
          <Button variant="outline" color="neutral" onClick={() => setPreview(true)}>Preview</Button>
          <Button variant="outline" color="neutral" loading={send.isPending} onClick={() => send.mutate()}>Send now</Button>
          <Button disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(s)}>Save</Button>
        </div>
      }
    >
      <div className="flex flex-col gap-[14px]">
        <InputToggle label="Send the digest" checked={s.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <div className="flex flex-wrap gap-[12px]">
          <Select size="sm" width={140} label="Every" value={s.every} options={[{ value: "daily", label: "Day" }, { value: "weekly", label: "Week" }]}
            onChange={(v) => v && set({ every: v as DigestSettings["every"] })} />
          {s.every === "weekly" && (
            <Select size="sm" width={150} label="On" value={String(s.weekday)} options={WEEKDAYS.map((d, i) => ({ value: String(i), label: d }))}
              onChange={(v) => v && set({ weekday: Number(v) })} />
          )}
          <Select size="sm" width={110} label="At (UTC)" value={String(s.hour)} options={HOURS} onChange={(v) => v && set({ hour: Number(v) })} />
        </div>
        <fieldset className="m-0 flex flex-col gap-[6px] border-0 p-0">
          <legend className="pb-[6px] text-[13px] font-semibold">What it covers</legend>
          {(Object.keys(SECTION_LABELS) as DigestSection[]).map((k) => (
            <Checkbox key={k} label={SECTION_LABELS[k]} checked={s.sections[k]} onChange={(e) => set({ sections: { ...s.sections, [k]: e.target.checked } })} />
          ))}
          <Checkbox label="Send it even when there is nothing to report" checked={s.sendEmpty} onChange={(e) => set({ sendEmpty: e.target.checked })} />
        </fieldset>
        <fieldset className="m-0 flex flex-col gap-[6px] border-0 p-0">
          <legend className="pb-[6px] text-[13px] font-semibold">Channels</legend>
          {enabledChannels.length === 0 ? (
            <Text size="sm" color="muted">No channel is on: add or turn on one above.</Text>
          ) : (
            <>
              <Text size="sm" color="muted">{s.channels.length ? "Only the ones picked:" : "Every channel that is on, or pick some:"}</Text>
              {enabledChannels.map((c) => (
                <Checkbox key={c.id} label={c.name} checked={s.channels.includes(c.id)} onChange={(e) => toggleChannel(c.id, e.target.checked)} />
              ))}
            </>
          )}
        </fieldset>
        <Text size="sm" color="muted">
          {last
            ? last.skipped
              ? `Last digest ${formatDate(last.at)}: nothing to report, not sent.`
              : `Last digest ${formatDate(last.at)}: sent to ${last.sent.join(", ") || "nobody"}${last.failed.length ? `; failed: ${last.failed.join("; ")}` : ""}.`
            : "No digest sent yet."}
        </Text>
      </div>
      {preview && <PreviewDialog onClose={() => setPreview(false)} />}
    </SectionCard>
  );
}

function PreviewDialog({ onClose }: { onClose: () => void }) {
  const q = useQuery({ queryKey: ["digest-preview"], queryFn: previewDigest, retry: false, gcTime: 0 });
  return (
    <Popup open onOpenChange={(o) => !o && onClose()} size="lg">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Digest preview</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {q.isLoading ? (
            <Skeleton height={160} />
          ) : q.isError ? (
            <Callout color="danger">{errorMessage(q.error, "")}</Callout>
          ) : (
            <>
              <Text weight="semibold">{q.data!.title}</Text>
              <Code block>{q.data!.text}</Code>
              <Text size="sm" color="muted">Covers {formatDate(q.data!.since)} to {formatDate(q.data!.until)}, with the saved settings.</Text>
            </>
          )}
        </Popup.Body>
      </Popup.Content>
    </Popup>
  );
}
