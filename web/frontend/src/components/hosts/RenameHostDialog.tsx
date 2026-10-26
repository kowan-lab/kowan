import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Box, Button, Popup, Text, TextInput } from "@/ui/kit";
import { renameHost } from "../../api/client";
import type { Host } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";

// Same limit as the server (kowan/api/hosts.py MAX_NAME_LENGTH).
const MAX_NAME = 64;

/**
 * Renames a host, the local one included. Only the display name changes: the
 * id, and everything keyed on it, stays. `host` null keeps the dialog closed.
 */
export default function RenameHostDialog({ host, onClose }: { host: Host | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [error, setError] = useState<string | undefined>();

  useEffect(() => {
    if (host) {
      setName(host.name);
      setError(undefined);
    }
  }, [host]);

  const mutation = useMutation({
    mutationFn: (vars: { id: string; name: string }) => renameHost(vars.id, vars.name),
    onSuccess: (h) => {
      notify({ color: "green", title: "Host renamed", message: `It is now called ${h.name}` });
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
      onClose();
    },
    onError: (err) => setError(errorMessage(err, "Could not rename the host")),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!host) return;
    const next = name.trim();
    if (!next) return setError("Enter a name");
    if (next.length > MAX_NAME) return setError(`Use at most ${MAX_NAME} characters`);
    if (next === host.name) return onClose();
    mutation.mutate({ id: host.id, name: next });
  };

  return (
    <Popup
      open={host !== null}
      onOpenChange={(open) => {
        if (!open && !mutation.isPending) onClose();
      }}
      size="sm"
    >
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Rename host</Popup.Title>
        </Popup.Header>
        <form onSubmit={submit}>
          <Popup.Body>
            <Box direction="column" gap={3}>
              <TextInput
                label="Name"
                autoFocus
                maxLength={MAX_NAME}
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setError(undefined);
                }}
                errorText={error}
              />
              <Text size="xs" color="muted">
                Only the name shown in Kowan changes. Deployments, permissions and history stay attached to the host.
              </Text>
            </Box>
          </Popup.Body>
          <Popup.Footer>
            <Box direction="row" justify="end" gap={2}>
              <Button variant="outline" color="neutral" onClick={onClose} disabled={mutation.isPending}>
                Cancel
              </Button>
              <Button type="submit" color="info" loading={mutation.isPending}>
                Rename
              </Button>
            </Box>
          </Popup.Footer>
        </form>
      </Popup.Content>
    </Popup>
  );
}
