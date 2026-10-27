import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { useLogStream, type LogSource } from "../logs/logStream";
import { useDockerEvents } from "../shell/useDockerEvents";
import { hostsQuery, HOSTS_POLL_MS, useHosts } from "../../lib/hostsQuery";
import { baseApi, newQueryClient, Providers } from "../../test/render";
import { requests, sockets } from "../../test/api";

// jsdom always reports "visible": override the getter to simulate tab switches.
let visibility: DocumentVisibilityState = "visible";
Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
function setVisibility(v: DocumentVisibilityState) {
  act(() => {
    visibility = v;
    document.dispatchEvent(new Event("visibilitychange"));
  });
}
afterEach(() => {
  visibility = "visible";
});

const source: LogSource = { key: "web", label: "web", url: ({ tail }) => `ws://test/logs/web?tail=${tail}` };
const live = () => sockets.filter((s) => s.url.includes("/logs/web") && s.readyState !== 3);

describe("log stream and tab visibility", () => {
  it("closes on hide, opens nothing while hidden and reconnects when visible", async () => {
    const { result, rerender } = renderHook(({ reloadKey }) => useLogStream([source], { tail: 10, follow: true, reloadKey }), {
      initialProps: { reloadKey: 0 },
    });
    expect(live()).toHaveLength(1);
    await act(async () => { await Promise.resolve(); });
    act(() => live()[0].emit("hello"));
    await waitFor(() => expect(result.current.lines.map((l) => l.text)).toEqual(["hello"]));
    expect(result.current.status.web).toBe("live");

    setVisibility("hidden");
    expect(live()).toHaveLength(0);
    expect(result.current.status.web).toBe("closed");
    // Lines already received stay on screen.
    expect(result.current.lines).toHaveLength(1);
    const opened = sockets.length;
    // An auto-refresh tick while hidden must not open anything.
    rerender({ reloadKey: 1 });
    expect(sockets.length).toBe(opened);

    setVisibility("visible");
    expect(live()).toHaveLength(1);
    expect(sockets.length).toBe(opened + 1);
    expect(result.current.lines).toHaveLength(0);
  });

  it("does not connect while mounted in a hidden tab", () => {
    visibility = "hidden";
    renderHook(() => useLogStream([source], { tail: 10, follow: true }));
    expect(live()).toHaveLength(0);
    setVisibility("visible");
    expect(live()).toHaveLength(1);
  });
});

describe("shared hosts query", () => {
  it("serves the host switcher and Docker events from one polled query", async () => {
    baseApi("admin");
    const client = newQueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => <Providers client={client}>{children}</Providers>;
    function Both() {
      useDockerEvents();
      const hosts = useHosts();
      return <span>{hosts.data?.length ?? "loading"} hosts</span>;
    }
    render(<Both />, { wrapper });
    expect(await screen.findByText(/^[1-9]\d* hosts$/)).toBeInTheDocument();
    const query = client.getQueryCache().find({ queryKey: hostsQuery.queryKey })!;
    expect(query.getObserversCount()).toBe(2);
    for (const o of query.observers) {
      expect(o.options.refetchInterval).toBe(HOSTS_POLL_MS);
      expect(o.options.staleTime).toBe(hostsQuery.staleTime);
    }
    expect(requests("GET", "/hosts")).toHaveLength(1);
  });
});
