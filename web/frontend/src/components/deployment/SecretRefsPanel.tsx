// "Write into the compose": the [[secret]] references of an inline compose
// deployment, any of which an admin can replace with its value (after an
// import, an image tag or a port is easier to work with written in). What
// runs does not change; the result is a revision, so a rollback undoes it.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Code, Text } from "@/ui/kit";
import { inlineSecrets } from "../../api/client";
import type { Deployment } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";

// Same token as the server (kowan/compose/lint.py SECRET_TOKEN).
const TOKEN = /\[\[\s*([A-Za-z0-9_.:-]+?)\s*\]\]/g;

export function secretRefs(compose: string): string[] {
  return [...new Set([...compose.matchAll(TOKEN)].map((m) => m[1]))].sort();
}

export default function SecretRefsPanel({ deployment: d, dirty, onSaved }: { deployment: Deployment; dirty: boolean; onSaved: (d: Deployment) => void }) {
  const qc = useQueryClient();
  const refs = secretRefs(d.compose ?? "");
  const [picked, setPicked] = useState<string[]>([]);
  const chosen = picked.filter((r) => refs.includes(r));
  const write = useMutation({
    mutationFn: () => inlineSecrets(d.id, chosen),
    onSuccess: (saved) => {
      setPicked([]);
      qc.setQueryData(["deployment", d.id], saved);
      void qc.invalidateQueries({ queryKey: ["deployments"] });
      onSaved(saved);
      notify({ color: "green", title: "Written into the compose", message: `${chosen.length} value${chosen.length === 1 ? "" : "s"}, saved as a new revision.` });
    },
    onError: (err) => notify({ color: "red", title: "Not written", message: errorMessage(err, "Could not write the values") }),
  });
  if (!refs.length) return null;
  return (
    <section aria-labelledby="secret-refs-title" className="flex flex-col gap-[10px] shadow-[inset_0_0_0_1px_var(--border)] bg-card px-[var(--page-px,16px)] py-5">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 id="secret-refs-title" className="m-0 font-mono text-[13px] leading-[18px] font-medium">Secret values in the compose</h2>
        <Text size="xs" color="muted">Admins only: writing a value in shows it to everyone who can read this deployment.</Text>
      </div>
      <Text size="sm" color="muted">
        These references are filled in from encrypted secrets when the stack deploys. Pick the ones to write into the compose instead, such as an image tag or a port.
      </Text>
      <div className="flex flex-wrap gap-x-[16px] gap-y-[6px]">
        {refs.map((r) => (
          <Checkbox
            key={r}
            size="sm"
            label={<Code>{`[[${r}]]`}</Code>}
            aria-label={`Write ${r} into the compose`}
            checked={chosen.includes(r)}
            onChange={(e) => setPicked((p) => (e.target.checked ? [...p, r] : p.filter((x) => x !== r)))}
          />
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" color="neutral" disabled={!chosen.length || dirty} loading={write.isPending} onClick={() => write.mutate()}>
          Write into the compose
        </Button>
        {dirty && <Text size="xs" color="warning">Save or discard your changes first.</Text>}
      </div>
    </section>
  );
}
