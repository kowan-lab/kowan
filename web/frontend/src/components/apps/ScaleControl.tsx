import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Minus, Plus } from "lucide-react";
import { updateDeployment } from "../../api/client";
import { scaleRule, scaledInput } from "../../lib/scale";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import type { Deployment } from "../../types";
import { Btn } from "./ui";

const ICON = "size-[14px]";

/**
 * Requested replicas of one service of a Kowan deployment, changed in place:
 * − and + pick a count, Apply saves it as a new revision and redeploys (the
 * server does that for any runtime change of a deployed deployment).
 */
export default function ScaleControl({ deployment: d, service, canOperate }: { deployment: Deployment; service: string; canOperate: boolean }) {
  const qc = useQueryClient();
  const rule = scaleRule(d, service);
  const [draft, setDraft] = useState<number | null>(null);
  const save = useMutation({
    mutationFn: (n: number) => {
      const input = scaledInput(d, service, n);
      if (!input) throw new Error("The compose file does not parse; fix it in Configuration first.");
      return updateDeployment(d.id, input);
    },
    onSuccess: (_r, n) => {
      notify({ color: "blue", title: `${service}: ${n} replica${n === 1 ? "" : "s"}`, message: d.status === "deployed" ? "Saved as a new revision; redeploying." : "Saved; it applies on the next deploy." });
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["deployments"] });
      void qc.invalidateQueries({ queryKey: ["deployment", d.id] });
    },
    onError: (err) => notify({ color: "red", title: "Scale failed", message: errorMessage(err, "Could not change the replicas") }),
  });
  if (!rule) return null;

  const n = draft ?? rule.replicas;
  const fixed = !canOperate || !!rule.locked;
  const why = !canOperate ? "Operators and admins can change replicas" : rule.locked || rule.limit;
  return (
    <span className="inline-flex items-center gap-[4px]" onClick={(e) => e.stopPropagation()} title={why || undefined}>
      <span className="mr-[2px] font-mono text-[12px] text-muted-foreground uppercase">Replicas</span>
      <Btn size="sm" icon ghost aria-label={`Fewer replicas of ${service}`} disabled={fixed || n <= 0 || save.isPending} onClick={() => setDraft(n - 1)}>
        <Minus className={ICON} strokeWidth={1.5} aria-hidden />
      </Btn>
      <span className="min-w-[20px] text-center font-mono text-[13px] font-semibold" aria-live="polite" aria-label={`${n} replicas of ${service}`}>{n}</span>
      <Btn size="sm" icon ghost aria-label={`More replicas of ${service}`} disabled={fixed || n >= rule.max || save.isPending} onClick={() => setDraft(n + 1)}>
        <Plus className={ICON} strokeWidth={1.5} aria-hidden />
      </Btn>
      {draft != null && draft !== rule.replicas && (
        <>
          <Btn size="sm" solid disabled={save.isPending} aria-busy={save.isPending || undefined} onClick={() => save.mutate(draft)}>
            {save.isPending ? "Applying…" : "Apply"}
          </Btn>
          <Btn size="sm" ghost disabled={save.isPending} onClick={() => setDraft(null)}>Cancel</Btn>
        </>
      )}
    </span>
  );
}
