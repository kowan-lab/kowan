import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Box, Button, Callout, InputToggle, Popup, RadioGroup, Radio, TextInput } from "@/ui/kit";
import { notify } from "../../ui/toast";
import { errorMessage, formatDate } from "../../utils";
import { restoreSnapshot, type BackupRun, type Snapshot } from "./api";

/** Restore a snapshot into its volume (stop/start users) or into a new volume. */
export default function RestoreDialog({ snapshot, hostId, volume, onClose, onStarted }: {
  snapshot: Snapshot | null; hostId: string; volume: string; onClose: () => void; onStarted: (run: BackupRun) => void;
}) {
  const [mode, setMode] = useState<"same" | "new">("new");
  const [target, setTarget] = useState("");
  const [stop, setStop] = useState(true);
  const [exact, setExact] = useState(true);
  useEffect(() => {
    if (snapshot) { setMode("new"); setTarget(`${volume}-restore-${snapshot.shortId}`); setStop(true); setExact(true); }
  }, [snapshot, volume]);
  const go = useMutation({
    mutationFn: () => restoreSnapshot({
      hostId, repoId: snapshot!.repoId, snapshotId: snapshot!.id, target: mode === "same" ? volume : target.trim(),
      newVolume: mode === "new", stopContainers: stop, exact,
    }),
    onSuccess: (run) => { notify({ color: "green", title: "Restore started", message: run.jobName }); onStarted(run); onClose(); },
    onError: (e) => notify({ color: "red", title: "Restore failed", message: errorMessage(e, "Could not start the restore") }),
  });
  return (
    <Popup open={!!snapshot} onOpenChange={(o) => { if (!o) onClose(); }} size="md">
      <Popup.Content>
        <Popup.Header><Popup.Title>Restore snapshot {snapshot?.shortId}</Popup.Title><Popup.Close /></Popup.Header>
        <Popup.Body>
          <Box direction="column" gap={1}>
            <span className="text-xs text-muted-foreground">Taken {snapshot ? formatDate(snapshot.time) : ""} · {snapshot?.paths.join(", ")}</span>
          </Box>
          <RadioGroup value={mode} onChange={(v: string) => setMode(v as "same" | "new")}>
            <Radio value="new" label="Into a new volume" description="Safe: nothing existing is touched." />
            <Radio value="same" label={`Into ${volume} (overwrite)`} description="Replaces the volume's content with the snapshot." />
          </RadioGroup>
          {mode === "new" ? (
            <TextInput label="New volume name" value={target} onChange={(e) => setTarget(e.target.value)} />
          ) : (
            <>
              <InputToggle label="Stop containers using the volume, start them after" checked={stop} onChange={(e) => setStop(e.target.checked)} />
              <InputToggle label="Exact restore (delete files not in the snapshot)" checked={exact} onChange={(e) => setExact(e.target.checked)} />
              <Callout color="warning">The current content of {volume} will be overwritten{exact ? " and files added since the snapshot deleted" : ""}.</Callout>
            </>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="outline" color="neutral" onClick={onClose}>Cancel</Button>
          <Button color={mode === "same" ? "danger" : undefined} loading={go.isPending} disabled={mode === "new" && !target.trim()} onClick={() => go.mutate()}>
            Restore
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
