import type { CSSProperties, ReactNode, SVGProps } from "react";
import {
  Archive,
  FileClock,
  Mail,
  ShieldCheck,
  UserRound,
  ArrowLeft,
  ArrowUp,
  Bell,
  BellOff,
  Boxes,
  Braces,
  Cable,
  ChartColumn,
  ChartLine,
  Check,
  CheckCheck,
  ChevronDown,
  Circle,
  CircleAlert,
  CircleArrowUp,
  CircleCheck,
  CircleHelp,
  CirclePause,
  CirclePlay,
  CircleStop,
  CircleX,
  Copy,
  Database,
  DatabaseZap,
  Download,
  Eraser,
  ExternalLink,
  Eye,
  EyeOff,
  FileText,
  Folder,
  FolderArchive,
  FolderOpen,
  Globe,
  Activity,
  Hammer,
  HardDrive,
  HeartPulse,
  History,
  Sparkles,
  Info,
  Key,
  KeyRound,
  Layers,
  LayoutDashboard,
  Link,
  Lock,
  LogOut,
  MemoryStick,
  Menu,
  Monitor,
  Moon,
  Network,
  Package,
  PackageOpen,
  Pause,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  Rocket,
  Route,
  ScrollText,
  Search,
  Send,
  Server,
  Settings,
  ShieldAlert,
  Square,
  SquareTerminal,
  Sun,
  Tag,
  Trash2,
  TriangleAlert,
  Upload,
  Users,
  Waypoints,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toneVars, type Color } from "./tone";

/** Docker whale (Simple Icons, CC0). Used nominatively for "Docker containers". */
function DockerGlyph(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" {...props}>
      <path d="M13.983 11.078h2.119a.186.186 0 0 0 .186-.185V9.006a.186.186 0 0 0-.186-.186h-2.119a.185.185 0 0 0-.185.185v1.888c0 .102.083.185.185.185m-2.954-5.43h2.118a.186.186 0 0 0 .186-.186V3.574a.186.186 0 0 0-.186-.185h-2.118a.185.185 0 0 0-.185.185v1.888c0 .102.082.185.185.185m0 2.716h2.118a.187.187 0 0 0 .186-.186V6.29a.186.186 0 0 0-.186-.185h-2.118a.185.185 0 0 0-.185.185v1.887c0 .102.082.185.185.186m-2.93 0h2.12a.186.186 0 0 0 .184-.186V6.29a.185.185 0 0 0-.185-.185H8.1a.185.185 0 0 0-.185.185v1.887c0 .102.083.185.185.186m-2.964 0h2.119a.186.186 0 0 0 .185-.186V6.29a.185.185 0 0 0-.185-.185H5.136a.186.186 0 0 0-.186.185v1.887c0 .102.084.185.186.186m5.893 2.715h2.118a.186.186 0 0 0 .186-.185V9.006a.186.186 0 0 0-.186-.186h-2.118a.185.185 0 0 0-.185.185v1.888c0 .102.082.185.185.185m-2.93 0h2.12a.185.185 0 0 0 .184-.185V9.006a.185.185 0 0 0-.184-.186h-2.12a.185.185 0 0 0-.184.185v1.888c0 .102.083.185.185.185m-2.964 0h2.119a.185.185 0 0 0 .185-.185V9.006a.185.185 0 0 0-.184-.186h-2.12a.186.186 0 0 0-.186.186v1.887c0 .102.084.185.186.185m-2.92 0h2.12a.185.185 0 0 0 .184-.185V9.006a.185.185 0 0 0-.184-.186h-2.12a.185.185 0 0 0-.184.185v1.888c0 .102.082.185.185.185M23.763 9.89c-.065-.051-.672-.51-1.954-.51-.338.001-.676.03-1.01.087-.248-1.7-1.653-2.53-1.716-2.566l-.344-.199-.226.327c-.284.438-.49.922-.612 1.43-.23.97-.09 1.882.403 2.661-.595.332-1.55.413-1.744.42H.751a.751.751 0 0 0-.75.748 11.376 11.376 0 0 0 .692 4.062c.545 1.428 1.355 2.48 2.41 3.124 1.18.723 3.1 1.137 5.275 1.137.983.003 1.963-.086 2.93-.266a12.248 12.248 0 0 0 3.823-1.389c.98-.567 1.86-1.288 2.61-2.136 1.252-1.418 1.998-2.997 2.553-4.4h.221c1.372 0 2.215-.549 2.68-1.009.309-.293.55-.65.707-1.046l.098-.288Z" />
    </svg>
  );
}

const MAP: Record<string, LucideIcon | typeof DockerGlyph> = {
  "account-tree-outline-rounded": Workflow,
  "add-rounded": Plus,
  "api-rounded": KeyRound,
  "archive-outline-rounded": Archive,
  "arrow-back-rounded": ArrowLeft,
  "arrow-drop-down-rounded": ChevronDown,
  "arrow-upward-rounded": ArrowUp,
  "bar-chart-rounded": ChartColumn,
  "build-outline-rounded": Hammer,
  "cable-rounded": Cable,
  "cancel-outline-rounded": CircleX,
  "check-circle-outline-rounded": CircleCheck,
  "check-rounded": Check,
  "close-rounded": X,
  "content-copy-outline-rounded": Copy,
  "dark-mode-outline-rounded": Moon,
  "dashboard-outline-rounded": LayoutDashboard,
  "data-object-rounded": Braces,
  "database-outline": Database,
  "database-search-outline-rounded": DatabaseZap,
  "delete-outline-rounded": Trash2,
  "deployed-code-outline": Package,
  "description-outline-rounded": ScrollText,
  "desktop-windows-outline-rounded": Monitor,
  "dns-outline": Server,
  "dns-outline-rounded": Server,
  "done-all-rounded": CheckCheck,
  "download-rounded": Download,
  "draft-outline-rounded": FileText,
  "edit-outline-rounded": Pencil,
  "error-outline-rounded": CircleAlert,
  "folder-open-outline-rounded": FolderOpen,
  "folder-outline-rounded": Folder,
  "folder-zip-outline-rounded": FolderArchive,
  "gpp-maybe-outline-rounded": ShieldAlert,
  "group-outline-rounded": Users,
  "person-outline-rounded": UserRound,
  "verified-user-outline-rounded": ShieldCheck,
  "audit-outline-rounded": FileClock,
  "help-outline-rounded": CircleHelp,
  "history-rounded": History,
  "new-releases-outline-rounded": Sparkles,
  "uptime-rounded": Activity,
  "http-rounded": Globe,
  "hub-outline-rounded": Waypoints,
  "image-outline-rounded": Layers,
  "info-outline-rounded": Info,
  "key-outline-rounded": Key,
  "lan-outline-rounded": Network,
  "layers-outline-rounded": Layers,
  "light-mode-outline-rounded": Sun,
  "link-rounded": Link,
  "lock-outline": Lock,
  "logout-rounded": LogOut,
  "memory-outline-rounded": MemoryStick,
  "menu-rounded": Menu,
  "monitor-heart-outline-rounded": HeartPulse,
  "mop-outline-rounded": Eraser,
  "notifications-off-outline-rounded": BellOff,
  "notifications-outline-rounded": Bell,
  "mail-outline-rounded": Mail,
  "open-in-new-rounded": ExternalLink,
  "package-2-outline": PackageOpen,
  "pause-circle-outline": CirclePause,
  "pause-rounded": Pause,
  "play-arrow-rounded": Play,
  "play-circle-outline-rounded": CirclePlay,
  "power-outline-rounded": Power,
  "refresh-rounded": RefreshCw,
  "rocket-launch-outline": Rocket,
  "rocket-launch-outline-rounded": Rocket,
  "route-outline": Route,
  "search-rounded": Search,
  "sell-outline": Tag,
  "send-outline-rounded": Send,
  "settings-outline-rounded": Settings,
  "show-chart-rounded": ChartLine,
  "space-dashboard-outline-rounded": LayoutDashboard,
  "stacks-outline-rounded": Boxes,
  "stop-outline-rounded": CircleStop,
  "stop-rounded": Square,
  "storage-rounded": HardDrive,
  "terminal-rounded": SquareTerminal,
  "upgrade-rounded": CircleArrowUp,
  "upload-rounded": Upload,
  "visibility-off-outline-rounded": EyeOff,
  "visibility-outline-rounded": Eye,
  "warning-outline-rounded": TriangleAlert,
  docker: DockerGlyph,
};

export type IconSize = "xs" | "sm" | "md" | "lg" | "xl";
const PX: Record<IconSize, number> = { xs: 11, sm: 12, md: 14, lg: 17, xl: 22 };

export interface IconProps {
  /** Icon key from MAP (e.g. "add-rounded", "docker"). */
  name: string;
  size?: IconSize;
  color?: Color;
  label?: string;
  className?: string;
  style?: CSSProperties;
}

export function Icon({ name, size = "md", color, label, className, style }: IconProps) {
  const Cmp = MAP[name] ?? Circle;
  const px = PX[size];
  return (
    <Cmp
      width={px}
      height={px}
      strokeWidth={size === "xs" || size === "sm" ? 2.25 : 2}
      aria-hidden={label ? undefined : true}
      aria-label={label}
      role={label ? "img" : undefined}
      className={cn("shrink-0", color && "text-[var(--t-text)]", className)}
      style={{ ...(color ? toneVars(color) : {}), ...style }}
    />
  );
}

/** Render an icon prop that may be an icon key or a ready node. */
export function renderIcon(icon: string | ReactNode | undefined, size: IconSize = "sm"): ReactNode {
  if (icon == null || icon === false) return null;
  if (typeof icon === "string") return <Icon name={icon} size={size} />;
  return icon;
}
