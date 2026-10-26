/** Top-level service names of a compose file (block style, 2+ space indent). */
export function composeServices(yaml: string): string[] {
  const out: string[] = [];
  let inServices = false;
  let indent = -1;
  for (const raw of yaml.split("\n")) {
    const line = raw.replace(/\s+#.*$/, "");
    if (!line.trim()) continue;
    const lead = line.length - line.trimStart().length;
    if (lead === 0) {
      inServices = /^services:\s*$/.test(line);
      indent = -1;
      continue;
    }
    if (!inServices) continue;
    if (indent < 0) indent = lead;
    const m = lead === indent ? /^\s*["']?([A-Za-z0-9_.-]+)["']?:\s*$/.exec(line) : null;
    if (m) out.push(m[1]);
  }
  return out;
}
