/**
 * A path inside this app, or "/" when the value could lead elsewhere.
 * `//evil.com` and `/\evil.com` are read by browsers as another host, so a
 * redirect target taken from the URL or storage must be a plain local path.
 */
export function safeLocalPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\\]/.test(value)) return "/";
  return value;
}
