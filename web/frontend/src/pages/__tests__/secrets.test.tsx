import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

async function ready() {
  return within(pageRoot()).findByText("db-password", {}, { timeout: 10_000 });
}

describe("secrets page", () => {
  it("creates a static secret with fields", async () => {
    const { user } = renderApp("/secrets");
    mockApi({ "POST /credentials": (r) => ({ id: "s-new", kind: "secret", type: "generic", name: (r.body as { name: string }).name, meta: {}, configKeys: ["value"], createdAt: "", updatedAt: "" }) });
    await ready();
    await press(user, /new secret/i, pageRoot());
    const d = await dialog(/secret/i);
    await press(user, /^create$/i, d);
    await toast(/name is required/i);
    await user.type(within(d).getByLabelText(/^name/i), "api-token");
    await user.type(within(d).getByLabelText(/description/i), "for the api");
    await user.type(within(d).getByPlaceholderText("field"), "value");
    await user.type(within(d).getByPlaceholderText("value"), "s3cr3t");
    await press(user, /^create$/i, d);
    await toast(/secret created/i);
    expect(requests("POST", "/credentials")[0].body).toMatchObject({
      kind: "secret", name: "api-token", type: "generic", description: "for the api", fields: { value: "s3cr3t" },
    });
  });

  it("creates a connection-backed secret with selector fields only", async () => {
    const { user } = renderApp("/secrets");
    mockApi({ "POST /credentials": { id: "s2", kind: "secret", type: "vault", name: "db", meta: { connectionRef: "c-vault" }, configKeys: [], createdAt: "", updatedAt: "" } });
    await ready();
    await press(user, /new secret/i, pageRoot());
    const d = await dialog(/secret/i);
    await user.type(within(d).getByLabelText(/^name/i), "db");
    await choose(user, /source/i, /from a connection/i, d);
    // The only secret-backend connection is preselected.
    expect(await within(d).findByLabelText(/secret path/i)).toBeInTheDocument();
    // Credentials and what the connection already stores are not asked again.
    expect(within(d).queryByLabelText(/vault token/i)).toBeNull();
    expect(within(d).queryByLabelText(/vault address/i)).toBeNull();
    await user.type(within(d).getByLabelText(/secret path/i), "app/db");
    await press(user, /^create$/i, d);
    expect(requests("POST", "/credentials")).toHaveLength(0); // Key is still required
    await user.type(within(d).getByLabelText(/^key/i), "password");
    await press(user, /^create$/i, d);
    await toast(/secret created/i);
    const body = requests("POST", "/credentials")[0].body as Record<string, unknown>;
    expect(body).toMatchObject({ connectionRef: "c-vault", type: "vault", fields: { path: "app/db", key: "password" } });
  });

  it("edits a secret keeping blank values", async () => {
    const { user } = renderApp("/secrets");
    mockApi({ "PUT /credentials/:id": (r) => ({ id: r.params.id, kind: "secret", type: "generic", name: "db-password", meta: {}, configKeys: ["value"], createdAt: "", updatedAt: "" }) });
    await ready();
    await user.click(screen.getByRole("button", { name: "Edit db-password" }));
    const d = await dialog(/edit db-password/i);
    expect(within(d).getByLabelText(/^name/i)).toHaveValue("db-password");
    const desc = within(d).getByLabelText(/description/i);
    await user.clear(desc);
    await user.type(desc, "rotated");
    await press(user, /^save$/i, d);
    await toast(/secret updated/i);
    const body = requests("PUT", "/credentials/s-db")[0].body as Record<string, unknown>;
    expect(body.description).toBe("rotated");
  });

  it("reveals decrypted values", async () => {
    const { user } = renderApp("/secrets", { api: { "POST /credentials/:id/reveal": { fields: { value: "hunter2" } } } });
    await ready();
    await user.click(screen.getByRole("button", { name: "Reveal db-password" }));
    const d = await dialog(/reveal/i);
    expect(await within(d).findByText("value")).toBeInTheDocument();
    expect(requests("POST", "/credentials/s-db/reveal")).toHaveLength(1);
    await press(user, /^close$/i, d);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("reports a reveal failure", async () => {
    const { user } = renderApp("/secrets", { api: { "POST /credentials/:id/reveal": reply(502, { error: "vault unreachable" }) } });
    await ready();
    await user.click(screen.getByRole("button", { name: "Reveal vault-api" }));
    await toast(/vault unreachable/i);
  });

  it("deletes after confirmation and reports failures", async () => {
    const { user } = renderApp("/secrets");
    await ready();
    await user.click(screen.getByRole("button", { name: "Delete db-password" }));
    let d = await dialog(/delete secret/i);
    await press(user, /^delete$/i, d);
    await toast(/secret deleted/i);
    expect(requests("DELETE", "/credentials/s-db")).toHaveLength(1);

    mockApi({ "DELETE /credentials/:id": reply(409, { error: "secret is in use by deployment shop" }) });
    await user.click(screen.getByRole("button", { name: "Delete vault-api" }));
    d = await dialog(/delete secret/i);
    await press(user, /^delete$/i, d);
    await toast(/in use by deployment shop/i);
  });

  it("previews references live", async () => {
    const { user } = renderApp("/secrets", {
      api: {
        "POST /credentials/resolve": {
          references: [
            { token: "[[db-password]]", source: "", secretType: "generic", status: "resolved", masked: "hu•••", length: 7, external: false },
            { token: "[[missing]]", source: "", status: "error", message: "no secret named missing" },
          ],
        },
      },
    });
    await ready();
    await user.type(screen.getByPlaceholderText(/paste text with references/i), "A=[[[[db-password]] B=[[[[missing]]");
    expect(await screen.findByText("hu•••", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByText("no secret named missing")).toBeInTheDocument();
    expect(screen.getByText("7 chars")).toBeInTheDocument();
  });

  it("shows load errors and the empty state", async () => {
    renderApp("/secrets", { api: { "GET /credentials": reply(500, { error: "db locked" }) } });
    expect(await screen.findByText(/failed to load secrets/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    renderApp("/secrets", { api: { "GET /credentials": [] } });
    expect(await screen.findByText(/no secrets yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies non-admins", async () => {
    renderApp("/secrets", { role: "operator" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
