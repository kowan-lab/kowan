import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const settings = {
  enabled: false, every: "weekly", weekday: 0, hour: 8, channels: [] as string[],
  sections: { updates: true, cves: true, backups: true, certificates: true, alerts: true }, sendEmpty: false,
};

async function card(extra: Record<string, unknown> = {}) {
  const r = renderApp("/channels", { api: { "GET /digest": { settings, last: null }, ...extra } as never });
  const el = (await screen.findByRole("heading", { name: "Digest" }, { timeout: 10_000 })).closest("section")!;
  return { ...r, el };
}

describe("digest card", () => {
  it("saves when and what to send, to which channels", async () => {
    const { user, el } = await card({ "PUT /digest": (r: { body: unknown }) => r.body });
    expect(within(el).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(within(el).getByText("No digest sent yet.")).toBeInTheDocument();
    // Only channels that are on can be picked (oncall-mail is off).
    expect(within(el).getByRole("checkbox", { name: "ops-hook" })).toBeInTheDocument();
    expect(within(el).queryByRole("checkbox", { name: "oncall-mail" })).toBeNull();
    await user.click(within(el).getByRole("switch", { name: "Send the digest" }));
    await choose(user, "On", "Friday", el);
    await choose(user, "At (UTC)", "17:00", el);
    await user.click(within(el).getByRole("checkbox", { name: /container updates waiting/i }));
    await user.click(within(el).getByRole("checkbox", { name: /even when there is nothing/i }));
    await user.click(within(el).getByRole("checkbox", { name: "ops-hook" }));
    expect(within(el).getByText("Only the ones picked:")).toBeInTheDocument();
    await press(user, "Save", el);
    await toast(/digest scheduled/i);
    expect(requests("PUT", "/digest")[0].body).toEqual({
      ...settings, enabled: true, weekday: 4, hour: 17, channels: ["ch-ops"], sendEmpty: true,
      sections: { ...settings.sections, updates: false },
    });
    await choose(user, "Every", "Day", el);
    expect(within(el).queryByRole("combobox", { name: "On" })).toBeNull();
    await user.click(within(el).getByRole("checkbox", { name: "ops-hook" }));
    expect(within(el).getByText(/every channel that is on/i)).toBeInTheDocument();
  });

  it("previews the text and sends now", async () => {
    let send = 0;
    const { user, el } = await card({
      "GET /digest/preview": { since: "2026-10-19T08:00:00Z", until: "2026-10-26T08:00:00Z", sections: {}, empty: false,
                               title: "Kowan digest: 2 updates", text: "Updates waiting (2)\n  • web on local", level: "info" },
      "POST /digest/send": () => [
        { at: "x", sent: ["ops-hook"], failed: [], skipped: false },
        { at: "x", sent: [], failed: ["ops-hook: down"], skipped: false },
        { at: "x", sent: [], failed: [], skipped: false },
      ][send++] ?? reply(500, { error: "boom" }),
    });
    await press(user, "Preview", el);
    const dlg = await dialog("Digest preview");
    expect(await within(dlg).findByText("Kowan digest: 2 updates")).toBeInTheDocument();
    expect(within(dlg).getByText(/updates waiting \(2\)/i)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await press(user, "Send now", el);
    await toast(/to ops-hook/i);
    await press(user, "Send now", el);
    await toast(/failed: ops-hook: down/i);
    await press(user, "Send now", el);
    await toast(/nobody to send it to/i);
    await press(user, "Send now", el);
    await toast(/^boom$/i);
  });

  it("shows the last send and reports errors", async () => {
    const { user, el } = await card({
      "GET /digest": { settings: { ...settings, enabled: true }, last: { at: "2026-10-26T08:00:00Z", sent: ["ops-hook"], failed: ["mail: x"], skipped: false } },
      "PUT /digest": reply(400, { error: "channels must be ids of existing channels" }),
      "GET /digest/preview": reply(500, { error: "preview broke" }),
    });
    expect(within(el).getByText(/sent to ops-hook; failed: mail: x/)).toBeInTheDocument();
    await user.click(within(el).getByRole("switch", { name: "Send the digest" }));
    await press(user, "Save", el);
    await toast(/channels must be ids/i);
    await press(user, "Preview", el);
    expect(await within(await dialog("Digest preview")).findByText("preview broke")).toBeInTheDocument();
  });

  it("says when the last digest was skipped and when it cannot load", async () => {
    await card({ "GET /digest": { settings, last: { at: "2026-10-26T08:00:00Z", sent: [], failed: [], skipped: true } } });
    expect(screen.getByText(/nothing to report, not sent/i)).toBeInTheDocument();
  });

  it("reports a digest it cannot load", async () => {
    renderApp("/channels", { api: { "GET /digest": reply(500, { error: "db locked" }) } });
    expect(await screen.findByText("db locked", {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});
