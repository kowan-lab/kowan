import {
  Children,
  Fragment,
  createContext,
  useContext,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Accordion as A, Tabs as TabsPrimitive } from "radix-ui";
import { ChevronDown, GripVertical } from "lucide-react";
import { cn } from "@/lib/utils";
import { renderIcon } from "./icon";
import { InPanel } from "./actions";

/* ── Tabs ────────────────────────────────────────────────────────────────
 * "line" (default): page sections, as a toolbar card like the Workloads
 * list's (inside a panel: plain underline tabs, no second frame).
 * "segmented": a pill control for switching modes inside a panel. */
type TabsVariant = "line" | "segmented";
const TabsVariantContext = createContext<{ variant: TabsVariant; fullWidth?: boolean }>({ variant: "line" });

function TabsRoot({
  value, defaultValue, onValueChange, variant = "line", fullWidth, children, className, style,
}: {
  value?: string; defaultValue?: string; onValueChange?: (v: string) => void; variant?: TabsVariant;
  fullWidth?: boolean; children?: ReactNode; className?: string; style?: CSSProperties;
}) {
  const inPanel = useContext(InPanel);
  const card = variant === "line" && !inPanel;
  return (
    <TabsVariantContext.Provider value={{ variant, fullWidth }}>
      <TabsPrimitive.Root value={value} defaultValue={defaultValue} onValueChange={onValueChange} className={cn("flex min-w-0 flex-col", card ? "gap-[20px]" : "gap-6", className)} style={style}>
        {children}
      </TabsPrimitive.Root>
    </TabsVariantContext.Provider>
  );
}

function TabsList({ children }: { children?: ReactNode }) {
  const { variant, fullWidth } = useContext(TabsVariantContext);
  const inPanel = useContext(InPanel);
  return (
    <TabsPrimitive.List
      className={cn(
        variant === "line"
          ? inPanel
            ? "flex items-stretch gap-[4px] overflow-x-auto overflow-y-hidden shadow-[inset_0_-1px_0_var(--border)]"
            : "flex items-stretch gap-[4px] overflow-x-auto overflow-y-hidden bg-card px-[12px] shadow-[inset_0_0_0_1px_var(--border)]"
          : "inline-flex items-center gap-[2px] self-start rounded-lg bg-muted p-[2px]",
        fullWidth && "w-full self-stretch [&>*]:flex-1",
      )}
    >
      {children}
    </TabsPrimitive.List>
  );
}

function TabsTrigger({ value, disabled, iconLeft, children }: { value: string; disabled?: boolean; iconLeft?: string | ReactNode; children?: ReactNode }) {
  const { variant } = useContext(TabsVariantContext);
  return (
    <TabsPrimitive.Trigger
      value={value}
      disabled={disabled}
      className={cn(
        "inline-flex items-center justify-center gap-4 whitespace-nowrap outline-none transition-colors",
        "focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal disabled:pointer-events-none disabled:opacity-45",
        variant === "line"
          ? "h-[52px] border-b-2 border-transparent px-[12px] text-[14px] leading-none font-semibold text-muted-foreground hover:text-foreground focus-visible:-outline-offset-2 data-[state=active]:border-[var(--foreground)] data-[state=active]:text-foreground"
          : "h-11 px-5 text-muted-foreground hover:text-foreground data-[state=active]:bg-foreground data-[state=active]:text-background",
      )}
    >
      {renderIcon(iconLeft, "sm")}
      {children}
    </TabsPrimitive.Trigger>
  );
}

function TabsPanel({ value, forceMount, children, className, style }: { value: string; forceMount?: boolean; children?: ReactNode; className?: string; style?: CSSProperties }) {
  return (
    <TabsPrimitive.Content
      value={value}
      forceMount={forceMount ? true : undefined}
      className={cn("min-w-0 outline-none data-[state=inactive]:hidden", className)}
      style={style}
    >
      {children}
    </TabsPrimitive.Content>
  );
}

export const Tabs = Object.assign(TabsRoot, { List: TabsList, Trigger: TabsTrigger, Panel: TabsPanel });

/* ── Accordion ───────────────────────────────────────────────────────────*/
function AccordionRoot({
  value, onValueChange, children,
}: { type?: "multiple"; variant?: "separated" | "default"; value?: string[]; onValueChange?: (v: string[]) => void; children?: ReactNode }) {
  return (
    <A.Root type="multiple" value={value} onValueChange={onValueChange} className="flex flex-col gap-3">
      {children}
    </A.Root>
  );
}
function AccordionItem({ value, children }: { value: string; children?: ReactNode }) {
  return <A.Item value={value} className="overflow-hidden border-b bg-transparent">{children}</A.Item>;
}
function AccordionTrigger({ children }: { children?: ReactNode }) {
  return (
    <A.Header className="m-0 flex">
      <A.Trigger className="group flex flex-1 items-center gap-5 px-6 py-5 text-left outline-none hover:bg-accent/50 focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal">
        {children}
        <ChevronDown className="size-6 shrink-0 text-muted-foreground transition-transform duration-200 group-data-[state=open]:rotate-180" />
      </A.Trigger>
    </A.Header>
  );
}
function AccordionContent({ children }: { children?: ReactNode }) {
  return (
    <A.Content className="overflow-hidden">
      <div className="p-6">{children}</div>
    </A.Content>
  );
}
export const Accordion = Object.assign(AccordionRoot, { Item: AccordionItem, Trigger: AccordionTrigger, Content: AccordionContent });

/* ── Breadcrumb ──────────────────────────────────────────────────────────*/
function BreadcrumbRoot({ separator = "/", children }: { separator?: ReactNode; children?: ReactNode }) {
  const items = Children.toArray(children);
  return (
    <nav aria-label="Breadcrumb">
      <ol className="m-0 flex list-none flex-wrap items-center gap-[4px] p-0 text-sm">
        {items.map((c, i) => (
          <Fragment key={i}>
            {i > 0 && <li aria-hidden className="text-muted-foreground/60">{separator}</li>}
            {c}
          </Fragment>
        ))}
      </ol>
    </nav>
  );
}
function BreadcrumbItem({ children }: { children?: ReactNode }) {
  return <li className="inline-flex items-center">{children}</li>;
}
export const Breadcrumb = Object.assign(BreadcrumbRoot, { Item: BreadcrumbItem });

/* ── Sortable (native drag + keyboard Alt+↑/↓) ───────────────────────────*/
export function Sortable<T>({
  items, getKey, onChange, renderItem,
}: {
  items: T[]; getKey: (item: T, i: number) => string | number; onChange: (next: T[]) => void;
  renderItem: (item: T, ctx: { index: number }) => ReactNode; bordered?: boolean;
}) {
  const [drag, setDrag] = useState<number | null>(null);
  const [over, setOver] = useState<number | null>(null);
  const move = (from: number, to: number) => {
    if (from === to || to < 0 || to >= items.length) return;
    const next = items.slice();
    const [it] = next.splice(from, 1);
    next.splice(to, 0, it);
    onChange(next);
  };
  return (
    <ol className="m-0 flex list-none flex-col gap-3 p-0">
      {items.map((item, i) => (
        <li
          key={getKey(item, i)}
          onDragOver={(e) => { e.preventDefault(); setOver(i); }}
          onDrop={(e) => { e.preventDefault(); if (drag !== null) move(drag, i); setDrag(null); setOver(null); }}
          className={cn("flex items-stretch gap-[4px] rounded-lg transition-opacity", drag === i && "opacity-50", over === i && drag !== null && drag !== i && "ring-2 ring-ring/40")}
        >
          <button
            type="button"
            draggable
            aria-label={`Reorder item ${i + 1}. Alt plus arrow keys move it.`}
            onDragStart={() => setDrag(i)}
            onDragEnd={() => { setDrag(null); setOver(null); }}
            onKeyDown={(e) => {
              if (!e.altKey) return;
              if (e.key === "ArrowUp") { e.preventDefault(); move(i, i - 1); }
              if (e.key === "ArrowDown") { e.preventDefault(); move(i, i + 1); }
            }}
            className="flex w-9 cursor-grab items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal outline-none active:cursor-grabbing"
          >
            <GripVertical className="size-6" />
          </button>
          <div className="flex min-w-0 flex-1"><InPanel.Provider value={true}>{renderItem(item, { index: i })}</InPanel.Provider></div>
        </li>
      ))}
    </ol>
  );
}
