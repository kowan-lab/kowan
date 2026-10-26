import { useEffect, useState } from "react";
import { Box, Button, Callout, Code, Popup, Text, TextInput } from "@/ui/kit";

export interface ConfirmDeleteModalProps {
  opened: boolean;
  onClose: () => void;
  /** The exact name the user must type to confirm. */
  itemName: string;
  /** What is being deleted, e.g. "workflow" or "deployment". */
  itemKind?: string;
  /** When true, deletion protection blocks the action (button stays disabled). */
  protectedOn?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  /** Optional extra content (e.g. a "also remove containers" note). */
  children?: React.ReactNode;
}

/**
 * A safety-railed delete confirmation (AWS-style): the user must type the exact
 * item name to enable the destructive action, and deletion protection — when
 * enabled — blocks it entirely until turned off.
 */
export default function ConfirmDeleteModal({
  opened,
  onClose,
  itemName,
  itemKind = "item",
  protectedOn = false,
  loading = false,
  onConfirm,
  children,
}: ConfirmDeleteModalProps) {
  const [typed, setTyped] = useState("");

  // Reset the typed confirmation whenever the modal (re)opens.
  useEffect(() => {
    if (opened) setTyped("");
  }, [opened, itemName]);

  const matches = typed.trim() === itemName;
  const canDelete = matches && !protectedOn;

  return (
    <Popup
      open={opened}
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      size="sm"
    >
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Delete {itemKind}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <Box direction="column" gap={4}>
            {protectedOn ? (
              <Callout
                color="warning"
                iconLeft="lock-outline"
                title="Deletion protection is on"
              >
                This {itemKind} is protected. Turn off deletion protection before
                it can be removed.
              </Callout>
            ) : (
              <Callout
                color="danger"
                iconLeft="warning-outline-rounded"
              >
                This action permanently deletes the {itemKind}{" "}
                <Text as="span" weight="semibold">
                  {itemName}
                </Text>{" "}
                and cannot be undone.
              </Callout>
            )}

            {children}

            <Box direction="column" gap={1}>
              <Text size="sm">
                Type <Code>{itemName}</Code> to confirm:
              </Text>
              <TextInput
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                placeholder={itemName}
                disabled={protectedOn}
                fullWidth
                errorText={
                  typed.length > 0 && !matches ? "Name does not match" : undefined
                }
              />
            </Box>
          </Box>
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={loading}>
            Cancel
          </Button>
          <Button
            color="danger"
            disabled={!canDelete}
            loading={loading}
            onClick={onConfirm}
          >
            Delete {itemKind}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
