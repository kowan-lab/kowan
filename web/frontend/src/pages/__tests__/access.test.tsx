// Users, API keys and teams: who can reach what.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

async function row(name: string) {
  return within(pageRoot()).findByText(name, {}, { timeout: 10_000 });
}

describe("users page", () => {
  it("validates and creates a user", async () => {
    const { user } = renderApp("/users");
    mockApi({ "POST /users": (r) => ({ id: "u-new", ...(r.body as object) }) });
    await row("watcher");
    await press(user, /new user/i, pageRoot());
    const d = await dialog(/new user/i);
    await press(user, /^create$/i, d);
    expect(await within(d).findByText(/username is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/username/i), "jane");
    await press(user, /^create$/i, d);
    expect(await within(d).findByText(/password is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^password/i), "pw-123456");
    await user.type(within(d).getByLabelText(/email/i), "jane@example.com");
    await choose(user, /role/i, /operator/i, d);
    await press(user, /^create$/i, d);
    await toast(/user created/i);
    expect(requests("POST", "/users")[0].body).toEqual({ username: "jane", password: "pw-123456", role: "operator", email: "jane@example.com" });
  });

  it("reports a duplicate username", async () => {
    const { user } = renderApp("/users", { api: { "POST /users": reply(409, { error: "username already exists" }) } });
    await row("watcher");
    await press(user, /new user/i, pageRoot());
    const d = await dialog(/new user/i);
    await user.type(within(d).getByLabelText(/username/i), "ops");
    await user.type(within(d).getByLabelText(/^password/i), "x");
    await press(user, /^create$/i, d);
    await toast(/already exists/i);
  });

  it("edits role, email and password", async () => {
    const { user } = renderApp("/users");
    mockApi({ "PUT /users/:id": (r) => ({ id: r.params.id, username: "watcher", ...(r.body as object) }) });
    await row("watcher");
    await user.click(screen.getByRole("button", { name: "Edit watcher" }));
    const d = await dialog(/edit watcher/i);
    await choose(user, /role/i, /operator/i, d);
    await user.type(within(d).getByLabelText(/reset password/i), "newpass");
    await press(user, /^save$/i, d);
    await toast(/user updated/i);
    expect(requests("PUT", "/users/u-view")[0].body).toMatchObject({ role: "operator", password: "newpass" });
  });

  it("reports an update failure", async () => {
    const { user } = renderApp("/users", { api: { "PUT /users/:id": reply(400, { error: "cannot demote the last admin" }) } });
    await row("watcher");
    await user.click(screen.getByRole("button", { name: "Edit ops" }));
    await press(user, /^save$/i, await dialog(/edit ops/i));
    await toast(/last admin/i);
  });

  it("deletes a user and reports failures", async () => {
    const { user } = renderApp("/users");
    await row("watcher");
    await user.click(screen.getByRole("button", { name: "Delete watcher" }));
    await press(user, /^delete$/i, await dialog(/delete user/i));
    await toast(/user deleted/i);
    mockApi({ "DELETE /users/:id": reply(400, { error: "cannot delete yourself" }) });
    await user.click(screen.getByRole("button", { name: "Delete ops" }));
    await press(user, /^delete$/i, await dialog(/delete user/i));
    await toast(/cannot delete yourself/i);
  });

  it("shows effective permissions", async () => {
    const { user } = renderApp("/users", {
      api: {
        "GET /users/:id/permissions": {
          userId: "u-op", username: "ops", role: "operator", ceiling: "manage", mode: "scoped", scoped: true,
          teams: [{ teamId: "t-web", teamName: "web", source: "manual" }],
          grants: [{ id: "g-1", scope: "deployment", hostId: "local", target: "d-shop", level: "manage", effectiveLevel: "operate", source: "team", sourceName: "web", hostName: "local", targetName: "shop" }],
          identities: [{ providerId: "p1", provider: "Okta", subject: "abc", email: "ops@example.com", lastLogin: "2026-10-26T10:00:00Z" }],
        },
      },
    });
    await row("watcher");
    await user.click(screen.getByRole("button", { name: "Permissions of ops" }));
    const d = await dialog(/permissions · ops/i);
    expect(await within(d).findByText(/capped by role/i)).toBeInTheDocument();
    expect(within(d).getByText("scoped")).toBeInTheDocument();
    expect(within(d).getByText(/linked identities/i)).toBeInTheDocument();
  });

  it("shows a load error and denies non-admins", async () => {
    renderApp("/users", { api: { "GET /users": reply(500, { error: "boom" }) } });
    expect(await screen.findByText(/failed to load users/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/users", { role: "operator" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("api keys page", () => {
  const created = (name: string) => ({ apiKey: { id: "k-new", name, prefix: "kw_zz", role: "viewer", createdAt: "" }, secret: "kw_zz_full_secret" });

  it("creates a key and shows the secret once", async () => {
    const { user } = renderApp("/api-keys", { api: { "POST /apikeys": (r: { body: { name: string } }) => created(r.body.name) } });
    await row("ci-deploy");
    await press(user, /new api key/i, pageRoot());
    const d = await dialog(/new api key/i);
    await user.type(within(d).getByLabelText(/^name/i), "nightly");
    await press(user, /^create$/i, d);
    const s = await dialog(/api key — nightly/i);
    expect(within(s).getByText("kw_zz_full_secret")).toBeInTheDocument();
    await press(user, /^copy$/i, s);
    expect(await within(s).findByRole("button", { name: /copied/i })).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe("kw_zz_full_secret");
    await press(user, /^done$/i, s);
    expect(requests("POST", "/apikeys")[0].body).toMatchObject({ name: "nightly", role: "operator" });
  });

  it("binds a key to a team", async () => {
    const { user } = renderApp("/api-keys", { api: { "POST /apikeys": created("team-key") } });
    await row("ci-deploy");
    await press(user, /new api key/i, pageRoot());
    const d = await dialog(/new api key/i);
    await user.type(within(d).getByLabelText(/^name/i), "team-key");
    await choose(user, /access/i, /act as a team/i, d);
    await choose(user, /^team/i, /^web$/i, d);
    await press(user, /^create$/i, d);
    await dialog(/api key — team-key/i);
    expect(requests("POST", "/apikeys")[0].body).toMatchObject({ teamId: "t-web" });
  });

  it("limits a key to scopes", async () => {
    const { user } = renderApp("/api-keys", { api: { "POST /apikeys": created("scoped") } });
    await row("ci-deploy");
    await press(user, /new api key/i, pageRoot());
    const d = await dialog(/new api key/i);
    await user.type(within(d).getByLabelText(/^name/i), "scoped");
    await choose(user, /access/i, /only specific scopes/i, d);
    await choose(user, /^scope/i, /label selector/i, d);
    await press(user, /add scope/i, d);
    expect(await within(d).findByText(/use key=value/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/label selector/i), "team=payments");
    await press(user, /add scope/i, d);
    expect(await within(d).findByText(/label team=payments/i)).toBeInTheDocument();
    await press(user, /^create$/i, d);
    await dialog(/api key — scoped/i);
    const body = requests("POST", "/apikeys")[0].body as { scopes: { scope: string; target: string }[] };
    expect(body.scopes[0]).toMatchObject({ scope: "labels", target: "team=payments" });
  });

  it("reports a create failure and deletes keys", async () => {
    const { user } = renderApp("/api-keys", { api: { "POST /apikeys": reply(400, { error: "name already used" }) } });
    await row("ci-deploy");
    await press(user, /new api key/i, pageRoot());
    const d = await dialog(/new api key/i);
    await user.type(within(d).getByLabelText(/^name/i), "ci-deploy");
    await press(user, /^create$/i, d);
    await toast(/name already used/i);
    await press(user, /^cancel$/i, d);
    await user.click(screen.getByRole("button", { name: "Delete web-bot" }));
    await press(user, /^delete$/i, await dialog(/delete api key/i));
    await toast(/api key deleted/i);
    expect(requests("DELETE", "/apikeys/k-team")).toHaveLength(1);
  });

  it("denies non-admins", async () => {
    renderApp("/api-keys", { role: "viewer" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("teams page", () => {
  it("creates a team", async () => {
    const { user } = renderApp("/teams");
    mockApi({ "POST /teams": { id: "t-new", name: "infra", description: "", members: [], grants: [], createdAt: "", updatedAt: "" } });
    await row("web");
    await press(user, /new team/i, pageRoot());
    const d = await dialog(/new team/i);
    expect(within(d).getByRole("button", { name: /^save$/i })).toBeDisabled();
    await user.type(within(d).getByLabelText(/^name/i), " infra ");
    await user.type(within(d).getByLabelText(/description/i), "platform");
    await press(user, /^save$/i, d);
    await waitFor(() => expect(requests("POST", "/teams")[0]?.body).toEqual({ name: "infra", description: "platform" }));
  });

  it("manages members and grants of a team", async () => {
    const { user } = renderApp("/teams");
    await user.click(await row("web"));
    expect(await screen.findByText("web · members")).toBeInTheDocument();
    // Members: remove the manual member, add another (admins are not candidates).
    await user.click(screen.getByRole("button", { name: "Remove ops" }));
    await waitFor(() => expect(requests("DELETE", "/teams/t-web/members/u-op")).toHaveLength(1));
    await choose(user, /add member/i, /watcher/i);
    await press(user, /^add$/i);
    await waitFor(() => expect(requests("POST", "/teams/t-web/members")[0]?.body).toEqual({ userId: "u-view" }));
    // Grants: remove one, add a host-wide read grant.
    await press(user, /remove grant/i);
    await waitFor(() => expect(requests("DELETE", "/grants/g-1")).toHaveLength(1));
    await press(user, /add grant/i);
    const d = await dialog(/add grant to web/i);
    await choose(user, /^scope/i, /one host/i, d);
    await choose(user, /^host/i, /^local$/i, d);
    await choose(user, /^level/i, /^read$/i, d);
    await press(user, /add grant/i, d);
    await waitFor(() => expect(requests("POST", "/grants")[0]?.body).toMatchObject({ scope: "host", hostId: "local", level: "read", subjectType: "team", subjectId: "t-web" }));
  });

  it("shows a grant error inside the dialog", async () => {
    const { user } = renderApp("/teams", { api: { "POST /grants": reply(400, { error: "unknown deployment" }) } });
    await user.click(await row("data"));
    await press(user, /add grant/i);
    const d = await dialog(/add grant to data/i);
    await press(user, /add grant/i, d);
    expect(await within(d).findByText(/pick a host/i)).toBeInTheDocument();
    expect(requests("POST", "/grants")).toHaveLength(0);
    await choose(user, /^host/i, /^local$/i, d);
    await press(user, /add grant/i, d);
    expect(await within(d).findByText(/unknown deployment/i)).toBeInTheDocument();
  });

  it("edits and deletes a team", async () => {
    const { user } = renderApp("/teams");
    mockApi({ "PUT /teams/:id": (r) => ({ ...(r.body as object), id: r.params.id, members: [], grants: [], createdAt: "", updatedAt: "" }) });
    await user.click(await row("data"));
    await press(user, /^edit$/i);
    const d = await dialog(/edit team/i);
    await user.type(within(d).getByLabelText(/^name/i), "-lake");
    await press(user, /^save$/i, d);
    await waitFor(() => expect(requests("PUT", "/teams/t-empty")[0]?.body).toMatchObject({ name: "data-lake" }));
    await press(user, /^delete$/i, pageRoot());
    await press(user, /^delete$/i, await dialog(/delete team/i));
    await waitFor(() => expect(requests("DELETE", "/teams/t-empty")).toHaveLength(1));
  });

  it("reports failures", async () => {
    const { user } = renderApp("/teams", { api: { "DELETE /teams/:id": reply(500, { error: "store locked" }) } });
    await user.click(await row("data"));
    await press(user, /^delete$/i, pageRoot());
    await press(user, /^delete$/i, await dialog(/delete team/i));
    await toast(/store locked/i);
  });

  it("shows the empty state and denies non-admins", async () => {
    renderApp("/teams", { api: { "GET /teams": [] } });
    expect(await screen.findByText(/no teams yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/teams", { role: "operator" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
