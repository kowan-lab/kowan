import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Box, Button, Callout, Checkbox, Popup, Skeleton, Text, TextArea, TextInput } from "@/ui/kit";
import { applyImport, scanMigrate } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import EnvEditor, { envRowsFromApi, envRowsProblem, envRowsToApi, type EnvRow } from "../EnvEditor";

/**
 * Migrate a workload Kowan only discovered into a Kowan deployment, straight
 * from Docker (kowan/importer/fromdocker.py): its original compose files when
 * the host still has them, else a compose file rebuilt from its containers;
 * a lone container from its own settings. Keeping the name keeps the same
 * compose project (or container), so a deploy takes over what runs.
 */
export default function MigrateDialog({ workload, onClose }: { workload: string; onClose: () => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const scan = useQuery({ queryKey: ["migrate-scan", workload], queryFn: () => scanMigrate(workload), retry: false, staleTime: Infinity });
  const c = scan.data;
  const [name, setName] = useState("");
  const [compose, setCompose] = useState("");
  const [vars, setVars] = useState<EnvRow[]>([]);
  const [deploy, setDeploy] = useState(false);
  useEffect(() => {
    if (!c) return;
    setName(c.name);
    setCompose(c.compose ?? "");
    setVars(envRowsFromApi(c.variables));
  }, [c]);
  const varProblem = envRowsProblem(vars, { strictNames: true });
  const apply = useMutation({
    mutationFn: async () => {
      const [r] = await applyImport([c!.type === "container"
        ? { name: name.trim(), host: c!.host, strategy: "standalone", type: "container", spec: c!.spec, deploy }
        : { name: name.trim(), host: c!.host, strategy: c!.strategy, compose, variables: envRowsToApi(vars), deploy }]);
      if (!r.ok) throw new Error(r.error || "Not created");
      return r;
    },
    onSuccess: (r) => {
      notify({
        color: r.deployError ? "yellow" : "green",
        title: `${name} is a Kowan deployment`,
        message: r.deployError ? `Created; the deploy did not start: ${r.deployError}` : deploy ? "Kowan is taking over its containers." : "Deploy it when you are ready: that takes over its containers.",
      });
      void qc.invalidateQueries({ queryKey: ["deployments"] });
      onClose();
      navigate(`/workloads/dep~${r.id}`);
    },
    onError: (err) => notify({ color: "red", title: "Not migrated", message: errorMessage(err, "") }),
  });
  const renamed = !!c && name.trim() !== c.name;
  return (
    <Popup open onOpenChange={(o) => !o && !apply.isPending && onClose()} size="xl">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Migrate to Kowan</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          {scan.isLoading && <Box direction="column" gap={3}><Text size="sm" color="muted">Reading it from Docker…</Text><Skeleton height={200} /></Box>}
          {scan.isError && <Callout color="danger" title="Cannot migrate it">{errorMessage(scan.error, "")}</Callout>}
          {c && (
            <Box direction="column" gap={4}>
              <Callout color="info" title={c.source === "files" ? "From its compose file on the host" : c.source === "rebuilt" ? "Rebuilt from its containers" : "From the container's settings"}>
                {c.source === "files" && <>Read from <code>{c.path}</code>, with its <code>.env</code>. Relative paths now point into that folder.</>}
                {c.source === "rebuilt" && "The compose file was not found on the host, so Kowan wrote one from what Docker reports. Review it: values the image sets are left out."}
                {c.source === "container" && "A container deployment with the same image, environment, ports, volumes and networks."}
              </Callout>
              {c.warnings.length > 0 && (
                <Callout color="yellow" title="Check before deploying">
                  <ul className="m-0 pl-[18px]">{c.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
                </Callout>
              )}
              <TextInput
                label="Deployment name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                description={renamed
                  ? c.type === "container"
                    ? "A different name starts a new container next to the old one: keep the name to take it over."
                    : "A different name is a different compose project: its containers start next to the old ones, with new networks."
                  : "The same name, so deploying takes over what runs now."}
              />
              {c.type === "compose" ? (
                <>
                  <TextArea label="Compose file" rows={16} className="font-mono" value={compose} onChange={(e) => setCompose(e.target.value)} />
                  <Box direction="column" gap={2}>
                    <Text weight="semibold" size="sm">Stack variables</Text>
                    <Text size="xs" color="muted">Secret-looking values were moved here and are stored encrypted.</Text>
                    <EnvEditor value={vars} onChange={setVars} strictNames emptyText="No stack variables." />
                  </Box>
                </>
              ) : (
                <Text size="sm">Image <code>{c.spec?.image}</code>, {c.spec?.env?.length ?? 0} variables, {c.spec?.ports?.length ?? 0} ports, {c.spec?.volumes?.length ?? 0} volumes.</Text>
              )}
              <Checkbox
                label="Deploy now"
                description="Kowan recreates the containers with this configuration: a short restart. Volumes keep their data. Off: only the deployment is created."
                checked={deploy}
                onChange={(e) => setDeploy(e.target.checked)}
              />
            </Box>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={apply.isPending}>Cancel</Button>
          <Button loading={apply.isPending} disabled={!c || !name.trim() || (c.type === "compose" && (!compose.trim() || !!varProblem))} onClick={() => apply.mutate()}>
            {deploy ? "Migrate and deploy" : "Create deployment"}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
