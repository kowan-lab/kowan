import { Suspense, useEffect, useState } from "react";
import { DropdownMenu as M } from "radix-ui";
import { ArrowLeft, ChevronDown, LogOut, Search } from "lucide-react";
import { Link, Outlet, useLocation } from "react-router";
import { useAuth } from "../auth/AuthContext";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
  useSidebar,
} from "./ui/sidebar";
import { Icon, Skeleton } from "@/ui/kit";
import { PageSlotContext } from "../ui/pageSlot";
import NotificationBell from "./NotificationBell";
import PageErrorBoundary from "./PageErrorBoundary";
import CommandPalette from "./shell/CommandPalette";
import HostSwitcher from "./shell/HostSwitcher";
import { ThemePanel } from "./shell/ThemePicker";
import { isActive, visibleNav, type NavItem } from "./shell/nav";
import { useFeatures } from "../lib/features";
import { cn } from "@/lib/utils";
import { useDockerEvents } from "./shell/useDockerEvents";
import { KowanMark } from "./KowanMark";
import OfflineNotice from "./shell/OfflineNotice";
import { safeLocalPath } from "../lib/safePath";

/** The wordmark: the hanko seal and the name. */
function Brand() {
  const { state } = useSidebar();
  return (
    <Link to="/" className="flex h-12 items-center gap-3 rounded-md px-2 outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal" aria-label="Kowan home">
      <KowanMark className="size-8 shrink-0" />
      {state === "expanded" && <span className="text-[15px] font-bold tracking-[-0.02em] uppercase">kowan</span>}
    </Link>
  );
}

/** Top-bar account menu: who you are, theme and palette, log out. */
function UserMenu() {
  const { user, logout } = useAuth();
  const initials = (user?.username ?? "?").slice(0, 2).toUpperCase();
  return (
    <M.Root modal={false}>
      <M.Trigger
        aria-label="Account and theme"
        className="flex h-18 items-center gap-4 pr-2 pl-[2px] text-left outline-none hover:bg-muted data-[state=open]:bg-muted"
      >
        <span className="flex size-16 shrink-0 items-center justify-center bg-foreground font-mono text-[12px] font-bold text-background">{initials}</span>
        <span className="hidden flex-col sm:flex">
          <span className="text-[13px] leading-[16px] font-medium">{user?.username ?? "Account"}</span>
          <span className="font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">{user?.role ?? ""}</span>
        </span>
        <ChevronDown aria-hidden className="size-7 text-muted-foreground" />
      </M.Trigger>
      <M.Portal>
        <M.Content
          align="end"
          sideOffset={6}
          className="anim-pop z-50 w-[320px] bg-popover p-4 text-popover-foreground shadow-[inset_0_0_0_1px_var(--border),0_8px_24px_-6px_rgb(40_40_36/0.22)]"
        >
          <M.Label className="px-3 pt-2 pb-4 text-[13px] text-muted-foreground">
            Signed in as <span className="font-medium text-foreground">{user?.username}</span>
          </M.Label>
          <div className="px-3 pb-4">
            <p className="m-0 pb-3 font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">Theme</p>
            <ThemePanel />
          </div>
          <M.Separator className="-mx-4 my-2 h-px bg-border" />
          <M.Item onSelect={logout} className="flex h-16 cursor-default items-center gap-4 px-3 text-[13px] text-[var(--tone-red-text)] outline-none data-[highlighted]:bg-accent">
            <LogOut className="size-8" strokeWidth={1.5} /> Log out
          </M.Item>
        </M.Content>
      </M.Portal>
    </M.Root>
  );
}

/** One sidebar row in the panel-row style: square marker, mono uppercase label. */
function NavRow({ item, active, sub, onNavigate }: { item: NavItem; active: boolean; sub?: boolean; onNavigate: () => void }) {
  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        asChild
        isActive={active}
        tooltip={item.label}
        className={cn(
          "gap-5 rounded-none px-4 text-sidebar-foreground hover:bg-accent data-[active=true]:bg-[var(--popover)] data-[active=true]:text-foreground data-[active=true]:shadow-[inset_0_0_0_1px_var(--border)]",
          // Collapsed rail: a centred 40x40 icon button, no label.
          "group-data-[collapsible=icon]:!size-20 group-data-[collapsible=icon]:!p-0 group-data-[collapsible=icon]:justify-center",
          sub
            ? "h-15 pl-16 text-[13px] font-medium data-[active=true]:font-semibold"
            : "h-17 font-mono text-[13px] font-semibold tracking-[0.04em] uppercase data-[active=true]:font-semibold",
        )}
      >
        <Link to={item.to} onClick={onNavigate} onPointerEnter={() => preloadRoute(item.to)} onFocus={() => preloadRoute(item.to)} aria-current={active ? "page" : undefined}>
          <Icon name={item.icon} size="md" className={cn("shrink-0", active ? "text-foreground" : "text-muted-foreground")} />
          <span className="truncate group-data-[collapsible=icon]:hidden">{item.label}</span>
        </Link>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

const LAST_PAGE_KEY = "kowan-last-page";
const SETTINGS_ROW: NavItem = { label: "Settings", to: "/settings", icon: "settings-outline-rounded" };

function AppSidebar() {
  const { user } = useAuth();
  const { pathname } = useLocation();
  const { isMobile, setOpenMobile } = useSidebar();
  const features = useFeatures();
  const sections = visibleNav(user?.role === "admin", features);
  // Only the most specific matching item is active (/networks/map must not
  // also light up /networks).
  const activeTo = sections
    .flatMap((sec) => sec.items.map((i) => i.to))
    .filter((to) => isActive(to, pathname))
    .sort((a, b) => b.length - a.length)[0];
  const onNavigate = () => isMobile && setOpenMobile(false);
  const main = sections.filter((s) => !s.collapsible);
  const settings = sections.find((s) => s.collapsible);
  const inSettings = !!settings?.items.some((i) => i.to === activeTo);
  // Remember the last page outside Settings, so "Back" returns there.
  useEffect(() => {
    if (!inSettings) {
      try {
        sessionStorage.setItem(LAST_PAGE_KEY, pathname + window.location.search);
      } catch {
        /* private mode */
      }
    }
  }, [inSettings, pathname]);
  // Keep the current page's row in view.
  useEffect(() => {
    const row = document.querySelector<HTMLElement>('[data-sidebar="content"] [aria-current="page"]');
    row?.scrollIntoView({ block: "nearest" });
  }, [activeTo, inSettings]);
  const back = (() => {
    try {
      return safeLocalPath(sessionStorage.getItem(LAST_PAGE_KEY));
    } catch {
      return "/";
    }
  })();

  return (
    <Sidebar collapsible="icon" className="border-r border-sidebar-border">
      <SidebarHeader className="h-28 justify-center px-7 py-0 shadow-[inset_0_-1px_0_var(--sidebar-border)] group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-0">
        <Brand />
      </SidebarHeader>
      {inSettings && settings ? (
        <>
          <div className="shadow-[inset_0_-1px_0_var(--sidebar-border)] px-4 py-4">
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton asChild tooltip="Back" className="h-17 gap-5 rounded-none px-4 text-[13px] font-medium text-sidebar-foreground hover:bg-accent group-data-[collapsible=icon]:!size-20 group-data-[collapsible=icon]:!p-0 group-data-[collapsible=icon]:justify-center">
                  <Link to={back} onClick={onNavigate}>
                    <ArrowLeft className="size-8 shrink-0" strokeWidth={1.5} />
                    <span className="truncate group-data-[collapsible=icon]:hidden">Back</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
            <p className="m-0 px-4 pt-4 text-[18px] leading-[24px] font-extrabold tracking-[-0.01em] uppercase group-data-[collapsible=icon]:hidden">Settings</p>
          </div>
          <SidebarContent className="gap-0 px-4 pt-0 pb-4 group-data-[collapsible=icon]:px-4">
            {groupBySub(settings.items).map(([sub, items]) => (
              <SidebarGroup key={sub} className="px-0 pt-6 pb-2">
                {!(items.length === 1 && items[0].label === sub) && (
                  <SidebarGroupLabel className="h-auto px-4 pb-3 font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">{sub}</SidebarGroupLabel>
                )}
                <SidebarMenu className="gap-0">
                  {items.map((i) => (
                    <NavRow key={i.to} item={i} active={i.to === activeTo} onNavigate={onNavigate} />
                  ))}
                </SidebarMenu>
              </SidebarGroup>
            ))}
          </SidebarContent>
        </>
      ) : (
        <>
          <HostSwitcher />
          <SidebarContent className="gap-0 px-4 pt-0 pb-4 group-data-[collapsible=icon]:px-4">
            {main.map((s) => (
              <SidebarGroup key={s.label} className="px-0 pt-6 pb-2">
                <SidebarGroupLabel className="h-auto px-4 pb-3 font-mono text-[12px] leading-[16px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">{s.label}</SidebarGroupLabel>
                <SidebarMenu className="gap-0">
                  {s.items.map((i) => (
                    <NavRow key={i.to} item={i} active={i.to === activeTo} onNavigate={onNavigate} />
                  ))}
                </SidebarMenu>
              </SidebarGroup>
            ))}
          </SidebarContent>
          {/* Settings opens its own area of the sidebar (with a Back row). */}
          <SidebarFooter className="px-4 py-4 shadow-[inset_0_1px_0_var(--sidebar-border)] group-data-[collapsible=icon]:px-4">
            <SidebarMenu className="gap-0">
              <NavRow item={SETTINGS_ROW} active={false} onNavigate={onNavigate} />
            </SidebarMenu>
          </SidebarFooter>
        </>
      )}
    </Sidebar>
  );
}

/** Split Settings items into their sub-groups, keeping the order. */
function groupBySub(items: NavItem[]): [string, NavItem[]][] {
  const out: [string, NavItem[]][] = [];
  for (const i of items) {
    const key = i.sub ?? "General";
    const last = out[out.length - 1];
    if (last && last[0] === key) last[1].push(i);
    else out.push([key, [i]]);
  }
  return out;
}


/**
 * Intent preloading: a page's code is fetched when its link is hovered or
 * focused, ~100-300 ms before the click, so navigation stays instant without
 * downloading pages that are never visited.
 */
const PAGE_LOADERS = import.meta.glob("../pages/*.tsx");
const ROUTE_PAGE: Record<string, string> = {
  "/": "Dashboard", "/workloads": "Workloads", "/hosts": "Hosts",
  "/templates": "Templates", "/images": "Images", "/volumes": "Volumes", "/networks": "Networks",
  "/workflows": "Workflows", "/gitops": "GitOps", "/backups": "Backups", "/schedules": "Schedules", "/activity": "Activity", "/monitoring": "Monitoring", "/logs": "Logs",
  "/audit": "Audit", "/registries": "Registries", "/credentials": "Connections", "/secrets": "Secrets",
  "/channels": "Notifications", "/users": "Users", "/api-keys": "ApiKeys", "/settings": "Settings", "/ai": "AiAssistants",
  "/networks/map": "NetworkMap", "/exposure": "Exposure",
  "/auto-update": "AutoUpdate",
  "/teams": "Teams", "/sso": "SsoProviders",
};
const warmed = new Set<string>();
function preloadRoute(to: string) {
  const page = ROUTE_PAGE[to];
  if (!page || warmed.has(page)) return;
  warmed.add(page);
  void PAGE_LOADERS[`../pages/${page}.tsx`]?.();
}

/** Placeholder shown inside the content area while a page chunk loads. */
function ContentFallback() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading page">
      <Skeleton width={180} height={20} />
      <Skeleton width="40%" height={12} />
      <Skeleton height={220} />
    </div>
  );
}

export default function Layout() {
  useDockerEvents();
  const { pathname } = useLocation();
  // DOM node of the top bar's breadcrumb slot; PageHeader portals into it.
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  const [palette, setPalette] = useState(false);
  const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <PageSlotContext.Provider value={slot}>
      <SidebarProvider className="h-svh overflow-hidden">
        <AppSidebar />
        <SidebarInset className="app-canvas min-w-0 overflow-hidden">
          <header className="flex h-28 shrink-0 items-center gap-6 bg-sidebar px-8 shadow-[inset_0_-1px_0_var(--sidebar-border)]">
            <SidebarTrigger className="size-18 rounded-none bg-[var(--popover)] text-foreground shadow-[inset_0_0_0_1px_var(--border)] hover:bg-muted" />
            <span aria-hidden className="h-6 w-px bg-border" />
            <div ref={setSlot} className="hidden" />
            <button
              type="button"
              onClick={() => setPalette(true)}
              className="hidden h-18 max-w-[440px] flex-1 items-center gap-4 whitespace-nowrap bg-[var(--popover)] px-5 text-[14px] text-muted-foreground shadow-[inset_0_0_0_1px_var(--border)] outline-none transition-colors hover:text-foreground hover:shadow-[inset_0_0_0_1px_var(--input)] focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal md:flex"
            >
              <Search className="size-[12px]" />
              <span className="flex-1 text-left">Jump to workload, container, page or setting</span>
              <kbd className="rounded border px-2 font-mono text-[12px]">{isMac ? "⌘" : "Ctrl"} K</kbd>
            </button>
            <button
              type="button"
              aria-label="Search"
              onClick={() => setPalette(true)}
              className="flex size-12 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground md:hidden"
            >
              <Search className="size-6" />
            </button>
            <div className="ml-auto flex items-center gap-4">
              <NotificationBell placement="bottom-end" />
              <UserMenu />
            </div>
          </header>
          <OfflineNotice />
          <main className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto bg-transparent [--page-px:16px]">
            <Suspense fallback={<ContentFallback />}>
              {/* Keyed by path: only the page content re-enters, with a fast fade. */}
              <div key={pathname} className="page-content">
                <PageErrorBoundary>
                  <Outlet />
                </PageErrorBoundary>
              </div>
            </Suspense>
          </main>
        </SidebarInset>
      </SidebarProvider>
      <CommandPalette open={palette} onOpenChange={setPalette} />
    </PageSlotContext.Provider>
  );
}
