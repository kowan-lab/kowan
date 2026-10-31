import { useSearchParams } from "react-router";
import { Box } from "@/ui/kit";
import PageHeader from "../components/PageHeader";
import { useCanOperate } from "../components/deployment/shared";
import { RunbookList } from "../components/runbooks/Runbooks";

/** Every runbook, also the ones spanning several workloads. */
export default function Runbooks() {
  const canOperate = useCanOperate();
  const [params] = useSearchParams();
  return (
    <Box direction="column" gap={6}>
      <PageHeader
        title="Runbooks"
        description="Named actions on your workloads: a command in a running container or in a throwaway copy of a service, one or more steps, with parameters. Anyone with access runs them with a click; every run is kept with its output. Container labels kowan.runbook.<name>.command declare them too."
      />
      <RunbookList canOperate={canOperate} openRun={params.get("run")} />
    </Box>
  );
}
