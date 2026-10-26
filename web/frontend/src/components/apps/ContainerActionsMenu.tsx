// The "⋯" menu of one container in a workload: the lifecycle actions that make
// sense in its state (start, stop, restart, pause/unpause, kill, remove), so a
// container can be handled from the workload without opening its page. Kill and
// remove ask first. Hidden from roles that cannot operate.

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";
import { Checkbox, Menu, Text } from "@/ui/kit";
import { containerAction, removeContainer } from "../../api/client";
import type { Container, ContainerActionType } from "../../types";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import ConfirmModal from "../ConfirmModal";
import { useCanOperate } from "../deployment/shared";
import { Btn } from "./ui";

const DONE: Record<ContainerActionType, string> = {
  start: "Start requested",
  stop: "Stop requested",
  restart: "Restart requested",
  pause: "Paused",
  unpause: "Unpaused",
  kill: "Killed",
};

export default function ContainerActionsMenu({ container: c, hostId, name, managed }: { container: Container; hostId: string; name: string; managed?: boolean }) {
  const canOperate = useCanOperate();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<null | "kill" | "remove">(null);
  const [force, setForce] = useState(false);
  const refresh = () => qc.invalidateQueries({ predicate: (q) => String(q.queryKey[0]).startsWith("containers") });

  const act = useMutation({
    mutationFn: (action: ContainerActionType) => containerAction(hostId, c.id, action),
    onSuccess: (_d, action) => {
      notify({ color: "green", title: name, message: DONE[action] });
      void refresh();
    },
    onError: (err, action) => notify({ color: "red", title: `Could not ${action} ${name}`, message: errorMessage(err, "The Docker daemon rejected the request") }),
    onSettled: () => setConfirm(null),
  });
  const remove = useMutation({
    mutationFn: () => removeContainer(hostId, c.id, force),
    onSuccess: () => {
      notify({ color: "green", title: "Container removed", message: `${name} was removed` });
      void refresh();
    },
    onError: (err) => notify({ color: "red", title: `Could not remove ${name}`, message: errorMessage(err, "The Docker daemon rejected the request") }),
    onSettled: () => setConfirm(null),
  });

  if (!canOperate) return null;
  const state = c.state;
  const running = state === "running";
  const paused = state === "paused";
  const live = running || paused || state === "restarting";

  return (
    <>
      <Menu placement="bottom-end">
        <Menu.Trigger>
          <Btn icon size="sm" ghost aria-label={`Actions for ${name}`} title="Actions" disabled={act.isPending || remove.isPending}>
            <MoreHorizontal className="size-[16px] shrink-0" strokeWidth={1.5} aria-hidden />
          </Btn>
        </Menu.Trigger>
        <Menu.Content ariaLabel={`Actions for ${name}`}>
          {!live && state !== "removing" && <Menu.Item iconLeft="play-arrow-rounded" onSelect={() => act.mutate("start")}>Start</Menu.Item>}
          {running && <Menu.Item iconLeft="refresh-rounded" onSelect={() => act.mutate("restart")}>Restart</Menu.Item>}
          {live && <Menu.Item iconLeft="stop-rounded" onSelect={() => act.mutate("stop")}>Stop</Menu.Item>}
          {running && <Menu.Item iconLeft="pause-rounded" onSelect={() => act.mutate("pause")}>Pause</Menu.Item>}
          {paused && <Menu.Item iconLeft="play-arrow-rounded" onSelect={() => act.mutate("unpause")}>Unpause</Menu.Item>}
          <Menu.Separator />
          {live && <Menu.Item iconLeft="cancel-outline-rounded" color="danger" onSelect={() => setConfirm("kill")}>Kill…</Menu.Item>}
          <Menu.Item
            iconLeft="delete-outline-rounded"
            color="danger"
            onSelect={() => {
              setForce(live);
              setConfirm("remove");
            }}
          >
            Remove…
          </Menu.Item>
        </Menu.Content>
      </Menu>

      <ConfirmModal
        opened={confirm === "kill"}
        onClose={() => setConfirm(null)}
        title={`Kill ${name}`}
        confirmLabel="Kill container"
        loading={act.isPending}
        onConfirm={() => act.mutate("kill")}
        message={<><Text as="span" weight="semibold">{name}</Text> is stopped at once with SIGKILL, without the grace period Stop gives it to shut down cleanly.</>}
      />
      <ConfirmModal
        opened={confirm === "remove"}
        onClose={() => setConfirm(null)}
        title={`Remove ${name}`}
        confirmLabel="Remove container"
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
        message={
          <>
            <Text as="span" weight="semibold">{name}</Text> and its writable layer are deleted; named volumes and bind mounts are kept.
            {managed && " It belongs to a deployment or a compose stack, whose next deploy creates it again."}
          </>
        }
      >
        <Checkbox label={live ? "Force: stop it first (required while it runs)" : "Force remove"} checked={force} onChange={(e) => setForce(e.target.checked)} />
      </ConfirmModal>
    </>
  );
}
