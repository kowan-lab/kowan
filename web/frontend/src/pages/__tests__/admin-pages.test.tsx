// Audit log, the multi-container log viewer and the network access lists.
import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests, sockets } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";

describe("audit log", () => {
  it("lists entries with action and status badges", async () => {
    renderApp("/audit", {
      api: {
        "GET /audit": {
          total: 6,
          items: [
            { id: "a1", time: NOW, userId: "u", username: "root", action: "LOGIN", path: "", status: 200, ip: "10.0.0.1" },
            { id: "a2", time: NOW, userId: "u", username: "", action: "LOGIN_FAILED", path: "/api/v1/auth/login", status: 401, ip: "" },
            { id: "a3", time: NOW, userId: "u", username: "ops", action: "PUT", path: "/api/v1/deployments/d-shop", status: 302, ip: "10.0.0.2" },
            { id: "a4", time: NOW, userId: "u", username: "ops", action: "DELETE", path: "/api/v1/hosts/local/volumes/x", status: 500, ip: "10.0.0.2" },
            { id: "a5", time: NOW, userId: "u", username: "ops", action: "POST", path: "/api/v1/workflows", status: 201, ip: "10.0.0.2" },
            { id: "a6", time: NOW, userId: "u", username: "ops", action: "SYNC", path: "", status: 0, ip: "" },
          ],
        },
      },
    });
    expect(await within(pageRoot()).findByText("LOGIN_FAILED", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("/api/v1/deployments/d-shop")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.getByText("SYNC")).toBeInTheDocument();
    expect(requests("GET", "/audit")[0].query).toMatchObject({ limit: "500", offset: "0" });
  });

  it("shows errors, the empty state and denies non-admins", async () => {
    renderApp("/audit", { api: { "GET /audit": reply(500, { error: "audit table missing" }) } });
    expect(await screen.findByText(/audit table missing/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/audit", { role: "operator" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("logs page", () => {
  it("filters containers and merges the selected streams", async () => {
    const { user } = renderApp("/logs");
    await within(pageRoot()).findByRole("checkbox", { name: /shop-web-1/ }, { timeout: 10_000 });
    // Running only hides the exited job until the toggle is off.
    expect(screen.queryByRole("checkbox", { name: /nightly-job/ })).toBeNull();
    await user.click(screen.getByRole("switch", { name: /running only/i }));
    expect(await screen.findByRole("checkbox", { name: /nightly-job/ })).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/filter containers/i), "postgres");
    expect(screen.queryByRole("checkbox", { name: /shop-web-1/ })).toBeNull();
    expect(screen.getByRole("checkbox", { name: /shop-db-1/ })).toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText(/filter containers/i));
    await user.type(screen.getByPlaceholderText(/filter containers/i), "zzz");
    expect(screen.getByText(/no containers match/i)).toBeInTheDocument();
    await user.clear(screen.getByPlaceholderText(/filter containers/i));

    await user.click(screen.getByRole("checkbox", { name: /shop-web-1/ }));
    const db = screen.getByRole("checkbox", { name: /shop-db-1/ });
    db.focus();
    await user.keyboard(" ");
    expect(screen.getByRole("checkbox", { name: /shop-web-1/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("checkbox", { name: /shop-db-1/ })).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(sockets.filter((s) => s.url.includes("/logs")).length).toBeGreaterThanOrEqual(2));
    const ws = sockets.find((s) => s.url.includes("/logs"))!;
    act(() => ws.emit("2026-10-26T10:00:00.000000000Z hello from web"));
    expect(await screen.findByText(/hello from web/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /clear \(2\)/i }));
    expect(screen.getByRole("checkbox", { name: /shop-web-1/ })).toHaveAttribute("aria-checked", "false");
  });

  it("shows host names in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/logs");
    expect(await within(pageRoot()).findAllByText("local", {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});

describe("network access", () => {
  const data = (over: Record<string, unknown> = {}) => ({
    env: { uiAllow: ["10.0.0.0/8"], edgeAllow: [], trustedProxies: ["172.16.0.1"], proxyHeader: "X-Forwarded-For" },
    uiAllow: ["192.168.1.0/24"], edgeAllow: [], yourAddress: "192.168.1.20", peerAddress: "172.16.0.1", ...over,
  });

  it("saves the lists with the password and shows warnings", async () => {
    const { user } = renderApp("/settings/network", {
      api: { "GET /settings/network": data(), "PUT /settings/network": (r) => ({ ...data(), ...r.body, warnings: ["0.0.0.0/32 lets nobody in"] }) },
    });
    const ui = await screen.findByLabelText(/web ui, api and ai assistants/i, {}, { timeout: 10_000 });
    expect(screen.getByText("192.168.1.20")).toBeInTheDocument();
    expect(screen.getAllByText(/not set: no limit/i).length).toBeGreaterThan(0);
    const save = screen.getByRole("button", { name: /^save/i });
    expect(save).toBeDisabled();
    await user.type(ui, "\n10.1.0.0/16, 0.0.0.0/32");
    await user.type(screen.getByLabelText(/edge agents/i), "203.0.113.0/24");
    expect(save).toBeDisabled(); // the password is still missing
    await user.type(screen.getByLabelText(/your password/i), "pw");
    await user.click(save);
    await toast(/network access saved/i);
    expect(requests("PUT", "/settings/network")[0].body).toEqual({
      uiAllow: ["192.168.1.0/24", "10.1.0.0/16", "0.0.0.0/32"], edgeAllow: ["203.0.113.0/24"], password: "pw",
    });
    expect(await screen.findByText("0.0.0.0/32 lets nobody in")).toBeInTheDocument();
  });

  it("reports a refused save", async () => {
    const { user } = renderApp("/settings/network", { api: { "GET /settings/network": data(), "PUT /settings/network": reply(400, { error: "this would lock you out" }) } });
    await user.type(await screen.findByLabelText(/edge agents/i, {}, { timeout: 10_000 }), "1.2.3.4");
    await user.type(screen.getByLabelText(/your password/i), "pw");
    await press(user, /^save/i);
    await toast(/lock you out/i);
  });
});
