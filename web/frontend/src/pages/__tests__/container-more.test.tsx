// Container detail: edit & recreate, the problem banners (exit codes, OOM,
// restarts, failing health checks) and the image update state.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { containerInspect, containers, containerSpec } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const ID = containers[2].id; // a lone container, so it can be edited
const lone = { ...containerInspect, Id: ID, Name: "/nightly-job", Config: { ...containerInspect.Config, Labels: {} } };
const state = (s: Record<string, unknown>, policy = "no") =>
  ({ ...lone, State: { ...lone.State, ...s }, HostConfig: { ...lone.HostConfig, RestartPolicy: { Name: policy } } });
const URL = `/containers/${ID}?host=local`;
const base = {
  "GET /hosts/:h/containers/:id/spec": containerSpec,
  "GET /hosts/:h/updates": [],
};

async function open(inspect: object, extra: Record<string, unknown> = {}) {
  const r = renderApp(URL, { api: { ...base, "GET /hosts/:h/containers/:id": inspect, ...extra } as never });
  await within(pageRoot()).findAllByText("nightly-job", {}, { timeout: 10_000 });
  return r;
}

describe("container problems", () => {
  const cases: [string, object, RegExp, RegExp?][] = [
    ["exit 127", state({ Status: "exited", Running: false, ExitCode: 127, FinishedAt: "2026-10-26T09:00:00Z" }), /exited with code 127/i, /command was not found/i],
    ["exit 1 with an error", state({ Status: "exited", Running: false, ExitCode: 1, Error: "exec format error" }), /exited with code 1/i, /exec format error/i],
    ["OOM without a limit", state({ Status: "exited", Running: false, ExitCode: 137, OOMKilled: true }), /out of memory/i, /no memory limit is set/i],
    ["restarting", state({ Status: "restarting", Running: true, Restarting: true }), /restarting: 0 restarts so far/i],
    ["dead", state({ Status: "dead", Running: false, ExitCode: 0 }), /nightly-job/i],
    // unless-stopped would have restarted a crash: npm start exits 1 when stopped.
    ["a stop with an error code", state({ Status: "exited", Running: false, ExitCode: 1 }, "unless-stopped"), /^stopped.*exit code 1/i, /stopped on purpose/i],
  ];
  for (const [name, inspect, title, detail] of cases) {
    it(`explains ${name}`, async () => {
      await open(inspect);
      expect((await screen.findAllByText(title)).length).toBeGreaterThan(0);
      if (detail) expect(screen.getAllByText(detail).length).toBeGreaterThan(0);
    });
  }

  it("shows a failing health check with the last probe", async () => {
    await open({
      ...lone,
      Config: { ...lone.Config, Healthcheck: { Test: ["CMD-SHELL", "curl -f localhost"] } },
      State: { ...lone.State, Health: { Status: "unhealthy", FailingStreak: 3, Log: [{ ExitCode: 7, Output: "connection refused\n" }] } },
    });
    expect(await screen.findByText(/health check failing \(3 in a row\)/i)).toBeInTheDocument();
    expect(screen.getByText(/curl -f localhost · last exit 7/)).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
  });

  it("shows the image update state", async () => {
    await open(lone, {
      "GET /hosts/:h/updates": [{ containerId: ID, containerName: "nightly-job", image: "alpine:3.22", localDigest: "a", remoteDigest: "b", updateAvailable: true, checkedAt: "2026-10-26T10:00:00Z" }],
    });
    expect((await screen.findAllByText(/update available/i)).length).toBeGreaterThan(0);
  });

  it("shows a failed update check", async () => {
    await open(lone, {
      "GET /hosts/:h/updates": [{ containerId: ID, containerName: "nightly-job", image: "alpine:3.22", localDigest: "a", remoteDigest: "", updateAvailable: false, checkedAt: "", error: "registry unreachable" }],
    });
    expect(await screen.findByText(/update check failed/i)).toBeInTheDocument();
  });
});

describe("edit and recreate", () => {
  it("edits the spec and follows the new container id", async () => {
    const { user } = await open(lone, { "POST /hosts/:h/containers": { id: "c-new".padEnd(64, "0") } });
    await press(user, /more actions/i, pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /edit & recreate/i }));
    const d = await dialog(/edit and recreate/i);
    const image = await within(d).findByLabelText(/^image/i);
    await user.clear(image);
    await press(user, /recreate container/i, d);
    expect(await within(d).findByText(/image is required/i)).toBeInTheDocument();
    await user.type(image, "nginx:1.28");
    await press(user, /recreate container/i, d);
    await toast(/container recreated/i);
    expect(requests("POST", "/hosts/local/containers")[0].body).toMatchObject({ name: "nightly-job", image: "nginx:1.28" });
    await waitFor(() => expect(currentPath()).toMatch(/^\/containers\/c-new/));
  });

  it("reports a rejected configuration and a spec that cannot load", async () => {
    const { user } = await open(lone, { "POST /hosts/:h/containers": reply(400, { error: "invalid mount config" }) });
    await press(user, /more actions/i, pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /edit & recreate/i }));
    const d = await dialog(/edit and recreate/i);
    await within(d).findByLabelText(/^image/i);
    await press(user, /recreate container/i, d);
    await toast(/invalid mount config/i);
  });

  it("explains a spec load error", async () => {
    const { user } = await open(lone, { "GET /hosts/:h/containers/:id/spec": reply(500, { error: "inspect failed" }) });
    await press(user, /more actions/i, pageRoot());
    await user.click(await screen.findByRole("menuitem", { name: /edit & recreate/i }));
    expect(await within(await dialog(/edit and recreate/i)).findByText(/could not load the current configuration/i)).toBeInTheDocument();
  });
});
