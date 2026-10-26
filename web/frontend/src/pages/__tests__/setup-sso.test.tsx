// First-run setup, the SSO landing page and the SSO parts of the login page.
import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TOKEN_KEY } from "../../api/client";
import { mockApi, reply, requests } from "../../test/api";
import { users } from "../../test/fixtures";
import { currentPath, renderApp } from "../../test/render";
import { toast } from "../../test/ui";

const fresh = { "GET /auth/status": { initialized: false, setupTokenRequired: true } };

describe("first-run setup", () => {
  it("validates, creates the admin and signs in", async () => {
    const { user } = renderApp("/setup", {
      role: null,
      api: { ...fresh, "POST /auth/init": { token: "t0", user: users.admin }, "POST /auth/login": { token: "tok", user: users.admin } },
    });
    await user.clear(await screen.findByLabelText(/^username/i, {}, { timeout: 10_000 }));
    await user.click(screen.getByRole("button", { name: /create administrator/i }));
    expect(await screen.findByText(/paste the setup token/i)).toBeInTheDocument();
    expect(screen.getByText(/username is required/i)).toBeInTheDocument();
    expect(screen.getByText(/at least 8 characters/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/setup token/i), " st-123 ");
    await user.type(screen.getByLabelText(/^username/i), "root");
    await user.type(screen.getByLabelText(/^password/i), "long-password");
    await user.type(screen.getByLabelText(/confirm password/i), "other");
    await user.click(screen.getByRole("button", { name: /create administrator/i }));
    expect(await screen.findByText(/do not match/i)).toBeInTheDocument();
    await user.clear(screen.getByLabelText(/confirm password/i));
    await user.type(screen.getByLabelText(/confirm password/i), "long-password");
    await user.click(screen.getByRole("button", { name: /create administrator/i }));
    await toast(/welcome to kowan/i);
    await waitFor(() => expect(currentPath()).toBe("/"));
    expect(requests("POST", "/auth/init")[0].body).toMatchObject({ username: "root", password: "long-password", setupToken: "st-123" });
  });

  it("reports a wrong setup token", async () => {
    const { user } = renderApp("/setup", { role: null, api: { ...fresh, "POST /auth/init": reply(403, { error: "invalid setup token" }) } });
    await user.type(await screen.findByLabelText(/setup token/i, {}, { timeout: 10_000 }), "bad");
    await user.type(screen.getByLabelText(/^password/i), "long-password");
    await user.type(screen.getByLabelText(/confirm password/i), "long-password");
    await user.click(screen.getByRole("button", { name: /create administrator/i }));
    await toast(/invalid setup token/i);
  });

  it("bounces to login once initialized and to the app when signed in", async () => {
    renderApp("/setup", { role: null });
    await waitFor(() => expect(currentPath()).toBe("/login"), { timeout: 10_000 });
  });

  it("sends a signed-in user to the app", async () => {
    renderApp("/setup");
    await waitFor(() => expect(currentPath()).toBe("/"), { timeout: 10_000 });
  });

  it("login sends a fresh server to setup", async () => {
    renderApp("/login", { role: null, api: fresh });
    await waitFor(() => expect(currentPath()).toBe("/setup"), { timeout: 10_000 });
  });
});

describe("single sign-on", () => {
  it("offers providers, hides the password form in SSO-only mode and explains errors", async () => {
    const { user } = renderApp("/login?sso_error=domain_not_allowed", {
      role: null,
      api: { "GET /auth/sso/providers": { providers: [{ id: "p1", name: "okta", type: "oidc", label: "Okta", icon: "" }], ssoOnly: true } },
    });
    const btn = await screen.findByRole("link", { name: /continue with okta/i }, { timeout: 10_000 });
    expect(btn.getAttribute("href")).toMatch(/\/api\/v1\/auth\/sso\/p1\/start\?returnTo=%2F/);
    expect(screen.getByText(/email domain is not allowed/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^password/i)).toBeNull();
    await user.click(screen.getByRole("button", { name: /password|local account|break-glass/i }));
    expect(screen.getByLabelText(/^password/i)).toBeInTheDocument();
  });

  it("explains provider and unknown error codes", async () => {
    renderApp("/login?sso_error=idp_access_denied", { role: null });
    expect(await screen.findByText(/denied the sign-in \(access_denied\)/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("explains an unknown error code", async () => {
    renderApp("/login?sso_error=weird", { role: null });
    expect(await screen.findByText(/single sign-on failed \(weird\)/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("redeems the ticket and continues where the user started", async () => {
    renderApp("/login/sso", { role: null, api: { "POST /auth/sso/exchange": { token: "sso-tok", user: users.operator, returnTo: "/volumes" } } });
    await waitFor(() => expect(currentPath()).toBe("/volumes"), { timeout: 10_000 });
    expect(localStorage.getItem(TOKEN_KEY)).toBe("sso-tok");
    expect(requests("POST", "/auth/sso/exchange")[0].headers["x-requested-with"]).toBe("kowan");
  });

  it("shows a failed exchange and goes back to sign in", async () => {
    const { user } = renderApp("/login/sso", { role: null, api: { "POST /auth/sso/exchange": reply(400, { error: "ticket expired" }) } });
    expect(await screen.findByText(/ticket expired/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    mockApi({});
    await user.click(screen.getByRole("button", { name: /back to sign in/i }));
    await waitFor(() => expect(currentPath()).toBe("/login"));
  });
});
