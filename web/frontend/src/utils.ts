// Small formatting helpers used across pages.

/** Format a byte count into a human-readable string (e.g. 1.5 GB). */
export function formatBytes(bytes: number, decimals = 1): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const k = 1024;
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(k)), units.length - 1);
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(i === 0 ? 0 : decimals)} ${units[i]}`;
}

/** Format a percentage value (0-100) with a single decimal. */
export function formatPercent(value: number, decimals = 1): string {
  if (!Number.isFinite(value)) return "0%";
  return `${value.toFixed(decimals)}%`;
}

/**
 * Format a date input into a locale string.
 * Accepts ISO strings, epoch seconds (number) or Date.
 */
export function formatDate(input: string | number | Date | null | undefined): string {
  if (input === null || input === undefined || input === "") return "-";
  let date: Date;
  if (typeof input === "number") {
    // Docker created timestamps are epoch seconds; JS expects ms.
    date = new Date(input < 1e12 ? input * 1000 : input);
  } else if (input instanceof Date) {
    date = input;
  } else {
    date = new Date(input);
  }
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

/** Relative "time ago" formatting for a date input. */
export function timeAgo(input: string | number | Date | null | undefined): string {
  if (input === null || input === undefined || input === "") return "-";
  let date: Date;
  if (typeof input === "number") {
    date = new Date(input < 1e12 ? input * 1000 : input);
  } else if (input instanceof Date) {
    date = input;
  } else {
    date = new Date(input);
  }
  if (Number.isNaN(date.getTime())) return "-";

  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 0) return formatDate(date);
  const intervals: [number, string][] = [
    [31536000, "year"],
    [2592000, "month"],
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
    [1, "second"],
  ];
  for (const [secs, label] of intervals) {
    const count = Math.floor(seconds / secs);
    if (count >= 1) {
      return `${count} ${label}${count > 1 ? "s" : ""} ago`;
    }
  }
  return "just now";
}

/** Strip the leading slash Docker prepends to container names. */
export function containerName(names: string[] | undefined): string {
  if (!names || names.length === 0) return "-";
  return names[0].replace(/^\//, "");
}

/** Short Docker id (first 12 chars). */
export function shortId(id: string | undefined): string {
  if (!id) return "-";
  return id.replace(/^sha256:/, "").slice(0, 12);
}

/** Pick a badge colour for an alert level. */
export function levelColor(level: string | undefined): string {
  switch ((level || "").toLowerCase()) {
    case "critical":
      return "red";
    case "warning":
      return "yellow";
    case "info":
      return "blue";
    default:
      return "gray";
  }
}

/**
 * Extract a human-readable message from a thrown API error.
 *
 * Duck-typed superset (no axios import needed): prefers the server-provided
 * `response.data.error`, then `response.data.message`, then the Error's own
 * `message`, then the caller's fallback. Covers every previous per-page
 * `errorMessage`/`errMessage`/`apiError` variant.
 */
export function errorMessage(err: unknown, fallback: string): string {
  const data = (err as { response?: { data?: { error?: string; message?: string } } })
    ?.response?.data;
  return (
    data?.error ??
    data?.message ??
    (err as { message?: string })?.message ??
    fallback
  );
}

/**
 * A container's ports in a fixed order: container port, protocol, host port,
 * then IPv4 before IPv6. Docker lists them from a Go map, in a different order
 * on every call; the server sorts them the same way.
 */
export function sortPorts<T extends { privatePort: number; publicPort: number; type: string; ip: string }>(ports: T[] | null | undefined): T[] {
  // Plain code-unit order, as the server's sort: "0.0.0.0" before "::".
  const cmp = (x = "", y = "") => (x < y ? -1 : x > y ? 1 : 0);
  return [...(ports ?? [])].sort(
    (a, b) => a.privatePort - b.privatePort || cmp(a.type, b.type) || a.publicPort - b.publicPort || cmp(a.ip, b.ip),
  );
}
