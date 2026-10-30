import type { FeatureId, Features } from "../../lib/features";

/**
 * Navigation model shared by the sidebar and the command palette.
 *
 * Every destination is visible in the sidebar (the palette is only a shortcut):
 *   Infrastructure  what runs and what it uses: workloads (stacks, deployments,
 *               lone containers, with a flat container view), images, volumes, networks
 *   Automation  what runs on its own: update, backup, schedule and exposure policies, workflows
 *   Activity    what happened: events, alerts, logs, audit
 *   Settings    the rare things: a separate area opened from the account menu;
 *               there the sidebar shows only these, with a Back button
 */
export interface NavItem {
  label: string;
  to: string;
  icon: string;
  adminOnly?: boolean;
  /** Extra words the command palette matches on. */
  keywords?: string;
  /** Sub-heading inside a group (Settings uses Connections / Access / Server). */
  sub?: string;
  /** A feature that can be turned off in Settings; hidden while it is off. */
  feature?: FeatureId;
}
export interface NavSection {
  label: string;
  items: NavItem[];
  /** Rendered as a collapsible group pinned to the bottom of the sidebar. */
  collapsible?: boolean;
}

export const NAV: NavSection[] = [
  {
    label: "Infrastructure",
    items: [
      { label: "Overview", to: "/", icon: "dashboard-outline-rounded", keywords: "home dashboard needs attention" },
      { label: "Workloads", to: "/workloads", icon: "stacks-outline-rounded", keywords: "apps stacks compose deployments services containers ps running" },
      { label: "Templates", to: "/templates", icon: "package-2-outline", feature: "templates", keywords: "catalog new workload app" },
      { label: "Images", to: "/images", icon: "layers-outline-rounded", keywords: "pull build tags" },
      { label: "Volumes", to: "/volumes", icon: "database-outline", keywords: "storage files" },
      { label: "Networks", to: "/networks", icon: "lan-outline-rounded", keywords: "bridge overlay" },
      { label: "Network map", to: "/networks/map", icon: "route-outline", feature: "networkMap", keywords: "graph topology dependencies depends_on" },
    ],
  },
  {
    label: "Automation",
    items: [
      { label: "Auto-update", to: "/auto-update", icon: "upgrade-rounded", keywords: "updates watchtower rollback scan vulnerabilities policies" },
      { label: "Backups", to: "/backups", icon: "archive-outline-rounded", keywords: "restic volumes snapshots restore s3 sftp" },
      { label: "Schedules", to: "/schedules", icon: "power-outline-rounded", keywords: "cron start stop restart timer nightly" },
      { label: "Exposure", to: "/exposure", icon: "http-rounded", keywords: "ports public https domains traefik certificates expose" },
      { label: "Workflows", to: "/workflows", icon: "account-tree-outline-rounded", keywords: "triggers cron webhooks" },
      { label: "GitOps", to: "/gitops", icon: "data-object-rounded", adminOnly: true, feature: "gitops", keywords: "git sync config as code export resources yaml" },
    ],
  },
  {
    label: "Activity",
    items: [
      { label: "Timeline", to: "/activity", icon: "history-rounded", keywords: "events docker stream activity" },
      { label: "Alerts", to: "/monitoring", icon: "monitor-heart-outline-rounded", keywords: "alerts rules metrics monitoring" },
      { label: "Uptime", to: "/uptime", icon: "uptime-rounded", keywords: "uptime http tcp ping status page response time certificates" },
      { label: "Logs", to: "/logs", icon: "description-outline-rounded", keywords: "tail output" },
      { label: "Audit log", to: "/audit", icon: "audit-outline-rounded", adminOnly: true, keywords: "activity events who" },
    ],
  },
  {
    label: "Settings",
    collapsible: true,
    items: [
      { label: "General", to: "/settings", icon: "settings-outline-rounded", sub: "General", keywords: "preferences server backup update theme profile" },
      { label: "Registries", to: "/registries", icon: "deployed-code-outline", adminOnly: true, sub: "Connections", keywords: "docker hub ghcr" },
      { label: "Credentials", to: "/credentials", icon: "key-outline-rounded", adminOnly: true, sub: "Connections", keywords: "connections tokens" },
      { label: "Secrets", to: "/secrets", icon: "lock-outline", adminOnly: true, sub: "Connections", keywords: "vault env" },
      { label: "Notifications", to: "/channels", icon: "notifications-outline-rounded", adminOnly: true, sub: "Connections", keywords: "slack discord email webhook channels" },
      { label: "Users", to: "/users", icon: "person-outline-rounded", adminOnly: true, sub: "Access", keywords: "roles accounts" },
      { label: "Teams", to: "/teams", icon: "group-outline-rounded", adminOnly: true, sub: "Access", keywords: "permissions grants scopes rbac access" },
      { label: "API keys", to: "/api-keys", icon: "api-rounded", adminOnly: true, sub: "Access", keywords: "tokens cli" },
      { label: "Single sign-on", to: "/sso", icon: "verified-user-outline-rounded", adminOnly: true, sub: "Access", keywords: "sso oidc openid keycloak authentik authelia entra google github login" },
      { label: "AI assistants", to: "/ai", icon: "hub-outline-rounded", sub: "Access", keywords: "mcp claude cursor llm agent" },
      { label: "Hosts & agents", to: "/hosts", icon: "dns-outline", sub: "Server", keywords: "environments agents servers nodes join" },
      { label: "Network access", to: "/settings/network", icon: "lan-outline-rounded", adminOnly: true, sub: "Server", keywords: "cidr allowlist ip firewall proxy security" },
      { label: "What's new", to: "/changelog", icon: "new-releases-outline-rounded", sub: "Server", keywords: "changelog release notes version updates history" },
    ],
  },
];

/** Pages that belong to a nav item without living under its path. */
const ALIASES: Record<string, string[]> = { "/workloads": ["/containers"] };

/** Items that own only their exact path: their sub-paths are other items. */
const EXACT = new Set(["/", "/settings"]);

export function isActive(to: string, pathname: string): boolean {
  if (EXACT.has(to)) return pathname === to;
  return [to, ...(ALIASES[to] ?? [])].some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function visibleNav(isAdmin: boolean, features?: Features): NavSection[] {
  const shown = (i: NavItem) => (!i.adminOnly || isAdmin) && (!i.feature || !features || features[i.feature]);
  return NAV.map((s) => ({ ...s, items: s.items.filter(shown) })).filter((s) => s.items.length > 0);
}
