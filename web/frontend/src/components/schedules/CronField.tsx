import { useEffect, useMemo, useState } from "react";
import { Box, Button, Select, Text, TextInput } from "@/ui/kit";
import { errorMessage } from "../../utils";
import { CRON_PRESETS, inZone, previewSchedule, timeZones } from "./api";

/**
 * Cron expression + IANA timezone with a live next-run preview computed by the
 * server (same parser and DST rules as the scheduler). Shared by the schedule
 * editor and the backup job editor.
 */
export default function CronField({
  cron, timezone, onCron, onTimezone, allowEmpty = false, label = "Cron expression",
}: {
  cron: string;
  timezone: string;
  onCron: (v: string) => void;
  onTimezone: (v: string) => void;
  /** Empty cron allowed (manual-only backup jobs). */
  allowEmpty?: boolean;
  label?: string;
}) {
  const [preview, setPreview] = useState<string[]>([]);
  const [error, setError] = useState("");
  const zones = useMemo(() => [{ value: "", label: "Server local time" }, ...timeZones().map((z) => ({ value: z, label: z }))], []);

  useEffect(() => {
    if (!cron.trim()) {
      setPreview([]);
      setError(allowEmpty ? "" : "Required");
      return;
    }
    let alive = true;
    const t = window.setTimeout(() => {
      previewSchedule(cron.trim(), timezone, 3)
        .then((r) => { if (alive) { setPreview(r); setError(""); } })
        .catch((e) => { if (alive) { setPreview([]); setError(errorMessage(e, "Invalid cron")); } });
    }, 300);
    return () => { alive = false; window.clearTimeout(t); };
  }, [cron, timezone, allowEmpty]);

  return (
    <Box direction="column" gap={2}>
      <Box direction="row" wrap gap={3} align="start">
        <TextInput
          label={label}
          value={cron}
          placeholder={allowEmpty ? "empty = manual only" : "0 22 * * 1-5"}
          onChange={(e) => onCron(e.target.value)}
          errorText={error || undefined}
          description="minute hour day-of-month month day-of-week, or @daily / @hourly"
          style={{ fontFamily: "inherit" }}
          maxWidth={300}
        />
        <Select label="Timezone" options={zones} value={timezone} onChange={onTimezone} searchable width={240} />
      </Box>
      <Box direction="row" wrap gap={1}>
        {CRON_PRESETS.map((p) => (
          <Button key={p.cron} size="xs" variant="outline" color="neutral" onClick={() => onCron(p.cron)}>
            {p.label}
          </Button>
        ))}
      </Box>
      {preview.length > 0 && (
        <Text size="xs" color="muted">
          Next: {preview.map((p) => inZone(p, timezone)).join("  ·  ")}
        </Text>
      )}
    </Box>
  );
}
