import { Fragment, type ReactNode } from "react";
import { Link, useLocation } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { listHosts } from "@/api/client";
import { ALL_HOSTS, useHost } from "@/HostContext";
import { NAV } from "./shell/nav";
import { cn } from "@/lib/utils";

export interface Breadcrumb {
  label: string;
  to?: string;
}

export interface PageHeaderProps {
  /** A string renders as the page heading; a node renders as-is (detail pages
   *  put state pills next to the name). */
  title: ReactNode;
  description?: ReactNode;
  /** Accepted for compatibility; the header no longer draws an icon chip. */
  icon?: ReactNode;
  actions?: ReactNode;
  breadcrumbs?: Breadcrumb[];
}

/**
 * Page header. The breadcrumb trail goes to the shell's top bar; the title,
 * description and actions render as a solid full-bleed band at the top of the
 * page, so the background grid shows only in the gutters below it.
 */
export default function PageHeader({ title, description, actions, breadcrumbs }: PageHeaderProps) {
  const page: Breadcrumb[] =
    breadcrumbs && breadcrumbs.length > 0 ? breadcrumbs : typeof title === "string" ? [{ label: title }] : [];
  // The trail always starts with the scope, as in the console mock: the host
  // scope on scoped pages, "Server-wide" on settings pages.
  const { pathname } = useLocation();
  const { hostId } = useHost();
  const settingsPaths = NAV.filter((s) => s.collapsible).flatMap((s) => s.items.map((i) => i.to));
  const serverWide = settingsPaths.some((to) => pathname === to || pathname.startsWith(`${to}/`));
  // Only for the host's display name. Pages render this header in their error
  // states too, so it must not refetch a failed host list on mount: that reset
  // the shared query to "loading", unmounted the header, failed again and
  // looped (thousands of GET /hosts a second when the list endpoint errors).
  const hosts = useQuery({
    queryKey: ["hosts"],
    queryFn: listHosts,
    staleTime: 15_000,
    enabled: !serverWide && hostId !== ALL_HOSTS,
    retryOnMount: false,
  });
  const scope = serverWide
    ? "Server-wide"
    : hostId === ALL_HOSTS
      ? "All hosts"
      : hosts.data?.find((h) => h.id === hostId)?.name || hostId;
  const trail: Breadcrumb[] = [{ label: scope }, ...page];

  const crumbs = trail.length > 1 && (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="m-0 flex list-none flex-wrap items-center gap-4 p-0 font-mono text-[12px] leading-[20px] font-semibold tracking-[0.06em] text-muted-foreground uppercase">
        {trail.map((c, i) => {
          const last = i === trail.length - 1;
          return (
            <Fragment key={`${c.label}-${i}`}>
              {i > 0 && <li aria-hidden className="text-[var(--input)]">/</li>}
              <li className="min-w-0 truncate">
                {c.to && !last ? (
                  <Link to={c.to} className="text-muted-foreground no-underline hover:text-foreground hover:underline">{c.label}</Link>
                ) : (
                  <span className={last ? "text-foreground" : undefined} aria-current={last ? "page" : undefined}>
                    {c.label}
                  </span>
                )}
              </li>
            </Fragment>
          );
        })}
      </ol>
    </nav>
  );

  // Section pages get the panel-label treatment (bold uppercase); detail pages
  // (with breadcrumbs) show an object's real name as typed.
  const detail = Boolean(breadcrumbs && breadcrumbs.length > 0);
  return (
    <header className="bg-card px-8 pt-7 pb-8 shadow-[inset_0_0_0_1px_var(--border)]">
      {crumbs}
      <div className={cn("flex flex-wrap items-end gap-x-8 gap-y-4", crumbs && "mt-3")}>
        {typeof title === "string" ? (
          <h1
            className={cn(
              "m-0 text-[24px] leading-[32px] tracking-[-0.02em] text-foreground",
              detail ? "font-[650]" : "font-extrabold uppercase",
            )}
          >
            {title}
          </h1>
        ) : (
          <div className="flex min-w-0 flex-wrap items-center gap-4 text-[24px] leading-[32px] font-[650] tracking-[-0.02em]">{title}</div>
        )}
        {description && <p className="m-0 max-w-[72ch] pb-[5px] text-[14px] leading-[20px] text-muted-foreground">{description}</p>}
        {actions && <div className="ml-auto flex flex-wrap items-center justify-end gap-4 pb-[2px]">{actions}</div>}
      </div>
    </header>
  );
}
