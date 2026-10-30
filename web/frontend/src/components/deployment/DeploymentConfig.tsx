// Configuration tab: the shared DeploymentForm (mode="edit") under a sticky
// save bar that says whether there are unsaved changes and when a save reaches
// the running containers: right away when auto-heal is on and the deployment
// runs (the server redeploys in the background when what runs changes),
// otherwise on the next deploy.

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Text } from "@/ui/kit";
import { updateDeployment, type DeploymentInput } from "../../api/client";
import { notify } from "../../ui/toast";
import {
  DeploymentForm,
  deploymentFormFromDeployment,
  deploymentFormToInput,
  validateDeploymentForm,
  type DeploymentFormValue,
} from "../DeploymentForm";
import type { Deployment } from "../../types";
import { errorMessage } from "../../utils";
import { deployInFlight, useLifecycle } from "./shared";

const signature = (f: DeploymentFormValue) => JSON.stringify(deploymentFormToInput(f));

export default function DeploymentConfig({
  deployment: d,
  canOperate,
  onDirtyChange,
}: {
  deployment: Deployment;
  canOperate: boolean;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const qc = useQueryClient();
  const life = useLifecycle(d);
  const [form, setForm] = useState<DeploymentFormValue>(() => deploymentFormFromDeployment(d));
  const [error, setError] = useState<string | null>(null);

  // Re-hydrate only when another deployment is opened: the 5 s poll must never
  // clobber edits in progress.
  const loadedId = useRef(d.id);
  useEffect(() => {
    if (loadedId.current !== d.id) {
      loadedId.current = d.id;
      setForm(deploymentFormFromDeployment(d));
      setError(null);
    }
  }, [d]);

  const baseline = useMemo(() => signature(deploymentFormFromDeployment(d)), [d]);
  const dirty = signature(form) !== baseline;
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const save = useMutation({
    mutationFn: (input: DeploymentInput) => updateDeployment(d.id, input),
    onSuccess: (saved) => {
      setForm(deploymentFormFromDeployment(saved));
      qc.setQueryData(["deployment", d.id], saved);
      qc.invalidateQueries({ queryKey: ["deployments"] });
    },
    onError: (err) => setError(errorMessage(err, "Could not save the configuration")),
  });

  const submit = async (redeploy: boolean) => {
    const msg = validateDeploymentForm(form);
    if (msg) {
      setError(msg);
      return;
    }
    setError(null);
    let saved: Deployment;
    try {
      // The name is not edited here (the page header owns it); keep it.
      const input = deploymentFormToInput(form);
      // Preview settings are edited on the overview, not here: keep them.
      if (input.git) input.git = { ...input.git, previews: d.git?.previews ?? null };
      saved = await save.mutateAsync({ ...input, name: d.name } as DeploymentInput);
    } catch {
      return;
    }
    if (deployInFlight(saved)) {
      // Auto-heal on a running deployment: the server is already applying it.
      notify({ color: "green", title: "Configuration saved", message: `${d.name} is redeploying to apply it.` });
    } else if (redeploy) {
      life.run("deploy");
    } else {
      notify({ color: "green", title: "Configuration saved", message: `Redeploy ${d.name} to apply it to the running containers.` });
    }
  };

  const autoApplies = d.enabled && (d.status === "deployed" || deployInFlight(d));

  const reset = () => {
    setForm(deploymentFormFromDeployment(d));
    setError(null);
  };

  return (
    <div className="flex flex-col">
      {canOperate ? (
        <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-x-6 gap-y-3 shadow-[inset_0_0_0_1px_var(--border)] bg-card px-[var(--page-px,16px)] py-3">
          <Text size="sm" color={dirty ? "warning" : "muted"} role="status">
            {dirty
              ? "Unsaved changes"
              : autoApplies
                ? "Saved. Auto-heal is on: a change to what runs redeploys it right away."
                : "Saved. Changes apply to the containers on the next deploy."}
          </Text>
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="clear" color="neutral" disabled={!dirty || save.isPending} onClick={reset}>
              Discard
            </Button>
            <Button variant="outline" color="neutral" disabled={!dirty} loading={save.isPending && !life.busy} onClick={() => void submit(false)}>
              Save
            </Button>
            <Button
              iconLeft="rocket-launch-outline-rounded"
              disabled={!dirty || deployInFlight(d)}
              loading={save.isPending || life.acting("deploy")}
              onClick={() => void submit(true)}
            >
              Save and redeploy
            </Button>
          </div>
        </div>
      ) : (
        <Callout color="info" title="Read only">
          Your role can view this configuration but not change it.
        </Callout>
      )}

      {error && (
        <div className="pt-5">
          <Callout color="danger" title="Not saved" onDismiss={() => setError(null)}>
            {error}
          </Callout>
        </div>
      )}

      <div className="shadow-[inset_0_0_0_1px_var(--border)] bg-card px-[var(--page-px,16px)] py-6">
        <fieldset disabled={!canOperate} className="m-0 min-w-0 border-0 p-0">
          <DeploymentForm value={form} onChange={setForm} mode="edit" />
        </fieldset>
      </div>
    </div>
  );
}
