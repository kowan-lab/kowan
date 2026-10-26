import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { Dialog as D } from "radix-ui";
import { useQuery } from "@tanstack/react-query";
import { CornerDownLeft, Search } from "lucide-react";
import { useNavigate } from "react-router";
import { listContainers, listDeployments, listHosts } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";
import { ALL_HOSTS, useHost } from "@/HostContext";
import { Icon } from "@/ui/kit";
import { StatePill } from "@/ui/kit";
import { useTheme } from "@/ui/theme";
import { visibleNav } from "./nav";
import { useContainerFilter, useFeatures } from "../../lib/features";

const ITEM =
  "flex h-14 cursor-default items-center gap-[8px] rounded-md px-[8px] text-[13px] outline-none select-none data-[selected=true]:bg-accent";
const GROUP =
  "[&_[cmdk-group-heading]]:px-[8px] [&_[cmdk-group-heading]]:pt-[8px] [&_[cmdk-group-heading]]:pb-2 [&_[cmdk-group-heading]]:text-[12px] [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:uppercase";

/**
 * ⌘K command palette: jump to any page, host, container or deployment, and run
 * a few global actions. Data loads only while the palette is open.
 */
export default function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { hostId, setHostId } = useHost();
  const { theme, toggle } = useTheme();
  const features = useFeatures();
  const [q, setQ] = useState("");
  const isAdmin = user?.role === "admin";
  const scopedHost = hostId === ALL_HOSTS ? "local" : hostId;

  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: open });
  const containers = useQuery({
    queryKey: ["containers", scopedHost, "palette"],
    queryFn: () => listContainers(scopedHost, true),
    enabled: open,
    staleTime: 10_000,
  });
  const deployments = useQuery({ queryKey: ["deployments"], queryFn: listDeployments, enabled: open });
  const shownContainers = useContainerFilter()(containers.data ?? []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onOpenChange]);

  const go = (to: string) => {
    onOpenChange(false);
    setQ("");
    navigate(to);
  };

  return (
    <D.Root open={open} onOpenChange={onOpenChange}>
      <D.Portal>
        <D.Overlay className="fixed inset-0 z-50 bg-black/50 anim-fade" />
        <D.Content
          aria-describedby={undefined}
          className="fixed top-[14vh] left-1/2 z-50 w-[calc(100vw-2rem)] max-w-xl -translate-x-1/2 overflow-hidden rounded-xl border bg-popover text-popover-foreground shadow-[0_24px_64px_-16px_rgb(0_0_0/0.55)] outline-none anim-pop"
        >
          <D.Title className="sr-only">Command palette</D.Title>
          <Command loop>
            <div className="flex items-center gap-[8px] border-b px-[10px]">
              <Search className="size-6 text-muted-foreground" aria-hidden />
              <Command.Input
                value={q}
                onValueChange={setQ}
                placeholder="Search pages, hosts, containers, deployments…"
                className="h-18 w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
              <kbd className="rounded border px-[4px] font-mono text-[12px] text-muted-foreground">esc</kbd>
            </div>
            <Command.List className="max-h-[56vh] overflow-y-auto p-[4px]">
              <Command.Empty className="px-5 py-12 text-center text-sm text-muted-foreground">No matches for “{q}”.</Command.Empty>

              {shownContainers.length > 0 && (
                <Command.Group heading={`Containers · ${hostId === ALL_HOSTS ? "local" : scopedHost}`} className={GROUP}>
                  {shownContainers.slice(0, q ? 50 : 6).map((c) => {
                    const name = (c.names[0] ?? c.id).replace(/^\//, "");
                    return (
                      <Command.Item key={c.id} value={`container ${name} ${c.image}`} onSelect={() => go(`/containers/${c.id}?host=${c.hostId || scopedHost}`)} className={ITEM}>
                        <Icon name="docker" size="sm" className="text-muted-foreground" />
                        <span className="min-w-0 flex-1 truncate font-medium">{name}</span>
                        <span className="hidden max-w-72 truncate font-mono text-[12px] text-muted-foreground sm:inline">{c.image}</span>
                        <StatePill status={c.state} size="sm" />
                      </Command.Item>
                    );
                  })}
                </Command.Group>
              )}

              {(deployments.data?.length ?? 0) > 0 && (
                <Command.Group heading="Deployments" className={GROUP}>
                  {deployments.data!.slice(0, q ? 50 : 5).map((d) => (
                    <Command.Item key={d.id} value={`deployment ${d.name}`} onSelect={() => go(`/workloads/dep~${d.id}`)} className={ITEM}>
                      <Icon name="stacks-outline-rounded" size="sm" className="text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate font-medium">{d.name}</span>
                      <span className="font-mono text-[12px] text-muted-foreground">{d.type}</span>
                    </Command.Item>
                  ))}
                </Command.Group>
              )}

              {visibleNav(isAdmin, features).map((s) => (
                <Command.Group key={s.label} heading={s.label} className={GROUP}>
                  {s.items.map((i) => (
                    <Command.Item key={i.to} value={`page ${i.label} ${i.keywords ?? ""}`} onSelect={() => go(i.to)} className={ITEM}>
                      <Icon name={i.icon} size="sm" className="text-muted-foreground" />
                      <span className="flex-1">{i.label}</span>
                      <CornerDownLeft className="size-[12px] text-muted-foreground opacity-0 [[data-selected=true]_&]:opacity-100" />
                    </Command.Item>
                  ))}
                </Command.Group>
              ))}

              {(hosts.data?.length ?? 0) > 0 && (
                <Command.Group heading="Switch host" className={GROUP}>
                  <Command.Item value="host all hosts" onSelect={() => { setHostId(ALL_HOSTS); navigate("/"); onOpenChange(false); }} className={ITEM}>
                    <Icon name="stacks-outline-rounded" size="sm" className="text-muted-foreground" />
                    <span className="flex-1">All hosts</span>
                  </Command.Item>
                  {hosts.data!.map((h) => (
                    <Command.Item key={h.id} value={`host ${h.name} ${h.id}`} onSelect={() => { setHostId(h.id); navigate("/"); onOpenChange(false); }} className={ITEM}>
                      <Icon name="dns-outline" size="sm" className="text-muted-foreground" />
                      <span className="flex-1">{h.name || h.id}</span>
                      <StatePill status={h.online ? "online" : "offline"} size="sm" />
                    </Command.Item>
                  ))}
                </Command.Group>
              )}

              <Command.Group heading="Actions" className={GROUP}>
                <Command.Item value="action new deployment deploy stack" onSelect={() => go("/deployments/new")} className={ITEM}>
                  <Icon name="add-rounded" size="sm" className="text-muted-foreground" />
                  <span className="flex-1">New deployment</span>
                </Command.Item>
                <Command.Item value="action theme dark light toggle" onSelect={() => { toggle(); onOpenChange(false); }} className={ITEM}>
                  <Icon name={theme === "dark" ? "light-mode-outline-rounded" : "dark-mode-outline-rounded"} size="sm" className="text-muted-foreground" />
                  <span className="flex-1">Switch to {theme === "dark" ? "light" : "dark"} theme</span>
                </Command.Item>
                <Command.Item value="action log out sign out" onSelect={() => { onOpenChange(false); logout(); }} className={ITEM}>
                  <Icon name="logout-rounded" size="sm" className="text-muted-foreground" />
                  <span className="flex-1">Log out</span>
                </Command.Item>
              </Command.Group>
            </Command.List>
          </Command>
        </D.Content>
      </D.Portal>
    </D.Root>
  );
}
