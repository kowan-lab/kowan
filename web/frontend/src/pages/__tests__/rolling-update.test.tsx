import { useState } from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, requests } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { dialog, press } from "../../test/ui";
import RollingUpdateDialog, { runProgress } from "../../components/updates/RollingUpdateDialog";
import type { Host, PlatformUpdate } from "../../types";

const NOW = new Date().toISOString();
const agent = (id: string, name: string): Host =>
  ({ id, name, type: "agent", mode: "agent", address: "10.0.0.2:9001", online: true, lastSeen: NOW, createdAt: NOW }) as Host;
const run = (over: Partial<PlatformUpdate>): PlatformUpdate =>
  ({ id: "u", component: "agent", hostId: "h-a", mode: "update", status: "running", fromImage: "", fromDigest: "", fromVersion: "", toImage: "", toDigest: "", toVersion: "", phases: [], error: "", logs: [], startedAt: NOW, finishedAt: "", startedBy: "admin", ...over }) as PlatformUpdate;

/** The dialog with a button that takes agent-a offline, as a restart does. */
function Harness() {
  const [hosts, setHosts] = useState([agent("h-a", "agent-a"), agent("h-b", "agent-b")]);
  return (
    <>
      <button type="button" onClick={() => setHosts((hs) => hs.map((h) => (h.id === "h-a" ? { ...h, online: false } : h)))}>restart a</button>
      <RollingUpdateDialog open onOpenChange={() => {}} hosts={hosts} />
    </>
  );
}

describe("agent update dialog", () => {
  it("updates agents together and keeps a restarting agent listed with its progress", async () => {
    mockApi({
      "GET /hosts/:id/agent/update": { supported: true, updateAvailable: true, image: "kowan-agent:1" },
      "POST /agents/update": { batchId: "b1", ids: ["u-a", "u-b"] },
      "GET /system/updates/:id": (r) => run({ id: r.params.id, phases: [{ phase: "queued", ok: true, at: NOW }, { phase: "pull", ok: true, at: NOW }] }),
    });
    const { user } = renderWithProviders(<Harness />);
    const d = await dialog(/update agents/i);
    expect(within(d).getByText(/at the same time/i)).toBeInTheDocument();
    await waitFor(() => expect(within(d).getByRole("button", { name: /update 2 agents/i })).toBeEnabled());
    await press(user, /update 2 agents/i, d);
    await waitFor(() => expect(requests("POST", "/agents/update")[0]?.body).toEqual({ hostIds: ["h-a", "h-b"] }));
    expect(await within(d).findAllByText("Pulling the image")).toHaveLength(2);
    expect(within(d).getByRole("progressbar", { name: /update progress/i })).toBeInTheDocument();
    // Behind the modal (aria-hidden): a plain click, so the dialog does not
    // read it as a click outside and close.
    fireEvent.click(screen.getByText("restart a"));
    expect(within(d).getByRole("checkbox", { name: "Update agent-a" })).toBeInTheDocument();
  });

  it("estimates how far a run is", () => {
    expect(runProgress(run({ status: "updated" })).value).toBe(1);
    const early = runProgress(run({ phases: [{ phase: "queued", ok: true, at: NOW }] }));
    const late = runProgress(run({ phases: [{ phase: "queued", ok: true, at: NOW }, { phase: "verify", ok: true, at: NOW }] }));
    expect(early.value).toBeLessThan(late.value);
    expect(late.label).toBe("Checking health");
  });
});
