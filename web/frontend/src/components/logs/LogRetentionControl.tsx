// "Keep logs" on a workload's Logs tab: turns retained logs on or off and
// links to the searchable history.

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { Select, Text } from "@/ui/kit";
import { deleteLogRetention, listLogRetention, setLogRetention } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import ConfirmModal from "../ConfirmModal";
import { useCanOperate } from "../deployment/shared";
import { RETENTION_DAYS, daysLabel } from "./retention";

const OFF = "off";

export default function LogRetentionControl({ workload }: { workload: string }) {
  const qc = useQueryClient();
  const canOperate = useCanOperate();
  const [confirmOff, setConfirmOff] = useState(false);
  const q = useQuery({ queryKey: ["log-retention"], queryFn: listLogRetention });
  const current = q.data?.find((r) => r.workload === workload);

  const save = useMutation({
    mutationFn: async (value: string) => {
      if (value === OFF) await deleteLogRetention(workload);
      else await setLogRetention(workload, Number(value));
      return value;
    },
    onSuccess: (value) => {
      setConfirmOff(false);
      qc.invalidateQueries({ queryKey: ["log-retention"] });
      notify(value === OFF
        ? { color: "green", title: "Logs no longer kept", message: "The stored lines were deleted." }
        : { color: "green", title: "Keeping logs", message: `New lines are kept for ${daysLabel(Number(value))}.` });
    },
    onError: (e) => notify({ color: "red", title: "Not saved", message: errorMessage(e, "") }),
  });

  if (q.isLoading) return null;
  const value = current ? String(current.days) : OFF;
  const options = [{ value: OFF, label: "Off" }, ...RETENTION_DAYS];
  if (current && !options.some((o) => o.value === value)) options.push({ value, label: daysLabel(current.days) });

  const choose = (v: string) => {
    if (!v || v === value) return;
    if (v === OFF && current && current.lines > 0) setConfirmOff(true);
    else save.mutate(v);
  };

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3 border bg-card px-6 py-4" data-testid="log-retention">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-[13px] font-semibold">Keep logs</span>
        <Text size="sm" color="muted">
          {current
            ? `${current.lines.toLocaleString()} lines stored. They stay searchable after restarts and redeploys.`
            : "Off: only what Docker still holds is shown. Keep logs to search them later, even after a container is replaced."}
        </Text>
      </div>
      {canOperate ? (
        <Select size="sm" width={140} aria-label="Keep logs" options={options} value={value} disabled={save.isPending} onChange={choose} />
      ) : (
        <Text size="sm">{current ? daysLabel(current.days) : "Off"}</Text>
      )}
      {current && (
        <Link
          to={`/logs?mode=history&workload=${encodeURIComponent(workload)}`}
          className="text-[13px] font-semibold text-[var(--link)] hover:underline"
        >
          Search history
        </Link>
      )}
      <ConfirmModal
        opened={confirmOff}
        onClose={() => setConfirmOff(false)}
        onConfirm={() => save.mutate(OFF)}
        loading={save.isPending}
        title="Stop keeping logs?"
        message={`The ${current?.lines.toLocaleString() ?? 0} stored lines are deleted. Live logs are not affected.`}
        confirmLabel="Stop and delete"
        confirmColor="red"
      />
    </div>
  );
}
