import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import { Highlight, highlightTerms } from "../../components/logs/LogHistory";
import { daysLabel, workloadName } from "../../components/logs/retention";
import { deployments, hosts } from "../../test/fixtures";

const retention = [
  { workload: "dep~d-shop", days: 7, createdAt: "2026-10-20T00:00:00Z", createdBy: "root", lines: 1200, oldest: "2026-10-20T00:00:00.000000000Z" },
  { workload: "ctr~h-edge~cache", days: 30, createdAt: "2026-10-20T00:00:00Z", createdBy: "root", lines: 3, oldest: "" },
];
const line = (id: number, text: string, over: Record<string, unknown> = {}) => ({
  id, ts: "2026-10-26T09:00:00.000000000Z", line: text, hostId: "local", hostName: "local", containerId: "c1",
  container: "shop-web-1", workload: "dep~d-shop", stack: "shop", deploymentId: "d-shop", ...over,
});

describe("retained log helpers", () => {
  it("names workloads and highlights the searched words", () => {
    expect(daysLabel(1)).toBe("1 day");
    expect(daysLabel(30)).toBe("30 days");
    expect(workloadName("dep~d-shop", deployments, hosts)).toEqual({ name: "shop", where: "local" });
    expect(workloadName("dep~gone1234abcd", deployments, hosts)).toEqual({ name: "gone1234", where: "deployment" });
    expect(workloadName("stack~h-edge~shop", deployments, hosts)).toEqual({ name: "shop", where: "edge-1" });
    expect(workloadName("ctr~unknown~web", deployments, hosts)).toEqual({ name: "web", where: "unknown" });
    expect(workloadName("bad", deployments, hosts)).toEqual({ name: "bad", where: "" });
    expect(highlightTerms('error "db down" -health -')).toEqual(["error", "db down", "-"]);
    const { container } = render(<Highlight text="Error: DB down (error)" terms={["error", "db down"]} />);
    expect([...container.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(["Error", "DB down", "error"]);
    const plain = render(<Highlight text="a.b" terms={[]} />);
    expect(plain.container.querySelector("mark")).toBeNull();
  });
});

describe("logs history", () => {
  it("explains how to start when no workload keeps logs", async () => {
    const { user } = renderApp("/logs?mode=history", { api: { "GET /log-archive/retention": [] } });
    expect(await screen.findByText(/no workload keeps its logs yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(requests("GET", "/log-archive/search")).toHaveLength(0);
    await user.click(screen.getByRole("tab", { name: "Live" }));
    await waitFor(() => expect(currentPath()).toBe("/logs"));
    expect(await screen.findByPlaceholderText(/filter containers/i)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "History" }));
    await waitFor(() => expect(currentPath()).toContain("mode=history"));
  });

  it("searches across workloads with filters, highlights and older pages", async () => {
    const { user } = renderApp("/logs?mode=history", {
      api: {
        "GET /log-archive/retention": retention,
        "GET /log-archive/search": (r) =>
          r.query.before === "7"
            ? { items: [line(3, "older line")], next: null }
            : { items: [line(9, "GET /api 500 error"), line(7, "cache miss", { container: "cache", hostName: "edge-1", workload: "ctr~h-edge~cache" })], next: 7 },
      },
    });
    const root = pageRoot();
    expect(await within(root).findByText("GET /api 500 error", {}, { timeout: 10_000 })).toBeInTheDocument();
    // Two hosts: the host is shown next to the container.
    expect(screen.getByText("@edge-1")).toBeInTheDocument();
    expect(screen.getByText(/local · 7 days · 1,200 lines/)).toBeInTheDocument();
    expect(requests("GET", "/log-archive/search")[0].query).toMatchObject({ hours: "24", limit: "200" });

    await user.click(screen.getByRole("button", { name: /load older lines/i }));
    expect(await screen.findByText("older line")).toBeInTheDocument();
    expect(screen.getByText(/start of the kept logs/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Search logs"), "error -health");
    await choose(user, "Time range", "Last 7 days");
    await user.click(screen.getByRole("checkbox", { name: /filter by shop/i }));
    await press(user, /^search$/i, screen.getByLabelText("Search logs").closest("form")!);
    await waitFor(() => {
      const last = requests("GET", "/log-archive/search").at(-1)!;
      expect(last.query).toMatchObject({ q: "error -health", hours: "168", workload: "dep~d-shop" });
    });
    await waitFor(() => expect(document.querySelector("mark")?.textContent).toBe("error"));
    expect(screen.getByRole("checkbox", { name: /filter by shop/i })).toHaveAttribute("aria-checked", "true");
    await press(user, "All");
    expect(screen.getByRole("checkbox", { name: /filter by shop/i })).toHaveAttribute("aria-checked", "false");

    await choose(user, "Time range", "Everything kept");
    await waitFor(() => expect(requests("GET", "/log-archive/search").at(-1)!.query.hours).toBeUndefined());
  });

  it("starts filtered on the workload from the link and shows empty and failed searches", async () => {
    renderApp("/logs?mode=history&workload=dep~d-shop", {
      api: { "GET /log-archive/retention": retention, "GET /log-archive/search": { items: [], next: null } },
    });
    expect(await screen.findByText(/no matching lines/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(requests("GET", "/log-archive/search")[0].query.workload).toBe("dep~d-shop");
    expect(screen.getByRole("checkbox", { name: /filter by shop/i })).toHaveAttribute("aria-checked", "true");
  });

  it("reports a failed search", async () => {
    renderApp("/logs?mode=history", {
      api: { "GET /log-archive/retention": retention, "GET /log-archive/search": reply(400, { error: "since must be an RFC3339 time" }) },
    });
    expect(await screen.findByText(/since must be an RFC3339 time/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("lets operators change and stop retention", async () => {
    const { user } = renderApp("/logs?mode=history", {
      role: "operator",
      api: {
        "GET /log-archive/retention": retention,
        "GET /log-archive/search": { items: [line(1, "x")], next: null },
        "PUT /log-archive/retention/:w": (r) => ({ ...retention[0], workload: r.params.w, days: r.body.days }),
        "DELETE /log-archive/retention/:w": { removed: 1200 },
      },
    });
    await screen.findByText("x", {}, { timeout: 10_000 });
    await user.click(screen.getByRole("button", { name: /retention of shop/i }));
    await user.click(await screen.findByRole("menuitem", { name: /90 days/ }));
    await toast(/keeps logs for 90 days/i);
    expect(requests("PUT", "/log-archive/retention/:w")[0]).toMatchObject({ path: "/log-archive/retention/dep~d-shop", body: { days: 90 } });

    await user.click(screen.getByRole("button", { name: /retention of shop/i }));
    await user.click(await screen.findByRole("menuitem", { name: /stop keeping/i }));
    const dlg = await dialog(/stop keeping logs/i);
    expect(within(dlg).getByText(/1,200 stored lines are deleted/)).toBeInTheDocument();
    await press(user, /stop and delete/i, dlg);
    await toast(/1,200 stored lines deleted/i);
    expect(requests("DELETE", "/log-archive/retention/:w")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: /retention of cache/i }));
    await user.click(await screen.findByRole("menuitem", { name: /open workload/i }));
    await waitFor(() => expect(currentPath()).toBe("/workloads/ctr~h-edge~cache?tab=logs"));
  });

  it("reports a failed retention change", async () => {
    const { user } = renderApp("/logs?mode=history", {
      role: "operator",
      api: {
        "GET /log-archive/retention": retention,
        "GET /log-archive/search": { items: [], next: null },
        "PUT /log-archive/retention/:w": reply(400, { error: "days must be between 1 and 365" }),
        "DELETE /log-archive/retention/:w": reply(500, { error: "disk full" }),
      },
    });
    await screen.findByText(/no matching lines/i, {}, { timeout: 10_000 });
    await user.click(screen.getByRole("button", { name: /retention of shop/i }));
    await user.click(await screen.findByRole("menuitem", { name: /1 day/ }));
    await toast(/days must be between 1 and 365/i);
    await user.click(screen.getByRole("button", { name: /retention of shop/i }));
    await user.click(await screen.findByRole("menuitem", { name: /stop keeping/i }));
    await press(user, /stop and delete/i, await dialog(/stop keeping logs/i));
    await toast(/disk full/i);
  });

  it("hides retention controls from viewers", async () => {
    renderApp("/logs?mode=history", {
      role: "viewer",
      api: { "GET /log-archive/retention": retention, "GET /log-archive/search": { items: [], next: null } },
    });
    await screen.findByText(/no matching lines/i, {}, { timeout: 10_000 });
    expect(screen.queryByRole("button", { name: /retention of/i })).toBeNull();
  });
});

describe("keep logs on a workload", () => {
  const api = {
    "PUT /log-archive/retention/:w": (r: { params: { w: string }; body: { days: number } }) => ({ ...retention[0], workload: r.params.w, days: r.body.days }),
    "DELETE /log-archive/retention/:w": { removed: 1200 },
  };

  it("turns retention on from the logs tab", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=logs", { api: { ...api, "GET /log-archive/retention": [] } as never });
    const bar = await screen.findByTestId("log-retention", {}, { timeout: 10_000 });
    expect(within(bar).getByText(/only what docker still holds/i)).toBeInTheDocument();
    expect(within(bar).queryByText(/search history/i)).toBeNull();
    await choose(user, "Keep logs", "14 days", bar);
    await toast(/new lines are kept for 14 days/i);
    expect(requests("PUT", "/log-archive/retention/:w")[0]).toMatchObject({ path: "/log-archive/retention/dep~d-shop", body: { days: 14 } });
  });

  it("turns retention off after confirming and links to the history", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=logs", { api: { ...api, "GET /log-archive/retention": retention } as never });
    const bar = await screen.findByTestId("log-retention", {}, { timeout: 10_000 });
    expect(within(bar).getByText(/1,200 lines stored/)).toBeInTheDocument();
    await choose(user, "Keep logs", "Off", bar);
    const dlg = await dialog(/stop keeping logs/i);
    await press(user, /stop and delete/i, dlg);
    await toast(/the stored lines were deleted/i);
    expect(requests("DELETE", "/log-archive/retention/:w")).toHaveLength(1);
    await user.click(within(bar).getByRole("link", { name: /search history/i }));
    await waitFor(() => expect(currentPath()).toBe("/logs?mode=history&workload=dep~d-shop"));
  });

  it("turns off without asking when nothing is stored, and reports errors", async () => {
    const empty = [{ ...retention[0], lines: 0, days: 45 }];
    const { user } = renderApp("/workloads/dep~d-shop?tab=logs", {
      api: { "GET /log-archive/retention": empty, "DELETE /log-archive/retention/:w": reply(500, { error: "locked" }) },
    });
    const bar = await screen.findByTestId("log-retention", {}, { timeout: 10_000 });
    // A retention the menu does not offer is still shown.
    expect(within(bar).getByRole("combobox", { name: "Keep logs" })).toHaveTextContent("45 days");
    await choose(user, "Keep logs", "Off", bar);
    await toast(/locked/i);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the setting read-only to viewers", async () => {
    renderApp("/workloads/dep~d-shop?tab=logs", { role: "viewer", api: { "GET /log-archive/retention": retention } });
    const bar = await screen.findByTestId("log-retention", {}, { timeout: 10_000 });
    expect(within(bar).getByText("7 days")).toBeInTheDocument();
    expect(within(bar).queryByRole("combobox")).toBeNull();
  });
});
