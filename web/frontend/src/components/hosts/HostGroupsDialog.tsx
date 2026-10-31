import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Popup, TagsInput, Text } from "@/ui/kit";
import { listHosts, setHostTags } from "../../api/client";
import type { Host } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";

const TAG = /^[a-z0-9][a-z0-9._-]{0,39}$/;

/** Every group in use, for suggestions and filters. */
export function useHostGroups(): string[] {
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  return useMemo(() => [...new Set((hosts.data ?? []).flatMap((h) => h.tags ?? []))].sort(), [hosts.data]);
}

/**
 * A host's groups: words such as production or customer-x. Permissions,
 * schedules, runbooks and auto-update policies can target a whole group, so
 * tagging a host brings it under all of them at once.
 */
export default function HostGroupsDialog({ host, onClose }: { host: Host | null; onClose: () => void }) {
  const qc = useQueryClient();
  const groups = useHostGroups();
  const [tags, setTags] = useState<string[]>([]);
  const [error, setError] = useState<string | undefined>();
  useEffect(() => {
    if (host) {
      setTags(host.tags ?? []);
      setError(undefined);
    }
  }, [host]);
  const save = useMutation({
    mutationFn: (vars: { id: string; tags: string[] }) => setHostTags(vars.id, vars.tags),
    onSuccess: (h) => {
      notify({ color: "green", title: "Groups saved", message: h.tags?.length ? `${h.name}: ${h.tags.join(", ")}` : `${h.name} is in no group` });
      void qc.invalidateQueries({ queryKey: ["hosts"] });
      onClose();
    },
    onError: (err) => setError(errorMessage(err, "Could not save the groups")),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!host) return;
    const clean = [...new Set(tags.map((t) => t.trim().toLowerCase()).filter(Boolean))];
    const bad = clean.find((t) => !TAG.test(t));
    if (bad) return setError(`"${bad}": use lowercase letters, digits, dot, dash or underscore`);
    save.mutate({ id: host.id, tags: clean });
  };
  const others = groups.filter((g) => !tags.includes(g));
  return (
    <Popup open={host !== null} onOpenChange={(o) => !o && !save.isPending && onClose()} size="sm">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Groups of {host?.name}</Popup.Title>
        </Popup.Header>
        <form onSubmit={submit}>
          <Popup.Body>
            <Box direction="column" gap={3}>
              <TagsInput label="Groups" placeholder="production, customer-x…" value={tags} onChange={(v) => { setTags(v); setError(undefined); }} />
              {others.length > 0 && (
                <Box direction="row" wrap gap={2} align="center">
                  <Text size="xs" color="muted">In use:</Text>
                  {others.map((g) => (
                    <Button key={g} size="sm" variant="outline" color="neutral" onClick={() => setTags((t) => [...t, g])}>{g}</Button>
                  ))}
                </Box>
              )}
              {error && <Text size="sm" color="danger">{error}</Text>}
              <Text size="xs" color="muted">
                Team permissions, schedules, runbooks and auto-update policies set on a group cover every host in it.
              </Text>
            </Box>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" justify="end" gap={2}>
              <Button variant="outline" color="neutral" onClick={onClose} disabled={save.isPending}>Cancel</Button>
              <Button type="submit" loading={save.isPending}>Save</Button>
            </Box>
          </Popup.Footer>
        </form>
      </Popup.Content>
    </Popup>
  );
}
