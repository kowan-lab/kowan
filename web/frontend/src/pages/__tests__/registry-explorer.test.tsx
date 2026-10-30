import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { press } from "../../test/ui";

const api = {
  "GET /credentials/:id/catalog": { repositories: ["acme/api", "acme/web", "tools/ci"] },
  "GET /credentials/:id/tags": (r: { query: Record<string, string> }) => ({ tags: r.query.repo === "acme/web" ? ["1.0", "1.1", "latest"] : [] }),
};

describe("registry explorer", () => {
  it("lists repositories, filters and shows tags with pull references", async () => {
    const { user } = renderApp("/registry-explorer?id=r-priv", { api });
    expect(await within(pageRoot()).findByText("acme/web", {}, { timeout: 10_000 })).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/filter repositories/i), "acme");
    expect(screen.queryByText("tools/ci")).toBeNull();
    await user.click(screen.getByText("acme/web"));
    expect(await screen.findByText("3 tags")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Copy pull reference" })[0]);
    expect(await navigator.clipboard.readText()).toBe("registry.example.com/acme/web:1.0");
    await press(user, "Refresh tags");
    await press(user, "Refresh catalog");
    await user.click(screen.getByText("acme/api"));
    expect(await screen.findByText(/no tags in this repository/i)).toBeInTheDocument();
  });

  it("falls back to a typed repository when there is no catalog", async () => {
    const { user } = renderApp("/registry-explorer?id=r-priv", {
      api: { ...api, "GET /credentials/:id/catalog": reply(404, { error: "catalog not supported" }) },
    });
    const input = await screen.findByPlaceholderText("library/nginx", {}, { timeout: 10_000 });
    expect(screen.getByText(/does not expose a repository catalog/i)).toBeInTheDocument();
    await user.type(input, "acme/web");
    await user.click(screen.getByRole("button", { name: /browse tags|add|open/i }));
    expect(await screen.findByText("3 tags")).toBeInTheDocument();
    expect(requests("GET", "/credentials/r-priv/tags")[0].query).toEqual({ repo: "acme/web" });
  });

  it("reports tag failures", async () => {
    const { user } = renderApp("/registry-explorer?id=r-priv", { api: { ...api, "GET /credentials/:id/tags": reply(401, { error: "unauthorized" }) } });
    await user.click(await screen.findByText("acme/web", {}, { timeout: 10_000 }));
    expect(await screen.findByText(/unauthorized/i)).toBeInTheDocument();
  });

  it("picks a registry and handles none or a load error", async () => {
    const { user } = renderApp("/registry-explorer", { api });
    await waitFor(() => expect(screen.getByText(/pick a registry/i)).toBeInTheDocument(), { timeout: 10_000 });
    await user.click(within(pageRoot()).getByRole("combobox"));
    await user.click(await screen.findByRole("option", { name: "ghcr (ghcr.io)" }));
    await waitFor(() => expect(currentPath()).toBe("/registry-explorer?id=r-ghcr"));
  });

  it("explains an empty registry list", async () => {
    renderApp("/registry-explorer", { api: { "GET /credentials": [] } });
    expect(await screen.findByText(/no registries configured/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText(/settings → registries/i)).toBeInTheDocument();
  });

  it("shows a load error and denies viewers", async () => {
    renderApp("/registry-explorer", { api: { "GET /credentials": reply(500, { error: "x" }) } });
    expect(await screen.findByText(/failed to load registries/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("denies viewers", async () => {
    renderApp("/registry-explorer", { role: "viewer" });
    expect(await screen.findByText(/access denied/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
