import type { ReactNode } from "react";
import { KowanMark } from "./KowanMark";

/** Full-viewport frame for sign-in and first-run screens. */
export default function AuthShell({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="app-canvas flex min-h-svh flex-col items-center justify-center px-6 py-15">
      <div className="frame w-full max-w-[380px] bg-card">
        <div className="flex items-center gap-[8px] border-b px-9 py-6">
          <KowanMark className="size-8" />
          <span className="text-[15px] font-bold tracking-normal ">kowan</span>
          <span className="ml-auto text-[12px] text-muted-foreground">docker control plane</span>
        </div>
        <div className="flex flex-col gap-2 px-9 pt-8">
          <h1 className="m-0 text-lg font-semibold tracking-[-0.02em]">{title}</h1>
          {subtitle && <p className="m-0 text-[13px] text-muted-foreground">{subtitle}</p>}
        </div>
        <div className="px-9 pt-8 pb-9">{children}</div>
      </div>
      {footer && <div className="pt-6 text-[12px] text-muted-foreground">{footer}</div>}
    </div>
  );
}
