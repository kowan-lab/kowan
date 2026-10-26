// Fails the build when an API call targets a UI route instead of a REST path.
// UI routes (/workloads, /apps) do not exist under /api/v1; a rename that
// rewrites links must never touch the API client.
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const UI_ONLY = ["/workloads", "/apps"];
const CALL = /\b(?:api|http|client)\.(?:get|post|put|patch|delete)\s*(?:<[^>]*>)?\(\s*[`'"]([^`'"]+)/g;

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* walk(p);
    else if (/\.(ts|tsx)$/.test(name)) yield p;
  }
}

const bad = [];
for (const file of walk("src")) {
  const text = readFileSync(file, "utf8");
  for (const m of text.matchAll(CALL)) {
    if (UI_ONLY.some((r) => m[1] === r || m[1].startsWith(r + "/") || m[1].startsWith(r + "?"))) {
      const line = text.slice(0, m.index).split("\n").length;
      bad.push(`${file}:${line}  ${m[1]}`);
    }
  }
}
if (bad.length) {
  console.error("API calls pointing at UI-only routes:\n  " + bad.join("\n  "));
  process.exit(1);
}
console.log("check-api-paths: ok");
