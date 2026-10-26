import { useState } from "react";
import { Box, Button, Card, Icon, Text } from "@/ui/kit";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { isAllHosts, useHost } from "../HostContext";
import { createDeployment, type DeploymentInput } from "../api/client";
import {
  DeploymentForm,
  deploymentFormToInput,
  emptyDeploymentForm,
  validateDeploymentForm,
  type DeploymentFormValue,
} from "../components/DeploymentForm";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import {
  CreatePolicies,
  applyCreatePolicies,
  emptyCreatePolicies,
  validateCreatePolicies,
  type CreatePoliciesValue,
} from "../components/deployment/CreatePolicies";

/** Pull a human-friendly error message out of an axios error. */

// ---------------------------------------------------------------------------
// DeploymentCreate: the full-screen "New deployment" page (route
// /deployments/new). It renders the shared DeploymentForm with every setting
// (the same editor the detail Config tab uses), so creation is no longer
// limited to the old cramped quick-create modal. On success it navigates to the
// new deployment's detail page.
// ---------------------------------------------------------------------------

export default function DeploymentCreate() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Start on the host picked in the shell (the "New workload on <host>" menu
  // promises it); "All hosts" leaves the choice to the form.
  const { hostId } = useHost();
  const [form, setForm] = useState<DeploymentFormValue>(() => ({
    ...emptyDeploymentForm(),
    targets: isAllHosts(hostId) ? [] : [hostId],
  }));
  const [policies, setPolicies] = useState<CreatePoliciesValue>(emptyCreatePolicies);
  const [error, setError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: async (input: DeploymentInput) => {
      const d = await createDeployment(input);
      // The record exists from here on: what follows can fail one by one.
      const results = await applyCreatePolicies(d, policies);
      return { d, results };
    },
    onSuccess: ({ d, results }) => {
      const failed = results.filter((r) => !r.ok);
      if (failed.length === 0) {
        notify({
          color: "success",
          title: "Deployment created",
          message: results.length ? `"${d.name}" was created with ${results.map((r) => r.label.toLowerCase()).join(", ")}.` : `"${d.name}" was created`,
        });
      } else {
        notify({
          color: "warning",
          title: `"${d.name}" was created, but not everything was set`,
          message: failed.map((r) => `${r.label}: ${r.error ?? "failed"}`).join(" · ") + ". Finish it on the workload page.",
        });
      }
      queryClient.invalidateQueries({ queryKey: ["deployments"] });
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      queryClient.invalidateQueries({ queryKey: ["volbackups"] });
      navigate(`/workloads/dep~${d.id}`);
    },
    onError: (err) => {
      notify({
        color: "danger",
        title: "Create failed",
        message: errorMessage(err, "Could not create the deployment"),
      });
    },
  });

  const submit = () => {
    const msg = validateDeploymentForm(form) ?? validateCreatePolicies(policies);
    if (msg) {
      setError(msg);
      return;
    }
    setError(null);
    createMutation.mutate(deploymentFormToInput(form) as DeploymentInput);
  };

  return (
    <Box direction="column" gap={5} style={{ width: "100%" }}>
      <PageHeader
        title="New deployment"
        description="Configure the deployment, then choose what to set up with it: deploy, updates, backups, schedule, domain."
        icon={<Icon name="rocket-launch-outline-rounded" size="lg" />}
        breadcrumbs={[
          { label: "Workloads", to: "/workloads" },
          { label: "New deployment" },
        ]}
        actions={
          <>
            <Button
              variant="outline"
              onClick={() => navigate("/workloads")}
              disabled={createMutation.isPending}
            >
              Cancel
            </Button>
            <Button color="info" onClick={submit} loading={createMutation.isPending}>
              Create
            </Button>
          </>
        }
      />

      <Card variant="raised">
        <Card.Body>
          <DeploymentForm value={form} onChange={setForm} mode="create" />
        </Card.Body>
      </Card>

      <SectionCard
        title="After creating"
        description="The same settings as on the workload page, applied as soon as the deployment is saved. You can change any of them later."
        noBodyPadding
      >
        <CreatePolicies
          value={policies}
          onChange={setPolicies}
          hostId={form.targets[0] ?? ""}
          compose={form.type === "compose" && form.source === "inline" ? form.compose : undefined}
        />
      </SectionCard>

      {error && (
        <Text color="danger" size="sm">
          {error}
        </Text>
      )}
    </Box>
  );
}
