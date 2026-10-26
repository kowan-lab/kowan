import { Box, Button, Popup, Text } from "@/ui/kit";
import type { Color } from "@/ui/kit";

export interface ConfirmModalProps {
  opened: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  /** Body sentence(s). Kept a ReactNode so callers can embed <Code>, bold names, etc. */
  message?: React.ReactNode;
  /** Label for the destructive action button. */
  confirmLabel?: string;
  /** Color of the destructive action button. */
  confirmColor?: Color;
  loading?: boolean;
  /** Popup size; defaults to "sm". */
  size?: React.ComponentProps<typeof Popup>["size"];
  /** Extra content rendered below the message (e.g. a force Checkbox or RadioGroup). */
  children?: React.ReactNode;
  /**
   * When true, the dialog cannot be dismissed (backdrop, Esc, Cancel) while the
   * action is in flight — used by delete/prune flows that must not be interrupted
   * mid-request.
   */
  lockWhileLoading?: boolean;
}

/**
 * A lightweight "are you sure?" confirmation dialog: a title, an optional body
 * message (and/or children), a Cancel button, and a single destructive action
 * button that shows a loading spinner. This is the simple counterpart to
 * ConfirmDeleteModal (which additionally requires typing the exact name).
 */
export default function ConfirmModal({
  opened,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = "Delete",
  confirmColor = "danger",
  loading = false,
  size = "sm",
  children,
  lockWhileLoading = false,
}: ConfirmModalProps) {
  const locked = lockWhileLoading && loading;

  return (
    <Popup
      open={opened}
      onOpenChange={(open) => {
        if (!open && !locked) onClose();
      }}
      size={size}
      closeOnBackdropClick={!locked}
      closeOnEsc={!locked}
    >
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{title}</Popup.Title>
        </Popup.Header>
        <Popup.Body>
          <Box direction="column" gap={4}>
            {message && <Text>{message}</Text>}
            {children}
          </Box>
        </Popup.Body>
        <Popup.Footer>
          <Box direction="row" justify="end" gap={2}>
            <Button
              variant="outline"
              color="neutral"
              onClick={onClose}
              disabled={locked}
            >
              Cancel
            </Button>
            <Button color={confirmColor} loading={loading} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </Box>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
