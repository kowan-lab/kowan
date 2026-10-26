// Connections (/credentials), Registries and notification channels: the
// credential-backed admin pages.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const raw = (over: Record<string, unknown>) => ({ id: "new", kind: "connection", type: "github", name: "x", meta: {}, configKeys: [], createdAt: "", updatedAt: "", ...over });

async function row(name: string) {
  return within(pageRoot()).findByText(name, {}, { timeout: 10_000 });
}

describe("credentials (connections) page", () => {
  it("creates a typed credential with its schema fields", async () => {
    const { user } = renderApp("/credentials");
    mockApi({ "POST /credentials": (r) => raw({ name: (r.body as { name: string }).name }) });
    await row("vault-prod");
    await press(user, /new credential/i, pageRoot());
    const d = await dialog(/new credential/i);
    await user.type(within(d).getByLabelText(/^name/i), "gh-bot");
    await choose(user, /^type/i, /^github$/i, d);
    expect(within(d).getByText("gitFetch")).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/personal access token/i), "ghp_x");
    await press(user, /^create$/i, d);
    await toast(/credential created/i);
    expect(requests("POST", "/credentials")[0].body).toMatchObject({
      kind: "connection", type: "github", name: "gh-bot", config: { token: "ghp_x" },
    });
  });

  it("edits keeping blank sensitive values", async () => {
    const { user } = renderApp("/credentials");
    mockApi({ "PUT /credentials/:id": (r) => raw({ id: r.params.id, name: "github" }) });
    await row("vault-prod");
    await user.click(screen.getByRole("button", { name: "Edit github" }));
    const d = await dialog(/edit github/i);
    expect(within(d).getByLabelText(/personal access token/i)).toHaveAttribute("placeholder", "(unchanged)");
    await user.type(within(d).getByLabelText(/username/i), "bot");
    await press(user, /^save$/i, d);
    await toast(/credential updated/i);
    const body = requests("PUT", "/credentials/c-gh")[0].body as { config: Record<string, string> };
    expect(body.config.username).toBe("bot");
    expect(body.config.token ?? "").toBe("");
  });

  it("reports save and delete failures", async () => {
    const { user } = renderApp("/credentials", {
      api: {
        "PUT /credentials/:id": reply(400, { error: "invalid vault address" }),
        "DELETE /credentials/:id": reply(409, { error: "credential is referenced by secret vault-api" }),
      },
    });
    await row("vault-prod");
    await user.click(screen.getByRole("button", { name: "Edit github" }));
    const d = await dialog(/edit github/i);
    await press(user, /^save$/i, d);
    await toast(/invalid vault address/i);
    await press(user, /^cancel$/i, d);
    await user.click(screen.getByRole("button", { name: "Delete vault-prod" }));
    const c = await dialog(/delete credential/i);
    await press(user, /^delete$/i, c);
    await toast(/referenced by secret vault-api/i);
  });

  it("deletes a credential", async () => {
    const { user } = renderApp("/credentials");
    await row("aws-prod");
    await user.click(screen.getByRole("button", { name: "Delete aws-prod" }));
    await press(user, /^delete$/i, await dialog(/delete credential/i));
    await toast(/credential deleted/i);
    expect(requests("DELETE", "/credentials/c-aws")).toHaveLength(1);
  });

  it("shows empty, error and denied states", async () => {
    renderApp("/credentials", { api: { "GET /credentials": [] } });
    expect(await screen.findByText(/no credentials yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/credentials", { api: { "GET /credentials": reply(500, { error: "boom" }) } });
    expect(await screen.findByText(/failed to load credentials/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/credentials", { role: "viewer" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("registries page", () => {
  it("creates a registry backed by a credential", async () => {
    const { user } = renderApp("/registries");
    mockApi({ "POST /credentials": raw({ kind: "registry", type: "ghcr", name: "acme", meta: { url: "ghcr.io" } }) });
    await row("private");
    await press(user, /new registry/i, pageRoot());
    const d = await dialog(/new registry/i);
    await user.type(within(d).getByLabelText(/^name/i), "acme");
    await press(user, /^create$/i, d);
    await toast(/registry host is required/i);
    await user.type(within(d).getByLabelText(/registry host/i), "ghcr.io");
    await choose(user, /credential/i, /ghcr-acme/i, d);
    await press(user, /^create$/i, d);
    await toast(/registry (created|saved|added)/i);
    expect(requests("POST", "/credentials")[0].body).toMatchObject({ kind: "registry", name: "acme", url: "ghcr.io", connectionRef: "c-ghcr" });
  });

  it("tests a registry: verified, insecure, failed and error", async () => {
    const { user } = renderApp("/registries", { api: { "POST /credentials/:id/test": { verified: true, insecure: false } } });
    await row("private");
    await user.click(screen.getByRole("button", { name: "Test ghcr" }));
    await toast(/verified successfully/i);
    mockApi({ "POST /credentials/:id/test": { verified: true, insecure: true } });
    await user.click(screen.getByRole("button", { name: "Test ghcr" }));
    await toast(/reachable but insecure/i);
    mockApi({ "POST /credentials/:id/test": { verified: false, error: "401 unauthorized" } });
    await user.click(screen.getByRole("button", { name: "Test private" }));
    await toast(/401 unauthorized/i);
    mockApi({ "POST /credentials/:id/test": reply(502, { error: "registry timed out" }) });
    await user.click(screen.getByRole("button", { name: "Test private" }));
    await toast(/registry timed out/i);
  });

  it("edits and deletes a registry", async () => {
    const { user } = renderApp("/registries");
    mockApi({ "PUT /credentials/:id": (r) => raw({ id: r.params.id, kind: "registry", name: "ghcr", meta: { url: "ghcr.io", connectionRef: "c-ghcr" } }) });
    await row("private");
    await user.click(screen.getByRole("button", { name: "Edit ghcr" }));
    const d = await dialog(/edit ghcr/i);
    expect(within(d).getByLabelText(/registry host/i)).toHaveValue("ghcr.io");
    await press(user, /^save$/i, d);
    await waitFor(() => expect(requests("PUT", "/credentials/r-ghcr")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Delete private" }));
    await press(user, /^delete$/i, await dialog(/delete registry/i));
    await toast(/registry deleted/i);
  });

  it("opens the explorer for a registry", async () => {
    const { user } = renderApp("/registries");
    await row("private");
    await user.click(screen.getByRole("button", { name: "Explore ghcr" }));
    await waitFor(() => expect(document.location.href).toBeTruthy());
  });

  it("shows the empty state", async () => {
    renderApp("/registries", { api: { "GET /credentials": (r: { query: Record<string, string> }) => (r.query.kind === "registry" ? [] : []) } });
    expect(await screen.findByText(/no registries yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("notification channels page", () => {
  it("creates a channel via a messaging credential", async () => {
    const { user } = renderApp("/channels");
    mockApi({ "POST /credentials": raw({ kind: "channel", type: "webhook", name: "pager" }) });
    await row("ops-hook");
    await press(user, /new channel/i, pageRoot());
    const d = await dialog(/new channel/i);
    await press(user, /^create$/i, d);
    await toast(/name is required/i);
    await user.type(within(d).getByLabelText(/^name/i), "pager");
    await choose(user, /minimum level/i, /critical/i, d);
    await press(user, /^create$/i, d);
    await waitFor(() => expect(requests("POST", "/credentials")).toHaveLength(1));
    expect(requests("POST", "/credentials")[0].body).toMatchObject({ kind: "channel", name: "pager", minLevel: "critical", connectionRef: "c-hook" });
  });

  it("toggles, tests, edits and deletes channels", async () => {
    const { user } = renderApp("/channels", { api: { "POST /credentials/:id/test": { ok: true } } });
    mockApi({ "PUT /credentials/:id": (r) => raw({ id: r.params.id, kind: "channel", name: "ops-hook" }) });
    await row("ops-hook");
    await user.click(screen.getByRole("switch", { name: "Toggle oncall-mail" }));
    await waitFor(() => expect(requests("PUT", "/credentials/ch-mail")[0]?.body).toMatchObject({ enabled: true }));
    await user.click(screen.getByRole("button", { name: "Test ops-hook" }));
    await toast(/test sent/i);
    mockApi({ "POST /credentials/:id/test": reply(502, { error: "smtp refused" }) });
    await user.click(screen.getByRole("button", { name: "Test oncall-mail" }));
    await toast(/smtp refused/i);
    await user.click(screen.getByRole("button", { name: "Edit ops-hook" }));
    const d = await dialog(/edit ops-hook/i);
    await press(user, /^save$/i, d);
    await waitFor(() => expect(requests("PUT", "/credentials/ch-ops")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Delete oncall-mail" }));
    await press(user, /^delete$/i, await dialog(/delete channel/i));
    await toast(/channel deleted/i);
  });

  it("reports a toggle failure", async () => {
    const { user } = renderApp("/channels", { api: { "PUT /credentials/:id": reply(500, { error: "store down" }) } });
    await row("ops-hook");
    await user.click(screen.getByRole("switch", { name: "Toggle ops-hook" }));
    await toast(/store down/i);
  });

  it("shows the empty state", async () => {
    renderApp("/channels", { api: { "GET /credentials": [] } });
    expect(await screen.findByText(/no notification channels yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
