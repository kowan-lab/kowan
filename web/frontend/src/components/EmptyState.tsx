import type { ReactNode } from "react";

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}

/** Empty state: what is missing, why it matters, and the one action that fixes it. */
export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-15 text-center">
      {icon && (
        <div aria-hidden className="mb-2 flex size-15 items-center justify-center rounded-md border bg-background text-muted-foreground">
          {icon}
        </div>
      )}
      <p className="m-0 max-w-[46ch] text-sm font-medium text-foreground">{title}</p>
      {description && <p className="m-0 max-w-[52ch] text-[13px] text-muted-foreground">{description}</p>}
      {action && <div className="pt-3">{action}</div>}
    </div>
  );
}
