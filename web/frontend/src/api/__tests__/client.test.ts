// The typed REST client: every exported call reaches /api/v1 with the right
// method, path, query and body; errors come back shaped like axios; tokens and
// 401 handling; WebSocket URL builders.
import { describe, expect, it, vi } from "vitest";
import * as client from "../client";
import { createHttp, HttpError } from "../http";
import { calls, mockApi, reply, requests } from "../../test/api";

const SKIP = new Set(["getToken", "setToken", "clearToken", "saveBlob", "ssoStartUrl", "revokeSession"]);

describe("api client", () => {
  it("every call issues one request under /api/v1", async () => {
    const fns = Object.entries(client).filter(([k, v]) => typeof v === "function" && !SKIP.has(k) && !k.endsWith("WebSocketURL"));
    expect(fns.length).toBeGreaterThan(150);
    const silent: string[] = [];
    for (const [name, fn] of fns) {
      const before = calls.length;
      // Plausible arguments: ids are strings, objects/forms get an empty body.
      const args = ["h1", "x1", { name: "n" }, "y1", true];
      try {
        await (fn as (...a: unknown[]) => Promise<unknown>)(...args);
      } catch {
        // Some calls validate their response shape; only the request matters here.
      }
      if (calls.length === before) silent.push(name);
    }
    expect(silent).toEqual([]);
    expect(calls.every((c) => !c.path.startsWith("/api/v1"))).toBe(true); // prefix stripped by the fake
  });

  it("sends the bearer token and maps errors like axios", async () => {
    client.setToken("t-123");
    mockApi({ "GET /hosts": reply(500, { error: "db locked" }) });
    await expect(client.listHosts()).rejects.toMatchObject({ response: { status: 500, data: { error: "db locked" } } });
    expect(calls[0].headers.authorization).toBe("Bearer t-123");
    expect(client.getToken()).toBe("t-123");
    client.clearToken();
    expect(client.getToken()).toBeNull();
  });

  it("logs out locally on 401 and sends the browser to /login", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, pathname: "/volumes", assign });
    client.setToken("expired");
    mockApi({ "GET /hosts": reply(401, { error: "token expired" }) });
    await expect(client.listHosts()).rejects.toBeInstanceOf(HttpError);
    expect(client.getToken()).toBeNull();
    expect(assign).toHaveBeenCalledWith("/login");
    vi.unstubAllGlobals();
  });

  it("builds query strings and bodies", async () => {
    mockApi({ "GET /hosts/:host/containers": [] , "POST /hosts/:host/images/pull": {} });
    await client.listContainers("local");
    expect(requests("GET", "/hosts/local/containers")[0].query.all).toBe("true");
    await client.login("root", "pw");
    expect(requests("POST", "/auth/login")[0].body).toEqual({ username: "root", password: "pw" });
  });

  it("revokes the session only when there is a token", async () => {
    client.revokeSession();
    expect(calls).toHaveLength(0);
    client.setToken("t");
    client.revokeSession();
    await new Promise((r) => setTimeout(r, 0));
    expect(requests("POST", "/auth/logout")[0].headers.authorization).toBe("Bearer t");
  });

  it("builds websocket URLs on the current origin", () => {
    expect(client.logsWebSocketURL("local", "abc")).toMatch(/^ws:\/\/[^/]+\/api\/v1\/hosts\/local\/containers\/abc\/logs/);
    expect(client.statsStreamWebSocketURL("local", "abc")).toMatch(/stats\/stream$/);
    expect(client.execWebSocketURL("local", "abc", ["/bin/sh"])).toMatch(/exec\?/);
    expect(client.deploymentLogsWebSocketURL("d1", "local", { tail: 50, container: "web" })).toMatch(/\/deployments\/d1\/logs\?.*host=local/);
    expect(client.eventsWebSocketURL("local")).toMatch(/\/events\/ws/);
    expect(client.ssoStartUrl("p1", "/x")).toMatch(/\/auth\/sso\/p1\/start\?returnTo=%2Fx/);
  });
});

describe("http wrapper", () => {
  it("handles text, blob, form data, network errors and empty bodies", async () => {
    const http = createHttp({ baseURL: "/api/v1" });
    mockApi({
      "GET /text": reply(200, "plain", { "content-type": "text/plain" }),
      "GET /empty": reply(204),
      "POST /form": (r) => ({ isForm: r.body instanceof FormData }),
    });
    expect((await http.get("/text", { responseType: "text" })).data).toBe("plain");
    expect((await http.get("/empty")).status).toBe(204);
    const fd = new FormData();
    fd.append("f", "x");
    expect((await http.post<{ isForm: boolean }>("/form", fd)).data.isForm).toBe(true);
    expect((await http.get("/text", { params: { a: 1, b: undefined, c: null } })).status).toBe(200);
    expect(requests("GET", "/text").at(-1)!.query).toEqual({ a: "1" });
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    await expect(http.get("/x")).rejects.toThrow("Network Error");
    vi.unstubAllGlobals();
  });
});
