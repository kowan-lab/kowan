import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttp } from "../http";

describe("request timeouts", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("ends a read that gets no answer, and leaves writes alone", async () => {
    const seen: (AbortSignal | undefined)[] = [];
    vi.stubGlobal("fetch", (_url: string, init: RequestInit) => {
      seen.push(init.signal ?? undefined);
      return new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
    });
    const api = createHttp({ baseURL: "/api/v1" });
    await expect(api.get("/hosts/h1/containers", { timeout: 30 })).rejects.toThrow(/did not answer in 0 s/);
    const write = api.post("/deployments/x/deploy", {});
    await new Promise((r) => setTimeout(r, 60));
    expect(seen[1]?.aborted).toBe(false); // still waiting
    const ctl = new AbortController();
    const read = api.get("/x", { signal: ctl.signal });
    ctl.abort();
    await expect(read).rejects.toThrow(/aborted/);
    void write.catch(() => {});
  });
});
