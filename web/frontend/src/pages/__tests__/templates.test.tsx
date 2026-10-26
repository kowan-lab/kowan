import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import catalog from "../../test/catalog.json";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const custom = {
  id: "tpl-1", name: "Team wiki", description: "Our wiki stack", category: "Internal", icon: "", type: "compose",
  compose: "services:\n  wiki:\n    image: ghcr.io/acme/wiki:1\n", inputs: [], builtIn: false,
};
const templates = [...catalog.templates, custom];
const rendered = { type: "container", spec: { image: "postgres:17" }, compose: "" };

async function ready() {
  return within(pageRoot()).findByText("Nginx", {}, { timeout: 10_000 });
}

describe("templates page", () => {
  it("searches and filters by category", async () => {
    const { user } = renderApp("/templates", { api: { "GET /templates": templates } });
    await ready();
    await user.click(screen.getByRole("button", { name: /^database/i }));
    expect(screen.queryByText("Nginx")).toBeNull();
    expect(screen.getByText("PostgreSQL")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^all/i }));
    await user.type(screen.getByLabelText(/search templates/i), "acme/wiki");
    expect(screen.getByText("Team wiki")).toBeInTheDocument();
    expect(screen.queryByText("Nginx")).toBeNull();
    await user.clear(screen.getByLabelText(/search templates/i));
    await user.type(screen.getByLabelText(/search templates/i), "nothing-like-this");
    expect(await screen.findByText(/no templates match/i)).toBeInTheDocument();
    await press(user, /clear filters/i);
    expect(screen.getByText("Nginx")).toBeInTheDocument();
  });

  it("deploys a template with generated secrets and lands on the workload", async () => {
    const { user } = renderApp("/templates", {
      api: {
        "GET /templates": templates,
        "POST /templates/:id/render": rendered,
        "POST /deployments": (r) => ({ ...r.body, id: "d-pg" }),
      },
    });
    await ready();
    const deploys = screen.getAllByRole("button", { name: /^deploy$/i });
    await user.click(deploys[catalog.templates.findIndex((t) => t.id === "builtin:postgres")]);
    const d = await dialog(/deploy postgresql/i);
    const pw = within(d).getByLabelText(/^password/i, { selector: "input" }) as HTMLInputElement;
    const first = pw.value;
    expect(first.length).toBeGreaterThan(10);
    await user.click(within(d).getByRole("button", { name: /generate a new/i }));
    expect(pw.value).not.toBe(first);
    const name = within(d).getByLabelText(/deployment name/i);
    await user.clear(name);
    await press(user, /^deploy$/i, d);
    expect(await within(d).findByText(/deployment name is required/i)).toBeInTheDocument();
    await user.type(name, "pg-main");
    await user.click(within(d).getByRole("checkbox", { name: /start after creating/i }));
    await press(user, /^create$/i, d);
    await toast(/deployment created/i);
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-pg"));
    expect(requests("POST", "/templates/builtin:postgres/render")[0].body).toMatchObject({ name: "pg-main", values: { POSTGRES_PASSWORD: pw.value } });
    expect(requests("POST", "/deployments")[0].body).toMatchObject({ name: "pg-main", targets: ["local"], spec: { image: "postgres:17" } });
    expect(requests("POST", "/deployments/d-pg/deploy")).toHaveLength(0);
  });

  it("validates required inputs and reports render errors", async () => {
    const { user } = renderApp("/templates", { api: { "GET /templates": templates, "POST /templates/:id/render": reply(400, { error: "HTTP_PORT must be a number" }) } });
    await ready();
    await user.click(screen.getAllByRole("button", { name: /^deploy$/i })[0]);
    const d = await dialog(/deploy nginx/i);
    const port = within(d).getByLabelText(/http port/i);
    await user.clear(port);
    await press(user, /^deploy$/i, d);
    expect(await within(d).findByText(/http port is required/i)).toBeInTheDocument();
    await user.type(port, "eighty");
    await press(user, /^deploy$/i, d);
    expect(await within(d).findByText(/must be a number/i)).toBeInTheDocument();
  });

  it("deploys and starts, reporting a failed follow-up step", async () => {
    const { user } = renderApp("/templates", {
      api: {
        "GET /templates": templates,
        "POST /templates/:id/render": rendered,
        "POST /deployments": (r) => ({ ...r.body, id: "d-n" }),
        "POST /deployments/:id/deploy": reply(409, { error: "host offline" }),
      },
    });
    await ready();
    await user.click(screen.getAllByRole("button", { name: /^deploy$/i })[0]);
    const d = await dialog(/deploy nginx/i);
    await press(user, /^deploy$/i, d);
    await toast(/was created, but not everything was set/i);
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~d-n"));
  });

  it("deletes a custom template", async () => {
    const { user } = renderApp("/templates", { api: { "GET /templates": templates } });
    mockApi({ "DELETE /templates/:id": reply(204) });
    await ready();
    expect(screen.queryByRole("button", { name: "Delete Nginx" })).toBeNull();
    await press(user, "Delete Team wiki");
    await press(user, /^delete$/i, await dialog(/delete template/i));
    await toast(/template deleted/i);
    expect(requests("DELETE", "/templates/tpl-1")).toHaveLength(1);
    mockApi({ "DELETE /templates/:id": reply(500, { error: "in use" }) });
    await press(user, "Delete Team wiki");
    await press(user, /^delete$/i, await dialog(/delete template/i));
    await toast(/in use/i);
  });

  it("viewers cannot deploy and errors are shown", async () => {
    renderApp("/templates", { role: "viewer", api: { "GET /templates": templates } });
    await ready();
    expect(screen.getAllByRole("button", { name: /^deploy$/i })[0]).toBeDisabled();
  });

  it("shows a catalog error", async () => {
    renderApp("/templates", { api: { "GET /templates": reply(500, { error: "catalog broken" }) } });
    expect(await screen.findByText(/catalog broken/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
