import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import * as fx from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const tagged = [{ ...fx.hosts[0], tags: ["production"] }, { ...fx.hosts[1], tags: ["customer-x", "production"] }];

describe("host groups", () => {
  it("shows and edits a host's groups", async () => {
    const { user } = renderApp("/hosts", { api: {
      "GET /hosts": [tagged[0], { ...tagged[1], tags: ["customer-x"] }],
      "PATCH /hosts/:id": (r: { body: { tags: string[] } }) => ({ ...tagged[1], tags: r.body.tags }),
    } });
    await user.click(await screen.findByRole("button", { name: "Groups of edge-1" }, { timeout: 10_000 }));
    const d = await dialog(/groups of edge-1/i);
    await press(user, "production", d); // a group in use elsewhere, one click
    await user.type(within(d).getByLabelText(/^groups/i), "Bad Tag{Enter}");
    await press(user, /^save$/i, d);
    expect(await within(d).findByText(/"bad tag": use lowercase/i)).toBeInTheDocument();
    await user.click(within(d).getByLabelText(/^groups/i));
    await user.keyboard("{Backspace}");
    await press(user, /^save$/i, d);
    await toast(/groups saved/i);
    expect(requests("PATCH", "/hosts/h-edge")[0].body).toEqual({ tags: ["customer-x", "production"] });
  });

  it("sets a group's auto-update default", async () => {
    const { user } = renderApp("/auto-update", { api: {
      "GET /autoupdate/groups": [{ tag: "production", hosts: ["local", "h-edge"], stored: null, effective: { mode: "off" } }],
    } });
    expect(await screen.findByText("production", {}, { timeout: 10_000 })).toBeInTheDocument();
    await choose(user, /auto-update mode of group production/i, /^notify$/i);
    await waitFor(() => expect(requests("PUT", "/autoupdate/groups/production")[0].body).toEqual({ mode: "notify" }));
  });

  it("schedules a group and a runbook", async () => {
    const { user } = renderApp("/schedules", { api: { "GET /hosts": tagged, "GET /runbooks": [{ id: "rb-1", name: "Vacuum" }] } });
    await user.click((await screen.findAllByRole("button", { name: /new schedule/i }, { timeout: 10_000 }))[0]);
    let d = await dialog(/new schedule/i);
    await choose(user, /target type/i, /host group/i, d);
    expect(within(d).queryByRole("combobox", { name: /^host$/i })).toBeNull();
    await choose(user, /^target$/i, /^production$/i, d);
    await press(user, /create schedule/i, d);
    await toast(/schedule saved/i);
    expect(requests("POST", "/schedules")[0].body).toMatchObject({ targetType: "group", targetId: "production", action: "stop", hostId: "" });
    await user.click(screen.getAllByRole("button", { name: /new schedule/i })[0]);
    d = await dialog(/new schedule/i);
    await choose(user, /target type/i, /^runbook$/i, d);
    await choose(user, /^target$/i, /vacuum/i, d);
    await press(user, /create schedule/i, d);
    await toast(/schedule saved/i);
    expect(requests("POST", "/schedules")[1].body).toMatchObject({ targetType: "runbook", targetId: "rb-1", action: "run" });
  });
});
