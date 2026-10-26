// Pure helpers: workload grouping, ids and status text; log filters and
// highlighting; formatting; safe redirects; container origin; small maps.
import { describe, expect, it } from "vitest";
import { appHref, appId, appIdForContainer, buildApps, kindShort, nameOf, parseAppId, resourceName, servicesOf, statusAge, superseded, composeServices } from "../apps";
import { buildNeeds } from "../../components/overview/NeedsYou";
import { trouble } from "../../components/overview/model";
import { MAX_REPLICAS, scaledInput, scaleRule } from "../scale";
import { containerOrigin, isManaged, serviceOf, stackOf } from "../containerOrigin";
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

  it("groups swarm stacks and stacks by service", () => {
    const swarm = (name: string, svc: string, state = "running", status = "Up 1 hour") =>
      c({ id: name.padEnd(64, "0"), names: [`/${name}`], state, status, labels: { "com.docker.stack.namespace": "mon", "com.docker.swarm.service.name": `mon_${svc}` } });
    expect(stackOf(swarm("a", "web").labels)).toBe("mon");
    expect(serviceOf(swarm("a", "web").labels)).toBe("web");
    expect(serviceOf({ "com.docker.swarm.service.name": "solo" })).toBe("solo");
    expect(stackOf({ "com.docker.swarm.service.name": "solo" })).toBe("solo"); // a lone service is its own stack
    expect(stackOf({})).toBe("");
    expect(containerOrigin(swarm("a", "web").labels)).toMatchObject({ kind: "stack", name: "mon", service: "web" });
    expect(appIdForContainer(swarm("a", "web"), "local")).toBe("stack~local~mon");

    const cs = [
      swarm("mon_web.1.x", "web"), swarm("mon_web.2.y", "web", "exited", "Exited (1) 3 minutes ago"),
      swarm("mon_db.1.z", "db"), swarm("mon_cron.1.w", "cron", "exited", "Exited (0) 1 hour ago"),
      swarm("mon_cron.2.v", "cron", "exited", "Exited (0) 1 hour ago"),
    ];
    const [mon] = buildApps({ containers: cs, fallbackHost: "local", deployments: [], scopeHosts: null });
    expect(mon).toMatchObject({ kind: "stack", name: "mon", total: 5 });
    const svcs = servicesOf(mon)!;
    expect(svcs.map((s) => [s.name, s.containers.length, s.tone, s.label])).toEqual([
      ["cron", 2, "n", "Stopped"],
      ["db", 1, "g", svcs[1].containers[0].label],
      ["web", 2, "r", "Degraded · 1/2"],
    ]);
    const [lone] = buildApps({ containers: [c({ names: ["/solo"] })], fallbackHost: "local", deployments: [], scopeHosts: null });
    expect(servicesOf(lone)).toBeNull();
  });

  it("an old swarm task whose slot runs again is history, not a crash", () => {
    const task = (name: string, svcId: string, state: string, status: string) =>
      c({ id: name.padEnd(64, "0"), names: [`/${name}`], state, status, labels: {
        "com.docker.stack.namespace": "dl", "com.docker.swarm.service.name": "dl_tools", "com.docker.swarm.service.id": svcId, "com.docker.swarm.task.name": name } });
    const now = task("dl_tools.1.new", "s1", "running", "Up 2 days");
    const old = task("dl_tools.1.old", "s1", "exited", "Exited (143) 2 months ago");
    const killed = task("dl_tools.1.older", "s1", "exited", "Exited (137) 3 months ago");
    const other = task("dl_tools.2.gone", "s1", "exited", "Exited (1) 1 hour ago");
    const all = [now, old, killed, other];
    expect(superseded(old, all)).toBe(true);
    expect(superseded(killed, all)).toBe(true);
    expect(superseded(other, all)).toBe(false); // nothing runs in slot 2
    expect(superseded(old, [old, killed])).toBe(false); // the slot has nothing running
    expect(superseded(c({ state: "exited", status: "Exited (1) now" }), all)).toBe(false); // not swarm
    expect(trouble(old, all)).toBeNull();
    expect(trouble(old)).toBe("exited");
    expect(trouble(other, all)).toBe("exited");
    const [app] = buildApps({ containers: [now, old, killed], fallbackHost: "local", deployments: [], scopeHosts: null });
    expect(app.tone).toBe("g");
    // Without a stack namespace the service's tasks still make one workload.
    const lone = (name: string, state: string, status: string) =>
      c({ id: name.padEnd(64, "0"), names: [`/${name}`], state, status, labels: {
        "com.docker.swarm.service.name": "wrapper", "com.docker.swarm.service.id": "w1", "com.docker.swarm.task.name": name } });
    const loneApps = buildApps({ containers: [lone("wrapper.1.a", "running", "Up 3 days"), lone("wrapper.1.b", "exited", "Exited (137) 3 days ago")],
      fallbackHost: "local", deployments: [], scopeHosts: null });
    expect(loneApps.map((a) => [a.name, a.kind, a.total, a.tone])).toEqual([["wrapper", "stack", 2, "g"]]);
    expect(kindShort(loneApps[0])).toBe("Swarm · 1"); // one slot requested
    expect([loneApps[0].up, loneApps[0].desired, loneApps[0].total]).toEqual([1, 1, 2]);
    expect(app.containers.find((x) => x.name === "dl_tools.1.old")!.label).toMatch(/^Replaced/);
  });

  it("a compose workload with a declared service that has no container is degraded", () => {
    expect(composeServices("services:\n  a: {}\n  b: {profiles: [x]}\n  c: {scale: 0}\n  d: {deploy: {replicas: 0}}\n  e:\n")).toEqual(["a", "e"]);
    for (const bad of ["", undefined, "services: [", "services: [1]", "plain"]) expect(composeServices(bad)).toEqual([]);

    const shop = { ...deployments[0], compose: deployments[0].compose + "  cache:\n    image: redis\n  debug:\n    image: busybox\n    profiles: [debug]\n" };
    const app = buildApps({ containers, fallbackHost: "local", deployments: [shop], scopeHosts: hosts }).find((a) => a.name === "shop")!;
    expect(app.tone).toBe("r");
    expect(app.label).toBe("Degraded · 2/3"); // web, db up of web, db, cache
    expect(app.attention?.text).toBe("cache is missing");
    // Replicas come from the compose file; a one-shot that exited 0 is up.
    const job = { ...deployments[0], compose: "services:\n  web: {image: nginx, deploy: {replicas: 2}}\n  db: {image: postgres}\n  migrate: {image: app}\n" };
    const done = { ...containers[1], id: "c-mig".padEnd(64, "0"), names: ["/shop-migrate-1"], state: "exited", status: "Exited (0) 1 minute ago",
      labels: { "com.docker.compose.project": "shop", "com.docker.compose.service": "migrate" } };
    const withJob = buildApps({ containers: [...containers, done], fallbackHost: "local", deployments: [job], scopeHosts: hosts }).find((a) => a.name === "shop")!;
    expect([withJob.up, withJob.desired, withJob.label]).toEqual([3, 4, "Running · 3/4"]); // web 1/2, db, migrate done
    // A swarm deployment's containers carry swarm labels, not compose ones: never "missing".
    const onSwarm = buildApps({ containers, fallbackHost: "local", deployments: [{ ...shop, strategy: "swarm" }], scopeHosts: hosts }).find((a) => a.name === "shop")!;
    expect(onSwarm.attention).toBeNull();
    const whole = buildApps({ containers, fallbackHost: "local", deployments, scopeHosts: hosts }).find((a) => a.name === "shop")!;
    expect(whole.tone).toBe("g");

    const needs = buildNeeds({
      hosts, containers: containers.map((c) => ({ ...c, _host: "local" })), deployments: [shop], allDeployments: [shop],
      alerts: [], updates: [], canOperate: true, restarting: null, resolving: null, onRestart: () => {}, onResolve: () => {},
    });
    expect(needs.find((n) => n.key === "missing:d-shop")).toMatchObject({ sev: "critical", title: "shop: cache missing" });
  });

  it("scale rules and the scaled update", () => {
    const shop = deployments[0];
    expect(scaleRule(shop, "web")).toEqual({ replicas: 1, max: MAX_REPLICAS, locked: "", limit: "" });
    expect(scaleRule(shop, "nope")).toBeNull();
    expect(scaleRule({ ...shop, compose: "services: [" }, "web")).toBeNull();
    expect(scaleRule({ ...shop, git: { repoUrl: "https://x/y.git" } as never }, "web")?.locked).toMatch(/repository/);
    const ports = (p: unknown) => scaleRule({ ...shop, compose: `services:\n  web:\n    image: x\n    ports: ${JSON.stringify(p)}\n` }, "web")!.max;
    expect(ports(["8080:80"])).toBe(1);
    expect(ports(["127.0.0.1:8080:80"])).toBe(1);
    expect(ports([{ target: 80, published: 8080 }])).toBe(1);
    expect(ports(["80"])).toBe(MAX_REPLICAS);
    expect(ports([{ target: 80 }])).toBe(MAX_REPLICAS);
    expect(scaleRule({ ...shop, strategy: "swarm", compose: "services:\n  web:\n    image: x\n    container_name: a\n    deploy: {replicas: 3}\n" }, "web"))
      .toMatchObject({ replicas: 3, max: MAX_REPLICAS });
    expect(scaleRule({ ...shop, compose: "services:\n  web: {image: x, scale: 2}\n" }, "web")?.replicas).toBe(2);
    const single = { ...deployments[1], strategy: "swarm" as const, replicas: 4 };
    expect(scaleRule(single, single.name)?.replicas).toBe(4);
    expect(scaleRule(deployments[1], deployments[1].name)).toBeNull();
    expect(scaledInput(single, single.name, 2)?.replicas).toBe(2);
    expect(scaledInput({ ...shop, compose: "services:\n  web: {image: x, scale: 2}\n" }, "web", 3)?.compose).not.toMatch(/scale/);
    expect(scaledInput({ ...shop, compose: "services: [" }, "web", 3)).toBeNull();
  });

  it("sorts ports the same way every time", () => {
    const p = (privatePort: number, type: string, publicPort: number, ip: string) => ({ privatePort, type, publicPort, ip });
    expect(u.sortPorts([p(443, "tcp", 8443, "::"), p(80, "udp", 0, ""), p(80, "tcp", 8080, "::"), p(80, "tcp", 8080, "0.0.0.0")]))
      .toEqual([p(80, "tcp", 8080, "0.0.0.0"), p(80, "tcp", 8080, "::"), p(80, "udp", 0, ""), p(443, "tcp", 8443, "::")]);
    expect(u.sortPorts(undefined)).toEqual([]);
  });

  it("container origins", () => {
    const dep = containerOrigin({ "kowan.deployment.id": "d1" });
    expect(dep).toMatchObject({ kind: "deployment", name: "d1" });
    expect(isManaged(dep)).toBe(true);
    const stack = containerOrigin({ "com.docker.compose.project": "p", "com.docker.compose.service": "s" });
    expect(stack).toMatchObject({ kind: "stack" });
    const plain = containerOrigin(undefined);
    expect(isManaged(plain)).toBe(false);
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
