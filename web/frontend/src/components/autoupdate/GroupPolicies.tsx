import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Select, Table, Text, type Column } from "@/ui/kit";
import { listGroupPolicies, setGroupPolicy } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import type { GroupPolicy } from "../../types";
import SectionCard from "../SectionCard";
import { MODE_OPTIONS } from "./labels";

/**
 * A host group's policy: the default of every container and deployment on
 * its hosts that has no policy of its own (Kowan's own containers excepted,
 * they update through Kowan's self-update).
 */
export default function GroupPolicies({ canEdit }: { canEdit: boolean }) {
  const qc = useQueryClient();
  const groups = useQuery({ queryKey: ["autoupdate-groups"], queryFn: listGroupPolicies });
  const save = useMutation({
    mutationFn: (v: { tag: string; mode: string }) => setGroupPolicy(v.tag, v.mode),
    onSuccess: (_d, v) => {
      notify({ color: "green", title: `Group ${v.tag}`, message: `Auto-update: ${MODE_OPTIONS.find((m) => m.value === v.mode)?.label ?? v.mode}` });
      void qc.invalidateQueries({ queryKey: ["autoupdate-groups"] });
    },
    onError: (err) => notify({ color: "red", title: "Not saved", message: errorMessage(err, "") }),
  });
  const columns: Column<GroupPolicy>[] = [
    { key: "tag", header: "Group", accessor: (g) => g.tag, cell: (g) => <Text weight="semibold">{g.tag}</Text> },
    { key: "hosts", header: "Hosts", accessor: (g) => g.hosts.length, cell: (g) => <Text size="sm">{g.hosts.length}</Text> },
    {
      key: "mode",
      header: "Default mode",
      cell: (g) =>
        canEdit ? (
          <Select
            aria-label={`Auto-update mode of group ${g.tag}`}
            width={180}
            value={g.stored?.mode ?? "off"}
            options={MODE_OPTIONS.map((m) => ({ value: m.value, label: m.label }))}
            onChange={(v) => v && save.mutate({ tag: g.tag, mode: v })}
          />
        ) : (
          <Text size="sm">{MODE_OPTIONS.find((m) => m.value === (g.stored?.mode ?? "off"))?.label}</Text>
        ),
    },
  ];
  return (
    <SectionCard
      title="Host groups"
      description="The default for every container and deployment on a group's hosts that has no policy of its own. Groups are set on the Hosts page."
      noBodyPadding
    >
      <Table<GroupPolicy> data={groups.data ?? []} columns={columns} rowKey={(g) => g.tag} loading={groups.isLoading}
        emptyText="No host groups yet: give hosts a group on the Hosts page." />
    </SectionCard>
  );
}
