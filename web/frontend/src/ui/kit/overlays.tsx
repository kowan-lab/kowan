import { createContext, useContext, type CSSProperties, type ReactNode } from "react";
import { Dialog as D, DropdownMenu as M, Popover as Pop, Tooltip as T } from "radix-ui";
import { ChevronRight, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { renderIcon } from "./icon";

/* ── Popup (modal dialog) ────────────────────────────────────────────────*/
type PopupSize = "sm" | "md" | "lg" | "xl" | "full";
const WIDTH: Record<PopupSize, string> = {
  sm: "max-w-md", md: "max-w-xl", lg: "max-w-3xl", xl: "max-w-5xl", full: "max-w-[min(1400px,96vw)]",
};
interface PopupCtx { size: PopupSize; dismissable: boolean; esc: boolean }
const PopupContext = createContext<PopupCtx>({ size: "md", dismissable: true, esc: true });

function PopupRoot({
  open, onOpenChange, onClose, size = "md", closeOnBackdropClick = true, closeOnEsc = true, children,
}: {
  open?: boolean; onOpenChange?: (o: boolean) => void; onClose?: () => void; size?: PopupSize;
  closeOnBackdropClick?: boolean; closeOnEsc?: boolean; children?: ReactNode;
}) {
  return (
    <PopupContext.Provider value={{ size, dismissable: closeOnBackdropClick, esc: closeOnEsc }}>
      <D.Root open={open} onOpenChange={(o) => { onOpenChange?.(o); if (!o) onClose?.(); }}>
        {children}
      </D.Root>
    </PopupContext.Provider>
  );
}

function PopupContent({ children, className, style }: { children?: ReactNode; className?: string; style?: CSSProperties }) {
  const { size, dismissable, esc } = useContext(PopupContext);
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-black/55 anim-fade" />
      <D.Content
        aria-describedby={undefined}
        onPointerDownOutside={(e) => { if (!dismissable) e.preventDefault(); }}
        onEscapeKeyDown={(e) => { if (!esc) e.preventDefault(); }}
        onOpenAutoFocus={(e) => {
          // Fields marked data-autofocus win over Radix's first-focusable default.
          const target = (e.currentTarget as HTMLElement | null)?.querySelector<HTMLElement>("[data-autofocus]");
          if (target) { e.preventDefault(); target.focus(); }
        }}
        className={cn(
          "fixed top-1/2 left-1/2 z-50 flex max-h-[min(88vh,960px)] w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col",
          "rounded-lg border bg-popover text-popover-foreground shadow-[0_16px_48px_-12px_rgb(0_0_0/0.45)] outline-none",
          "anim-pop [&>form]:flex [&>form]:min-h-0 [&>form]:flex-1 [&>form]:flex-col",
          WIDTH[size],
          className,
        )}
        style={style}
      >
        {children}
      </D.Content>
    </D.Portal>
  );
}

function PopupHeader({ children }: { children?: ReactNode }) {
  return <div className="flex items-center gap-5 border-b px-8 py-[10px]">{children}</div>;
}
function PopupTitle({ children }: { children?: ReactNode }) {
  return <D.Title className="m-0 flex min-w-0 flex-1 items-center gap-3 text-[14px] font-semibold tracking-tight">{children}</D.Title>;
}
function PopupBody({ children, className, style }: { children?: ReactNode; className?: string; style?: CSSProperties }) {
  return <div data-slot="dialog-body" className={cn("flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-8 py-6", className)} style={style}>{children}</div>;
}
function PopupFooter({ children }: { children?: ReactNode }) {
  return <div className="flex items-center justify-end gap-3 border-t bg-sunken/60 px-8 py-5 rounded-b-lg">{children}</div>;
}
function PopupClose() {
  return (
    <D.Close aria-label="Close" className="ml-auto rounded-md p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal outline-none">
      <X className="size-6" />
    </D.Close>
  );
}

export const Popup = Object.assign(PopupRoot, {
  Content: PopupContent, Header: PopupHeader, Title: PopupTitle, Body: PopupBody, Footer: PopupFooter, Close: PopupClose,
});

/* ── Popover ─────────────────────────────────────────────────────────────*/
type Placement = "bottom-start" | "bottom-end" | "bottom" | "top-start" | "top-end" | "top" | "right" | "left";
function side(p: Placement) {
  const [s, a] = p.split("-") as ["top" | "bottom" | "left" | "right", "start" | "end" | undefined];
  return { side: s, align: a ?? "center" } as const;
}
const PlacementContext = createContext<Placement>("bottom-start");

function PopoverRoot({ open, onOpenChange, placement = "bottom-start", children }: { open?: boolean; onOpenChange?: (o: boolean) => void; placement?: Placement; children?: ReactNode }) {
  return (
    <PlacementContext.Provider value={placement}>
      <Pop.Root open={open} onOpenChange={onOpenChange}>{children}</Pop.Root>
    </PlacementContext.Provider>
  );
}
function PopoverTrigger({ children }: { children?: ReactNode }) {
  return <Pop.Trigger asChild>{children}</Pop.Trigger>;
}
function PopoverContent({ children, noPadding, ariaLabel }: { children?: ReactNode; noPadding?: boolean; ariaLabel?: string }) {
  const p = useContext(PlacementContext);
  return (
    <Pop.Portal>
      <Pop.Content
        {...side(p)}
        sideOffset={6}
        aria-label={ariaLabel}
        className={cn(
          "z-50 rounded-lg border bg-popover text-popover-foreground shadow-[0_12px_32px_-8px_rgb(0_0_0/0.35)] outline-none",
          "anim-pop",
          !noPadding && "p-5",
        )}
      >
        {children}
      </Pop.Content>
    </Pop.Portal>
  );
}
export const Popover = Object.assign(PopoverRoot, { Trigger: PopoverTrigger, Content: PopoverContent });

/* ── Menu (dropdown) ─────────────────────────────────────────────────────*/
const ITEM = "flex cursor-default items-center gap-3 rounded-sm px-3 py-[4px] text-[13px] outline-none select-none data-[highlighted]:bg-accent data-[disabled]:opacity-50";
const MENU_SURFACE = "z-50 min-w-72 overflow-hidden rounded-lg border bg-popover p-2 text-popover-foreground shadow-[0_12px_32px_-8px_rgb(0_0_0/0.35)] anim-pop";

function MenuRoot({ placement = "bottom-start", children }: { placement?: Placement; children?: ReactNode }) {
  return (
    <PlacementContext.Provider value={placement}>
      <M.Root modal={false}>{children}</M.Root>
    </PlacementContext.Provider>
  );
}
function MenuTrigger({ children }: { children?: ReactNode }) {
  return <M.Trigger asChild>{children}</M.Trigger>;
}
function MenuContent({ children, ariaLabel }: { children?: ReactNode; ariaLabel?: string }) {
  const p = useContext(PlacementContext);
  return (
    <M.Portal>
      <M.Content {...side(p)} sideOffset={6} aria-label={ariaLabel} className={MENU_SURFACE}>{children}</M.Content>
    </M.Portal>
  );
}
function MenuItem({
  children, iconLeft, trailing, onSelect, color, disabled,
}: { children?: ReactNode; iconLeft?: string | ReactNode; trailing?: ReactNode; onSelect?: () => void; color?: "danger"; disabled?: boolean }) {
  return (
    <M.Item disabled={disabled} onSelect={onSelect} className={cn(ITEM, color === "danger" && "text-[var(--tone-red-text)]")}>
      <span className="text-muted-foreground">{renderIcon(iconLeft, "sm")}</span>
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {trailing && <span className="ml-6 text-xs text-muted-foreground">{trailing}</span>}
    </M.Item>
  );
}
function MenuLabel({ children }: { children?: ReactNode }) {
  return <M.Label className="px-3 py-[4px] text-[12px] font-medium tracking-wide text-muted-foreground font-mono uppercase">{children}</M.Label>;
}
function MenuSeparator() {
  return <M.Separator className="-mx-2 my-2 h-px bg-border" />;
}
function MenuSub({ label, iconLeft, children }: { label?: ReactNode; iconLeft?: string | ReactNode; children?: ReactNode }) {
  return (
    <M.Sub>
      <M.SubTrigger className={cn(ITEM, "data-[state=open]:bg-accent")}>
        <span className="text-muted-foreground">{renderIcon(iconLeft, "sm")}</span>
        <span className="min-w-0 flex-1 truncate">{label}</span>
        <ChevronRight className="size-[12px] text-muted-foreground" />
      </M.SubTrigger>
      <M.Portal>
        <M.SubContent sideOffset={4} className={MENU_SURFACE}>{children}</M.SubContent>
      </M.Portal>
    </M.Sub>
  );
}
export const Menu = Object.assign(MenuRoot, {
  Trigger: MenuTrigger, Content: MenuContent, Item: MenuItem, Label: MenuLabel, Separator: MenuSeparator, Sub: MenuSub,
});

/* ── Tooltip ─────────────────────────────────────────────────────────────*/
export function TooltipProvider({ children }: { children?: ReactNode }) {
  return <T.Provider delayDuration={350} skipDelayDuration={150}>{children}</T.Provider>;
}

export function Tooltip({
  content, children, placement = "top", disabled,
}: { content?: ReactNode; children?: ReactNode; placement?: Placement; disabled?: boolean }) {
  if (!content || disabled) return <>{children}</>;
  return (
    <T.Root>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content
          {...side(placement)}
          sideOffset={6}
          className="z-50 max-w-108 rounded-md bg-foreground px-3 py-2 text-xs text-background shadow-sm anim-fade"
        >
          {content}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}
