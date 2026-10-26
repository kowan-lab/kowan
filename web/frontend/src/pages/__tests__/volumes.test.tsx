import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

async function ready() {
  return within(pageRoot()).findByText("orphan-data", {}, { timeout: 10_000 });
}

describe("volumes page", () => {
  it("creates a local volume with labels", async () => {
    const { user } = renderApp("/volumes");
    mockApi({ "POST /hosts/:h/volumes": (r) => ({ name: (r.body as { name: string }).name, driver: "local" }) });
    await ready();
    await press(user, /create volume/i, pageRoot());
    const d = await dialog(/create volume/i);
    await user.type(within(d).getByLabelText(/^name/i), "cache");
    await press(user, /^create$/i, d);
    await toast(/volume created/i);
    expect(requests("POST", "/hosts/local/volumes")[0].body).toMatchObject({ name: "cache", driver: "local" });
  });

  it("builds NFS driver options", async () => {
    const { user } = renderApp("/volumes");
    mockApi({ "POST /hosts/:h/volumes": { name: "media", driver: "local" } });
    await ready();
    await press(user, /create volume/i, pageRoot());
    const d = await dialog(/create volume/i);
    await choose(user, /provider/i, /^nfs$/i, d);
    await user.type(within(d).getByLabelText(/^name/i), "media");
    await user.type(within(d).getByLabelText(/server/i), "nfs.example.com");
    await user.type(within(d).getByLabelText(/export path/i), "/exports/media");
    await press(user, /^create$/i, d);
    await waitFor(() => expect(requests("POST", "/hosts/local/volumes")).toHaveLength(1));
    const body = requests("POST", "/hosts/local/volumes")[0].body as { options: Record<string, string> };
    expect(body.options.type).toBe("nfs");
    expect(body.options.device).toContain("/exports/media");
  });

  it("validates the name and reports server errors", async () => {
    const { user } = renderApp("/volumes");
    mockApi({ "POST /hosts/:h/volumes": reply(409, { error: "volume already exists" }) });
    await ready();
    await press(user, /create volume/i, pageRoot());
    const d = await dialog(/create volume/i);
    await press(user, /^create$/i, d);
    expect(requests("POST", "/hosts/local/volumes")).toHaveLength(0);
    await user.type(within(d).getByLabelText(/^name/i), "dup");
    await press(user, /^create$/i, d);
    await toast(/already exists/i);
  });

  it("removes a volume after confirmation", async () => {
    const { user } = renderApp("/volumes");
    await ready();
    await user.click(screen.getByRole("button", { name: /remove volume orphan-data/i }));
    const d = await dialog(/remove volume/i);
    await press(user, /^remove$/i, d);
    await toast(/volume removed/i);
    expect(requests("DELETE", "/hosts/local/volumes/orphan-data")).toHaveLength(1);
  });

  it("prunes unused volumes", async () => {
    const { user } = renderApp("/volumes");
    mockApi({ "POST /hosts/:h/prune/:kind": { spaceReclaimed: 1048576 } });
    await ready();
    await press(user, /prune unused/i, pageRoot());
    const d = await dialog(/prune unused volumes/i);
    await press(user, /^prune$/i, d);
    await toast(/reclaimed 1\.0 mb/i);
    expect(requests("POST", "/hosts/local/prune/volumes")).toHaveLength(1);
  });

  it("opens a volume and explores it", async () => {
    const { user } = renderApp("/volumes");
    await ready();
    await user.click(within(pageRoot()).getByText("shop_db"));
    await waitFor(() => expect(currentPath()).toMatch(/^\/volumes\/shop_db/));
  });

  it("shows load errors and the empty state", async () => {
    renderApp("/volumes", { api: { "GET /hosts/:h/volumes": reply(502, { error: "docker daemon unreachable" }) } });
    expect(await within(pageRoot()).findByText(/daemon unreachable/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    renderApp("/volumes", { api: { "GET /hosts/:h/volumes": [] } });
    expect(await within(pageRoot()).findByText(/no volumes on this host/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
