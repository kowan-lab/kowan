// Pure helpers: workload grouping, ids and status text; log filters and
// highlighting; formatting; safe redirects; container origin; small maps.
import { describe, expect, it } from "vitest";
import { appHref, appId, appIdForContainer, buildApps, kindShort, nameOf, parseAppId, resourceName, statusAge } from "../apps";
import { containerOrigin, isManaged, originBadge } from "../containerOrigin";
import { realTags, truncate } from "../images";
import { errorStatus, liveUnlessGone, setEventsLive, LIVE_REFETCH_MS } from "../live";
import { safeLocalPath } from "../safePath";
import { describeUpdate, updateKey } from "../updates";
import { cn } from "../utils";
import * as u from "../../utils";
import { compileFilter, highlightSegments, matchLine } from "../../components/logs/logStream";
import { applyComposePatch } from "../../components/compose/applyPatch";
import { modeLabel, outcomeColor, outcomeText, scanLabel } from "../../components/autoupdate/labels";
import { softChip, toKitColor } from "../../ui/colors";
import { containers, deployments, hosts } from "../../test/fixtures";
import type { Container } from "../../types";

const c = (over: Partial<Container>): Container => ({
  id: "x".repeat(64), names: ["/c"], image: "i", state: "running", status: "Up 2 minutes", created: 1_700_000_000,
  ports: [], labels: {}, hostId: "local", ...over,
});

describe("apps", () => {
  it("ids round-trip and hrefs", () => {
    for (const p of [{ kind: "deployment", deploymentId: "d1" }, { kind: "stack", hostId: "h", project: "p" },
                     { kind: "container", hostId: "h", name: "n" }] as const) {
      expect(parseAppId(appId(p))).toEqual(p);
    }
    expect(parseAppId("junk")).toBeNull();
    expect(parseAppId("dep~")).toBeNull();
    expect(appHref({ kind: "deployment", deploymentId: "d 1" }, "logs")).toBe("/workloads/dep~d%201?tab=logs");
    expect(appHref("dep~x", "overview")).toBe("/workloads/dep~x");
  });

  it("resource names mirror the server", () => {
    expect(resourceName({ id: "1", name: "  My Shop!! " })).toBe("my-shop");
    expect(resourceName({ id: "abc", name: "***" })).toBe("app-abc");
    expect(resourceName({ id: "", name: "" })).toBe("app");
  });

  it("status ages", () => {
    expect(statusAge("Up 13 minutes (healthy)")).toBe("13m");
    expect(statusAge("Exited (1) 2 hours ago")).toBe("2h");
    expect(statusAge("Up About an hour")).toBe("1h");
    expect(statusAge("Up 5 seconds")).toBe("5s");
    expect(statusAge("Up Less than a second")).toBe("now");
    expect(statusAge("Created")).toBe("");
  });

  it("groups containers into deployments, stacks and singles with a summary", () => {
    const list: Container[] = [
      ...containers,
      c({ id: "s1".padEnd(64, "0"), names: ["/blog-app-1"], labels: { "com.docker.compose.project": "blog", "com.docker.compose.service": "app" }, status: "Up 1 hour (unhealthy)" }),
      c({ id: "s2".padEnd(64, "0"), names: ["/paused"], state: "paused", status: "Up 1 hour (Paused)" }),
      c({ id: "s3".padEnd(64, "0"), names: ["/dead"], state: "dead", status: "Dead" }),
      c({ id: "s4".padEnd(64, "0"), names: ["/stopped"], state: "exited", status: "Exited (0) 1 day ago" }),
      c({ id: "s5".padEnd(64, "0"), names: ["/fresh"], state: "created", status: "Created" }),
      c({ id: "s6".padEnd(64, "0"), names: ["/odd"], state: "removing", status: "Removing" }),
      c({ id: "s7".padEnd(64, "0"), names: ["/booting"], status: "Up 3 seconds (health: starting)" }),
      c({ id: "s8".padEnd(64, "0"), names: ["/loop"], state: "restarting", status: "Restarting (1) 1 second ago" }),
    ];
    const apps = buildApps({ containers: list, fallbackHost: "local", deployments: [
      ...deployments, { ...deployments[1], id: "d-gone", name: "gone", targets: ["h-edge"], status: "error" },
      { ...deployments[1], id: "d-deploying", name: "deploying", status: "deploying" },
    ], scopeHosts: hosts, updates: (h, id) => (id === containers[0].id ? { hostId: h, containerId: id, updateAvailable: true } as never : undefined) });
    const by = Object.fromEntries(apps.map((a) => [a.name, a]));
    expect(by.shop.kind).toBe("deployment");
    expect(by.shop.updates).toBe(1);
    expect(by.shop.ports[0].publicPort).toBe(8080);
    expect(by.blog.kind).toBe("stack");
    expect(by.blog.tone).toBe("a");
    expect(by["nightly-job"].tone).toBe("r");
    expect(by.api.label).toBe("No containers");
    expect(by.gone.label).toBe("Unknown"); // host offline
    expect(by.deploying.label).toBe("Deploying");
    expect(by.stopped.label).toMatch(/stopped/i);
    expect(by.dead.tone).toBe("r");
    expect(kindShort(by.shop)).toMatch(/Compose/);
    expect(kindShort(by.api)).toBe("Container");
    expect(nameOf(c({ names: [] , id: "abc" }))).toBe("abc");
    expect(appIdForContainer(containers[0], "local", deployments)).toBe("dep~d-shop");
    expect(appIdForContainer(containers[1], "local", deployments)).toBe("dep~d-shop");
    expect(appIdForContainer(c({ labels: { "com.docker.compose.project": "x" } }), "local")).toBe("stack~local~x");
    expect(appIdForContainer(c({}), "local")).toBe("ctr~local~c");
  });
});

describe("log filters", () => {
  const line = (text: string, level: "error" | "info" = "info") => ({ id: 1, source: "s", text, level, rawTs: "", tsLabel: "" }) as never;

  it("compiles words, phrases, exclusions, levels and regexes", () => {
    const f = compileFilter('  timeout "db pool" -healthcheck level:error,bogus /5\\d\\d/i -/debug/ ');
    expect(f.includes).toEqual(["timeout", "db pool"]);
    expect(f.excludes).toEqual(["healthcheck"]);
    expect([...f.levels!]).toEqual(["error"]);
    expect(matchLine(line("Timeout from DB POOL: 503", "error"), f)).toBe(true);
    expect(matchLine(line("timeout db pool 503 healthcheck", "error"), f)).toBe(false);
    expect(matchLine(line("timeout db pool 200", "error"), f)).toBe(false);
    expect(matchLine(line("timeout db pool 503 debug", "error"), f)).toBe(false);
    expect(matchLine(line("timeout db pool 503"), f)).toBe(false); // level
    expect(compileFilter("/[unclosed/").error).toMatch(/invalid regex/i);
    expect(compileFilter("").includes).toEqual([]);
    expect(compileFilter('"" -').includes).toEqual([]);
  });

  it("highlights merged ranges", () => {
    const f = compileFilter("err /or\\b/");
    expect(highlightSegments("an error occurred", f).filter((s) => s.hit).map((s) => s.text)).toEqual(["error"]);
    expect(highlightSegments("nothing", compileFilter(""))).toEqual([{ text: "nothing", hit: false }]);
    expect(highlightSegments("nothing", f)).toEqual([{ text: "nothing", hit: false }]);
    expect(highlightSegments("aaa", compileFilter("/a*/")).some((s) => s.hit)).toBe(true);
  });
});

describe("formatting and misc", () => {
  it("formats bytes, percents, dates", () => {
    expect(u.formatBytes(0)).toBe("0 B");
    expect(u.formatBytes(1536)).toBe("1.5 KB");
    expect(u.formatBytes(5 * 2 ** 40)).toBe("5.0 TB");
    expect(u.formatPercent(Number.NaN)).toBe("0%");
    expect(u.formatPercent(12.345)).toBe("12.3%");
    expect(u.formatDate(null)).toBe("-");
    expect(u.formatDate("nope")).toBe("-");
    expect(u.formatDate(1_700_000_000)).not.toBe("-");
    expect(u.formatDate(new Date(0))).not.toBe("-");
    expect(u.timeAgo(new Date(Date.now() - 3 * 3600_000))).toBe("3 hours ago");
    expect(u.timeAgo(new Date(Date.now() - 1000))).toBe("1 second ago");
    expect(u.timeAgo(Date.now())).toBe("just now");
    expect(u.timeAgo(Math.floor(Date.now() / 1000) - 120)).toBe("2 minutes ago");
    expect(u.timeAgo(new Date(Date.now() + 86400_000))).not.toMatch(/ago/);
    expect(u.timeAgo("garbage")).toBe("-");
    expect(u.timeAgo("")).toBe("-");
  });

  it("names, ids, colours, errors", () => {
    expect(u.containerName(["/web"])).toBe("web");
    expect(u.containerName([])).toBe("-");
    expect(u.shortId("sha256:" + "f".repeat(64))).toBe("ffffffffffff");
    expect(u.shortId(undefined)).toBe("-");
    for (const [s, col] of [["running", "green"], ["paused", "yellow"], ["exited", "red"], ["weird", "gray"]]) {
      expect(u.stateColor(s)).toBe(col);
    }
    for (const [l, col] of [["critical", "red"], ["warning", "yellow"], ["info", "blue"], [undefined, "gray"]] as const) {
      expect(u.levelColor(l)).toBe(col);
    }
    expect(u.errorMessage({ response: { data: { error: "boom" } } }, "x")).toBe("boom");
    expect(u.errorMessage({ response: { data: { message: "m" } } }, "x")).toBe("m");
    expect(u.errorMessage(new Error("e"), "x")).toBe("e");
    expect(u.errorMessage(null, "fallback")).toBe("fallback");
    expect(cn("a", false && "b", "c")).toBe("a c");
  });

  it("safe local paths", () => {
    expect(safeLocalPath("/volumes?x=1")).toBe("/volumes?x=1");
    for (const bad of ["", null, "https://evil", "//evil.com", "/\\evil.com", "/a\u0000b", "relative"]) {
      expect(safeLocalPath(bad as string)).toBe("/");
    }
  });

  it("container origins", () => {
    const dep = containerOrigin({ "kowan.deployment.id": "d1" });
    expect(dep).toMatchObject({ kind: "deployment", name: "d1" });
    expect(isManaged(dep)).toBe(true);
    expect(originBadge(dep).group).toBe("Deployments");
    const stack = containerOrigin({ "com.docker.compose.project": "p", "com.docker.compose.service": "s" });
    expect(originBadge(stack).label).toBe("Stack");
    const plain = containerOrigin(undefined);
    expect(isManaged(plain)).toBe(false);
    expect(originBadge(plain).label).toBe("Standalone");
  });

  it("images, live polling, updates, labels, colours", () => {
    expect(realTags({ id: "x", repoTags: ["<none>:<none>", "a:1"], size: 0, created: 0 })).toEqual(["a:1"]);
    expect(realTags(null)).toEqual([]);
    expect(truncate("abcdef", 4)).toBe("abcd…");
    expect(truncate("abc", 4)).toBe("abc");
    expect(errorStatus({ response: { status: 404 } })).toBe(404);
    expect(errorStatus("x")).toBeUndefined();
    expect(liveUnlessGone({ state: { error: { response: { status: 404 } } } })).toBe(false);
    setEventsLive(true);
    expect(LIVE_REFETCH_MS()).toBe(30_000);
    setEventsLive(false);
    expect(LIVE_REFETCH_MS()).toBe(5_000);
    expect(typeof liveUnlessGone({ state: { error: null } })).toBe("number");
    expect(updateKey("h", "c")).toBe("h/c");
    expect(describeUpdate({ updateAvailable: true } as never)).toMatch(/./);
    expect(describeUpdate({ updateAvailable: false, error: "registry down" } as never)).toMatch(/registry/i);
    expect(modeLabel("apply")).toMatch(/./);
    expect(scanLabel("critical")).toMatch(/./);
    for (const o of ["updated", "blocked", "rolled_back", "failed", "notified", "other"]) {
      expect(outcomeColor(o)).toBeTruthy();
      expect(outcomeText(o)).toBeTruthy();
    }
    expect(toKitColor("green")).toBeTruthy();
    expect(toKitColor(undefined, "danger")).toBe("danger");
    expect(softChip("red").background).toBeTruthy();
  });

  it("applies compose patches", () => {
    const text = "services:\n  web:\n    image: nginx\n";
    const out = applyComposePatch(text, [{ op: "set", path: ["services", "web", "restart"], value: "unless-stopped" }] as never);
    expect(out).toContain("restart: unless-stopped");
    expect(applyComposePatch("not: [valid", [{ op: "set", path: ["a"], value: 1 }] as never)).toBeNull();
  });
});
