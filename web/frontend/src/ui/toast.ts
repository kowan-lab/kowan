import { toast } from "sonner";

export interface NotifyInput {
  title?: string;
  message?: string;
  /** Colour name: picks the toast kind (success / error / warning / info). */
  color?: string;
  /** Accepted for call-site compatibility; ignored. */
  icon?: unknown;
}

type ToastFn = (msg: string, opts?: { description?: string }) => unknown;

function pick(color?: string): ToastFn {
  switch ((color ?? "").toLowerCase()) {
    case "green": case "teal": case "lime": case "success":
      return toast.success;
    case "red": case "danger": case "critical":
      return toast.error;
    case "yellow": case "orange": case "warning":
      return toast.warning;
    case "blue": case "cyan": case "info":
      return toast.info;
    default:
      return toast;
  }
}

/** Fire a toast: title is the headline, message the description. */
export function notify(input: NotifyInput): void {
  const head = input.title || input.message || "";
  const description = input.title ? input.message : undefined;
  pick(input.color)(head, description ? { description } : undefined);
}

