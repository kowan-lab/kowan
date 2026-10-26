import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const ready = () => within(pageRoot()).findByText("shop_default", {}, { timeout: 10_000 });

describe("networks page", () => {
  it("creates a network with IPAM", async () => {
    const { user } = renderApp("/networks", { api: { "POST /hosts/:h/networks": { id: "n-new", name: "front" } } });
    await ready();
    await press(user, /create network/i, pageRoot());
    const d = await dialog(/create network/i);
    await user.type(within(d).getByLabelText(/^name/i), "front");
    await user.type(within(d).getByLabelText(/subnet/i), "10.10.0.0/24");
    await user.type(within(d).getByLabelText(/gateway/i), "10.10.0.1");
    await user.click(within(d).getByLabelText(/internal/i));
    await press(user, /^create$/i, d);
    await toast(/network created/i);
    expect(requests("POST", "/hosts/local/networks")[0].body).toMatchObject({
      name: "front", internal: true, ipam: [{ subnet: "10.10.0.0/24", gateway: "10.10.0.1" }],
    });
  });

  it("connects a running container", async () => {
    const { user } = renderApp("/networks");
    await ready();
    await user.click(screen.getByRole("button", { name: /connect a container to shop_default/i }));
    const d = await dialog(/connect container to shop_default/i);
    await choose(user, /container/i, /shop-db-1/i, d);
    await press(user, /^connect$/i, d);
    await toast(/container connected/i);
    expect(requests("POST", "/hosts/local/networks/n-shop/connect")[0].body).toMatchObject({ containerId: expect.stringMatching(/^c-db/) });
  });

  it("removes and prunes", async () => {
    const { user } = renderApp("/networks", { api: {
      "DELETE /hosts/:h/networks/:id": reply(409, { error: "network has active endpoints" }),
      "POST /hosts/:h/prune/:kind": { spaceReclaimed: 0 },
    } });
    await ready();
    await user.click(screen.getByRole("button", { name: /remove shop_default/i }));
    const d = await dialog(/remove network/i);
    await press(user, /^remove$/i, d);
    await toast(/active endpoints/i);
    await press(user, /cancel/i, d);
    await press(user, /prune unused/i, pageRoot());
    await press(user, /^prune$/i, await dialog(/prune unused networks/i));
    await toast(/pruned/i);
    expect(requests("POST", "/hosts/local/prune/networks")).toHaveLength(1);
  });

  it("opens a network's detail page", async () => {
    const { user } = renderApp("/networks", { api: {
      "GET /hosts/:h/networks/:id": { id: "n-shop", name: "shop_default", driver: "bridge", scope: "local", containers: [] },
    } });
    await ready();
    await user.click(within(pageRoot()).getByText("shop_default"));
    await waitFor(() => expect(currentPath()).toMatch(/^\/networks\/n-shop/));
  });
});

describe("networks page: options and failures", () => {
  it("validates, sends driver options and labels, and reports a create failure", async () => {
    const { user } = renderApp("/networks", { api: { "POST /hosts/:h/networks": reply(409, { error: "network with name front already exists" }) } });
    await ready();
    await press(user, /create network/i, pageRoot());
    const d = await dialog(/create network/i);
    await press(user, /^create$/i, d);
    expect(await within(d).findByText(/name is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/^name/i), "front");
    await user.type(within(d).getByLabelText(/ip range/i), "10.10.0.128/25");
    await user.click(within(d).getByRole("switch", { name: /attachable/i }));
    const adds = within(d).getAllByRole("button", { name: /^add/i });
    await user.click(adds[0]);
    await user.click(adds[1]);
    const keys = within(d).getAllByPlaceholderText("key");
    const values = within(d).getAllByPlaceholderText("value");
    await user.type(keys[0], "com.docker.network.bridge.name");
    await user.type(values[0], "br-front");
    await user.type(keys[1], "team");
    await user.type(values[1], "web");
    await user.click(within(d).getByRole("button", { name: "Remove label" }));
    await user.click(adds[1]);
    await user.type(within(d).getAllByPlaceholderText("key")[1], "env");
    await user.type(within(d).getAllByPlaceholderText("value")[1], "prod");
    await press(user, /^create$/i, d);
    await toast(/already exists/i);
    expect(requests("POST", "/hosts/local/networks")[0].body).toMatchObject({
      name: "front", attachable: true, options: { "com.docker.network.bridge.name": "br-front" }, labels: { env: "prod" },
    });
    await user.click(within(d).getByRole("button", { name: "Remove option" }));
  });

  it("reports connect and prune failures", async () => {
    const { user } = renderApp("/networks", {
      api: { "POST /hosts/:h/networks/:id/connect": reply(409, { error: "already connected" }), "POST /hosts/:h/prune/:kind": reply(500, { error: "prune failed" }) },
    });
    await ready();
    await user.click(screen.getByRole("button", { name: /connect a container to shop_default/i }));
    const d = await dialog(/connect container to shop_default/i);
    await choose(user, /container/i, /shop-web-1/i, d);
    await press(user, /^connect$/i, d);
    await toast(/already connected/i);
    await press(user, /cancel/i, d);
    await press(user, /prune unused/i, pageRoot());
    await press(user, /^prune$/i, await dialog(/prune unused networks/i));
    await toast(/prune failed/i);
  });

  it("lists networks of every host in the all-hosts view", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/networks");
    expect(await within(pageRoot()).findAllByText("shop_default", {}, { timeout: 10_000 })).not.toHaveLength(0);
    expect(screen.getAllByText("local").length).toBeGreaterThan(0);
  });
});
