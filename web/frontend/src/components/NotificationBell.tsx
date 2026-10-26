import { Bell } from "lucide-react";
import { useState } from "react";
import {
  Box,
  Button,
  Icon,
  Popover,
  Spinner,
  Text,
} from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  unreadNotificationCount,
} from "../api/client";
import type { Notification } from "../types";
import { levelColor, timeAgo } from "../utils";
import { softChip } from "../ui/colors";

/**
 * NotificationBell is the top-bar notification center: a bell with an unread
 * badge that opens a feed of recent alerts and system notices. The unread count
 * is polled in the background; the full list is fetched when the popover opens.
 */
export default function NotificationBell({
  placement = "bottom-end",
}: {
  placement?: "bottom-end" | "top-start";
} = {}) {
  const [opened, setOpened] = useState(false);
  const queryClient = useQueryClient();

  // Poll the unread count in the background so the badge stays fresh without
  // holding the whole list in memory.
  const unreadQuery = useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: unreadNotificationCount,
    refetchInterval: 20_000,
  });
  const unread = unreadQuery.data ?? 0;

  // Fetch the list only while the popover is open.
  const listQuery = useQuery({
    queryKey: ["notifications", "list"],
    queryFn: () => listNotifications(50),
    enabled: opened,
  });
  const items = listQuery.data ?? [];

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  const readMutation = useMutation({
    mutationFn: (id: string) => markNotificationRead(id),
    onSuccess: invalidate,
  });

  const readAllMutation = useMutation({
    mutationFn: () => markAllNotificationsRead(),
    onSuccess: invalidate,
  });

  return (
    <Popover open={opened} onOpenChange={setOpened} placement={placement}>
      <Popover.Trigger>
        <button
          type="button"
          aria-label={unread > 0 ? `Notifications, ${unread} unread` : "Notifications"}
          className="relative inline-flex size-18 items-center justify-center bg-[var(--popover)] text-foreground shadow-[inset_0_0_0_1px_var(--border)] outline-none hover:bg-muted"
        >
          <Bell className="size-8" strokeWidth={1.5} aria-hidden />
          {unread > 0 && (
            <span
              aria-hidden
              className="absolute -top-[4px] -right-[4px] inline-flex h-8 min-w-8 items-center justify-center bg-[var(--tone-red-solid)] px-[4px] font-mono text-[12px] leading-none font-bold text-white"
            >
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>
      </Popover.Trigger>

      <Popover.Content noPadding ariaLabel="Notifications">
        <Box direction="column" style={{ width: 380 }}>
          <Box
            direction="row"
            justify="between"
            align="center"
            style={{ padding: "10px 16px" }}
          >
            <Text size="sm" weight="semibold">
              Notifications
            </Text>
            <Button
              size="sm"
              variant="clear"
              iconLeft="done-all-rounded"
              disabled={unread === 0 || readAllMutation.isPending}
              onClick={() => readAllMutation.mutate()}
            >
              Mark all read
            </Button>
          </Box>

          <Box style={{ borderTop: "1px solid var(--border)" }} />

          {listQuery.isLoading ? (
            <Box justify="center" style={{ padding: "32px 0" }}>
              <Spinner size="sm" />
            </Box>
          ) : items.length === 0 ? (
            <Box
              direction="column"
              align="center"
              gap={1}
              style={{ padding: "32px 16px" }}
            >
              <Icon
                name="notifications-outline-rounded"
                size="lg"
                color="neutral"
              />
              <Text size="sm" color="muted">
                You're all caught up
              </Text>
            </Box>
          ) : (
            <Box
              direction="column"
              style={{ maxHeight: 420, overflowY: "auto" }}
            >
              {items.map((n) => (
                <NotificationRow
                  key={n.id}
                  n={n}
                  onClick={() => !n.read && readMutation.mutate(n.id)}
                />
              ))}
            </Box>
          )}
        </Box>
      </Popover.Content>
    </Popover>
  );
}

function NotificationRow({
  n,
  onClick,
}: {
  n: Notification;
  onClick: () => void;
}) {
  return (
    <Box
      as="button"
      onClick={onClick}
      direction="row"
      align="start"
      gap={2}
      style={{
        width: "100%",
        textAlign: "left",
        border: "none",
        cursor: "pointer",
        padding: "10px 16px",
        borderTop: "1px solid var(--border)",
        background: n.read ? "transparent" : "var(--muted)",
      }}
    >
      <Box
        aria-hidden
        style={{
          marginTop: 6,
          width: 8,
          height: 8,
          borderRadius: "50%",
          flexShrink: 0,
          background: softChip(levelColor(n.level)).color,
        }}
      />
      <Box direction="column" gap={1} style={{ minWidth: 0, flex: 1 }}>
        <Box direction="row" justify="between" align="center" gap={2}>
          <Text size="sm" weight={n.read ? "regular" : "semibold"} truncate>
            {n.title}
          </Text>
          <Text size="xs" color="muted" style={{ flexShrink: 0 }}>
            {timeAgo(n.createdAt)}
          </Text>
        </Box>
        {n.body && (
          <Text size="xs" color="muted" lines={2}>
            {n.body}
          </Text>
        )}
      </Box>
    </Box>
  );
}
