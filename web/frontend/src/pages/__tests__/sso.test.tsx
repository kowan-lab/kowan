import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const okta = {
  id: "p-okta", name: "okta", type: "oidc", label: "Okta", icon: "", issuer: "https://acme.okta.com", clientId: "cid",
  clientSecret: "********", scopes: "", enabled: true, managed: "", redirectUri: "", createdAt: "", updatedAt: "",
  config: { defaultRole: "viewer", roleMappings: [{ claim: "groups", value: "admins", role: "admin" }], teamMappings: [], allowSignup: true, linkByEmail: true },
};
const gh = { ...okta, id: "p-gh", name: "github", type: "github", label: "", issuer: "", enabled: false, managed: "env", config: {} };
const listing = (over: Record<string, unknown> = {}) => ({
  providers: [okta, gh], externalUrl: "https://kowan.example.com", settings: { ssoOnly: false, breakGlassUser: "root" }, ...over,
});

async function ready() {
  return within(pageRoot()).findByText("Okta", {}, { timeout: 10_000 });
}

describe("sso providers page", () => {
  it("creates a provider from a preset with mappings", async () => {
    const { user } = renderApp("/sso", { api: { "GET /sso/providers": listing() } });
    mockApi({ "POST /sso/providers": (r) => ({ ...okta, ...(r.body as object), id: "p-new" }) });
    await ready();
    await press(user, /new provider/i, pageRoot());
    const d = await dialog(/new identity provider/i);
    await choose(user, /^provider/i, /authelia/i, d);
    expect(within(d).getByLabelText(/scopes/i)).toHaveValue("openid profile email groups");
    expect(within(d).getByText(/kowan.example.com\/api\/v1\/auth\/sso\/<provider id>\/callback/)).toBeInTheDocument();
    const issuer = within(d).getByLabelText(/issuer url/i);
    await user.clear(issuer);
    await user.type(issuer, "https://auth.example.com");
    await user.type(within(d).getByLabelText(/client id/i), "kowan");
    await user.type(within(d).getByLabelText(/client secret/i), "s3cret");
    await press(user, /add role mapping/i, d);
    await user.type(within(d).getAllByLabelText("Value")[0], "ops");
    await choose(user, "Role", /operator/i, d);
    await press(user, /add team mapping/i, d);
    await user.type(within(d).getAllByLabelText("Value")[1], "web-team");
    await choose(user, "Team", /^web$/i, d);
    await press(user, /^save$/i, d);
    await toast(/provider saved/i);
    expect(requests("POST", "/sso/providers")[0].body).toMatchObject({
      name: "authelia", type: "oidc", issuer: "https://auth.example.com", clientId: "kowan", clientSecret: "s3cret",
      config: { roleMappings: [{ claim: "groups", value: "ops", role: "operator" }], teamMappings: [{ claim: "groups", value: "web-team", team: "t-web" }] },
    });
  });

  it("a github preset hides the issuer and save errors stay in the dialog", async () => {
    const { user } = renderApp("/sso", { api: { "GET /sso/providers": listing(), "POST /sso/providers": reply(400, { error: "clientSecret is required for GitHub" }) } });
    await ready();
    await press(user, /new provider/i, pageRoot());
    const d = await dialog(/new identity provider/i);
    await choose(user, /^provider/i, /^github$/i, d);
    expect(within(d).queryByLabelText(/issuer url/i)).toBeNull();
    await press(user, /^save$/i, d);
    expect(await within(d).findByText(/clientSecret is required/i)).toBeInTheDocument();
  });

  it("edits without resending the stored secret, even after focusing it", async () => {
    const { user } = renderApp("/sso", { api: { "GET /sso/providers": listing() } });
    mockApi({ "PUT /sso/providers/:id": (r) => ({ ...okta, ...(r.body as object) }) });
    await ready();
    await user.click(screen.getByRole("button", { name: "Edit okta" }));
    const d = await dialog(/edit okta/i);
    const secret = within(d).getByLabelText(/client secret/i);
    await user.click(secret);
    expect(secret).toHaveValue("");
    await user.click(within(d).getByLabelText(/client id/i));
    expect(secret).toHaveValue("********");
    await press(user, /remove mapping/i, d);
    await press(user, /^save$/i, d);
    await toast(/provider saved/i);
    const body = requests("PUT", "/sso/providers/p-okta")[0].body as Record<string, unknown>;
    expect(body).not.toHaveProperty("clientSecret");
    expect(body).toMatchObject({ config: { roleMappings: [] } });
  });

  it("tests and deletes providers", async () => {
    const { user } = renderApp("/sso", { api: { "GET /sso/providers": listing(), "POST /sso/providers/:id/test": { ok: true, detail: "discovery ok", redirectUri: "" } } });
    await ready();
    const okRow = screen.getByText("Okta").closest("tr")!;
    await press(user, /^test$/i, okRow as HTMLElement);
    await toast(/discovery ok/i);
    mockApi({ "POST /sso/providers/:id/test": { ok: false, detail: "issuer mismatch", redirectUri: "" } });
    await press(user, /^test$/i, okRow as HTMLElement);
    await toast(/issuer mismatch/i);
    mockApi({ "POST /sso/providers/:id/test": reply(502, { error: "dns failure" }) });
    await press(user, /^test$/i, okRow as HTMLElement);
    await toast(/dns failure/i);
    await user.click(screen.getByRole("button", { name: "Delete github" }));
    await press(user, /^delete$/i, await dialog(/delete identity provider/i));
    await waitFor(() => expect(requests("DELETE", "/sso/providers/p-gh")).toHaveLength(1));
    mockApi({ "DELETE /sso/providers/:id": reply(409, { error: "provider is managed by env" }) });
    await user.click(screen.getByRole("button", { name: "Delete okta" }));
    await press(user, /^delete$/i, await dialog(/delete identity provider/i));
    await toast(/managed by env/i);
  });

  it("updates password sign-in settings", async () => {
    const { user } = renderApp("/sso", { api: { "GET /sso/providers": listing() } });
    await ready();
    await user.click(screen.getByRole("switch", { name: /sso only/i }));
    await waitFor(() => expect(requests("PUT", "/sso/settings")[0]?.body).toEqual({ ssoOnly: true }));
    mockApi({ "PUT /sso/settings": reply(400, { error: "pick a break-glass admin first" }) });
    await user.click(screen.getByRole("switch", { name: /sso only/i }));
    await toast(/break-glass admin first/i);
  });

  it("warns when the external URL is unset and shows the empty state", async () => {
    renderApp("/sso", { api: { "GET /sso/providers": listing({ providers: [], externalUrl: "" }) } });
    expect(await screen.findByText(/KOWAN_EXTERNAL_URL is not set/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(await screen.findByText(/no identity providers/i)).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/sso", { role: "operator" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
