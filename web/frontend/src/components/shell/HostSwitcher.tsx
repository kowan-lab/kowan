import { Check, ChevronDown, Layers3, Plus, Server } from "lucide-react";
import { DropdownMenu as M } from "radix-ui";
import { useNavigate } from "react-router";
import { ALL_HOSTS, useHost } from "@/HostContext";
import { useHosts } from "@/lib/hostsQuery";
import { cn } from "@/lib/utils";
import { useSidebar } from "@/components/ui/sidebar";
import type { Host } from "@/types";

/** Online dot: green when the agent is connected, gray otherwise. */
function Dot({ online }: { online: boolean }) {
  return (
    <span
      aria-hidden
      className={cn("size-3 shrink-0 rounded-full", online ? "bg-[var(--tone-green-solid)] shadow-[0_0_0_3px_var(--tone-green-soft)]" : "bg-[var(--tone-gray-solid)]")}
    />
  );
}

/**
 * Host scope: the block at the top of the sidebar that names the current scope
 * IS the selector. "All hosts" fans lists out to every online host. Radix gives
 * the list keyboard behaviour (arrows, Enter, Esc, type-ahead).
 */
export default function HostSwitcher() {
  const { hostId, setHostId } = useHost();
  const navigate = useNavigate();
  // A page about one host's objects means nothing on another: start over at its overview.
  const pick = (id: string) => {
    setHostId(id);
    navigate("/");
  };
  const hosts = useHosts();
  const list: Host[] = hosts.data ?? [];
  const current = list.find((h) => h.id === hostId);
  const online = list.filter((h) => h.online).length;
  const label = hostId === ALL_HOSTS ? "All hosts" : current?.name || hostId;
  const sub =
    hostId === ALL_HOSTS
      ? `${list.length} host${list.length === 1 ? "" : "s"} · ${online} online`
      : current
        ? `${current.mode || current.type} · ${current.online ? "online" : "offline"}`
        : "";
  const { state } = useSidebar();
  const collapsed = state === "collapsed";

  return (
    <M.Root modal={false}>
      <M.Trigger
        aria-label={`Host scope: ${label}. Change host`}
        className={cn(
          "group/scope flex w-full shrink-0 items-center text-left outline-none shadow-[inset_0_-1px_0_var(--sidebar-border)] transition-colors hover:bg-accent focus-visible:outline-solid focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-signal data-[state=open]:bg-[var(--popover)]",
          collapsed ? "h-24 justify-center" : "gap-4 px-8 pt-7 pb-6",
        )}
      >
        {collapsed ? (
          hostId === ALL_HOSTS ? <Layers3 className="size-8 text-muted-foreground" /> : <Dot online={!!current?.online} />
        ) : (
          <>
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-4 text-[18px] leading-[24px] font-extrabold tracking-[-0.01em] uppercase">
                {hostId === ALL_HOSTS ? <Layers3 className="size-8 shrink-0 text-muted-foreground" /> : <Dot online={!!current?.online} />}
                <span className="truncate">{label}</span>
              </span>
              {sub && <span className="pt-[2px] truncate text-[13px] leading-[18px] text-muted-foreground">{sub}</span>}
            </span>
            <ChevronDown className="size-8 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/scope:rotate-180" />
          </>
        )}
      </M.Trigger>
      <M.Portal>
        <M.Content
          side={collapsed ? "right" : "bottom"}
          align="start"
          sideOffset={6}
          className="anim-pop z-50 w-[360px] bg-popover p-3 text-popover-foreground shadow-[inset_0_0_0_1px_var(--border),0_8px_24px_-6px_rgb(40_40_36/0.22)]"
        >
          <M.Label className="px-3 py-[4px] text-[12px] font-medium tracking-wide text-muted-foreground font-mono uppercase">Scope</M.Label>
          <M.Item onSelect={() => pick(ALL_HOSTS)} className="flex cursor-default items-center gap-[8px] rounded-sm px-3 py-[4px] text-[13px] outline-none data-[highlighted]:bg-accent">
            <Layers3 className="size-6 text-muted-foreground" />
            <span className="flex-1">All hosts</span>
            <span className="font-mono text-[12px] text-muted-foreground">{online}/{list.length}</span>
            {hostId === ALL_HOSTS && <Check className="size-[12px]" />}
          </M.Item>
          <M.Separator className="-mx-2 my-2 h-px bg-border" />
          {list.map((h) => (
            <M.Item key={h.id} onSelect={() => pick(h.id)} className="flex cursor-default items-center gap-[8px] rounded-sm px-3 py-[4px] text-[13px] outline-none data-[highlighted]:bg-accent">
              <Dot online={h.online} />
              <span className="min-w-0 flex-1 truncate">{h.name || h.id}</span>
              <span className="font-mono text-[12px] text-muted-foreground">{h.mode || h.type} · {h.online ? "online" : "offline"}</span>
              {hostId === h.id && <Check className="size-[12px]" />}
            </M.Item>
          ))}
          {list.length === 0 && <div className="px-3 py-3 text-[13px] text-muted-foreground">{hosts.isLoading ? "Loading hosts…" : "No hosts yet."}</div>}
          <M.Separator className="-mx-2 my-2 h-px bg-border" />
          <M.Item onSelect={() => navigate("/hosts")} className="flex cursor-default items-center gap-[8px] rounded-sm px-3 py-[4px] text-[13px] outline-none data-[highlighted]:bg-accent">
            <Server className="size-6 text-muted-foreground" />
            Manage hosts…
          </M.Item>
          <M.Item onSelect={() => navigate("/hosts?add=1")} className="flex cursor-default items-center gap-[8px] rounded-sm px-3 py-[4px] text-[13px] outline-none data-[highlighted]:bg-accent">
            <Plus className="size-6 text-muted-foreground" />
            Add a host
          </M.Item>
        </M.Content>
      </M.Portal>
    </M.Root>
  );
}
