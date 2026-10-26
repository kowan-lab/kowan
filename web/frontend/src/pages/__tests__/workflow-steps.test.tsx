// The workflow editor's per-type step forms, their validation and the trigger
// editors (catalog trigger types from the real backend).
import { screen, waitFor, within } from "@testing-library/react";
import type userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { workflow } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { choose, press, toast } from "../../test/ui";

type User = ReturnType<typeof userEvent.setup>;

async function newWorkflow() {
  const r = renderApp("/workflows/new", { api: { "POST /workflows": (q: { body: unknown }) => ({ ...workflow, ...(q.body as object), id: "wf-new" }) } });
  const page = pageRoot();
  await within(page).findAllByLabelText(/^name/i, {}, { timeout: 10_000 });
  await r.user.type(within(page).getByLabelText(/^name/i), "ops");
  return { ...r, page };
}

async function setStepType(user: User, page: HTMLElement, label: RegExp) {
  const types = within(page).getAllByRole("combobox").filter((c) => /run script|http request|deployment|container action|run container|prune|send notification|backup/i.test(c.textContent ?? ""));
  await user.click(types.at(-1)!);
  await user.click(await screen.findByRole("option", { name: label }));
}

async function expectError(user: User, page: HTMLElement, text: RegExp) {
  await press(user, /^create$/i, page);
  expect(await within(page).findByText(text)).toBeInTheDocument();
  expect(requests("POST", "/workflows")).toHaveLength(0);
}

/** Type a free value into a creatable (searchable) select and pick "Use …". */
async function create(user: User, root: HTMLElement, label: RegExp, value: string) {
  await user.click(within(root).getByRole("combobox", { name: label }));
  await user.type(await screen.findByPlaceholderText("Search…"), value);
  await user.click(await screen.findByRole("option", { name: `Use "${value}"` }, { timeout: 3000 }));
}

describe("workflow step forms", () => {
  it("validates and builds every step type", async () => {
    const { user, page } = await newWorkflow();
    await press(user, /add step/i, page);
    await expectError(user, page, /script is required for step 1/i);

    await setStepType(user, page, /http request/i);
    await expectError(user, page, /a url is required/i);

    await setStepType(user, page, /^deployment$/i);
    await expectError(user, page, /a deployment is required/i);
    await choose(user, /^deployment/i, /^shop$/i, page);
    await choose(user, /^action/i, /stop/i, page);

    await press(user, /add step/i, page);
    await setStepType(user, page, /container action/i);
    await expectError(user, page, /a host is required for step 2/i);
    await choose(user, /^host/i, /^local$/i, page);
    await expectError(user, page, /a container is required/i);
    await choose(user, /^container/i, /shop-web-1/i, page);

    await press(user, /add step/i, page);
    await setStepType(user, page, /run container/i);
    await expectError(user, page, /a host is required for step 3/i);
    const hosts = within(page).getAllByRole("combobox", { name: /^host/i });
    await user.click(hosts.at(-1)!);
    await user.click(await screen.findByRole("option", { name: /^local$/i }));
    await expectError(user, page, /an image is required/i);
    await user.type(within(page).getByLabelText(/^image/i), "alpine:3.22");
    await user.type(within(page).getByLabelText(/^command/i), "echo ok");
    await user.type(within(page).getByLabelText(/^environment/i), "A=1");
    await user.type(within(page).getByLabelText(/output variable/i), "OUT");

    await press(user, /add step/i, page);
    await setStepType(user, page, /^prune$/i);
    await expectError(user, page, /a host is required for step 4/i);
    const hosts2 = within(page).getAllByRole("combobox", { name: /^host/i });
    await user.click(hosts2.at(-1)!);
    await user.click(await screen.findByRole("option", { name: /^local$/i }));

    await press(user, /add step/i, page);
    await setStepType(user, page, /send notification/i);
    await expectError(user, page, /a channel is required/i);
    await choose(user, /^channel/i, /ops-hook/i, page);
    await user.type(within(page).getByLabelText(/^title/i), "done");
    await user.type(within(page).getByLabelText(/^body/i), "all good");

    await press(user, /add step/i, page);
    await setStepType(user, page, /^backup$/i);

    await press(user, /^create$/i, page);
    await waitFor(() => expect(requests("POST", "/workflows")).toHaveLength(1));
    const steps = (requests("POST", "/workflows")[0].body as { steps: { type: string; config: Record<string, string> }[] }).steps;
    expect(steps.map((s) => s.type)).toEqual(["deploy", "container", "run-container", "prune", "notify", "backup"]);
    expect(steps[0].config).toMatchObject({ deployment: "d-shop", action: "stop" });
    expect(steps[1].config).toMatchObject({ host: "local", container: "shop-web-1" });
    expect(steps[2].config).toMatchObject({ host: "local", image: "alpine:3.22", command: "echo ok", outputVar: "OUT" });
    expect(steps[4].config).toMatchObject({ channel: "ch-ops", title: "done", body: "all good" });
  });

  it("edits triggers of every catalog type", async () => {
    const { user, page } = await newWorkflow();
    await press(user, /add step/i, page);
    await user.type(within(page).getByLabelText(/^script/i), "echo hi");
    await press(user, /add trigger/i, page);
    await user.type(within(page).getByLabelText(/cron expression/i), "0 3 * * *");

    // A docker-image trigger: free-typed repository and tag (Docker Hub).
    await press(user, /add trigger/i, page);
    const triggerTypes = () => within(page).getAllByRole("combobox").filter((c) => /cron schedule|docker image|git commit|rss feed|webhook/i.test(c.textContent ?? ""));
    await user.click(triggerTypes().at(-1)!);
    await user.click(await screen.findByRole("option", { name: /docker image/i }));
    await create(user, page, /^repository/i, "library/nginx");
    await create(user, page, /^tag/i, "1.27");

    // An RSS trigger, then a webhook trigger with a method option.
    await press(user, /add trigger/i, page);
    await user.click(triggerTypes().at(-1)!);
    await user.click(await screen.findByRole("option", { name: /rss feed/i }));
    await user.type(within(page).getByLabelText(/feed url/i), "https://example.com/feed.xml");

    await press(user, /^create$/i, page);
    await waitFor(() => expect(requests("POST", "/workflows")).toHaveLength(1));
    const triggers = (requests("POST", "/workflows")[0].body as { triggers: { type: string; config: Record<string, string> }[] }).triggers;
    expect(triggers.map((t) => t.type)).toEqual(["cron", "docker-image", "rss"]);
    expect(triggers[0].config.cron).toBe("0 3 * * *");
    expect(triggers[1].config).toMatchObject({ repository: "library/nginx", tag: "1.27" });
    expect(triggers[2].config.url).toBe("https://example.com/feed.xml");
  });

  it("browses registry repositories and tags through a credential", async () => {
    const { user, page } = await newWorkflow();
    mockApi({
      "GET /credentials/:id/registry/repositories": { repositories: ["acme/api", "acme/web"] },
      "GET /credentials/:id/registry/tags": { tags: ["2.1", "2.2"] },
    });
    await press(user, /add step/i, page);
    await user.type(within(page).getByLabelText(/^script/i), "echo hi");
    await press(user, /add trigger/i, page);
    const types = within(page).getAllByRole("combobox").filter((c) => /cron schedule/i.test(c.textContent ?? ""));
    await user.click(types.at(-1)!);
    await user.click(await screen.findByRole("option", { name: /docker image/i }));
    await choose(user, /registry credential/i, /ghcr-acme/i, page);
    await choose(user, /^repository/i, /acme\/web/i, page);
    await waitFor(() => expect(requests("GET", "/credentials/c-ghcr/registry/tags")[0]?.query).toEqual({ repository: "acme/web" }));
    await choose(user, /^tag/i, /^2.2$/i, page);
    await press(user, /^create$/i, page);
    await waitFor(() => expect(requests("POST", "/workflows")).toHaveLength(1));
    const t = (requests("POST", "/workflows")[0].body as { triggers: { config: Record<string, string> }[] }).triggers[0];
    expect(t.config).toMatchObject({ connectionRef: "c-ghcr", repository: "acme/web", tag: "2.2" });
  });

  it("rotates and removes the run webhook, reporting failures", async () => {
    const hooked = { ...workflow, runToken: "tok" };
    const { user } = renderApp("/workflows/wf-1", {
      api: {
        "GET /workflows/:id": hooked,
        "POST /workflows/:id/webhook": { token: "tok2", url: "/hooks/workflow/tok2" },
        "DELETE /workflows/:id/webhook": reply(500, { error: "store locked" }),
      },
    });
    const page = pageRoot();
    await within(page).findAllByText("nightly backup", {}, { timeout: 10_000 });
    await user.click(screen.getByRole("tab", { name: /automation/i }));
    const rotate = await within(page).findByRole("button", { name: /rotate/i });
    await user.click(rotate);
    await toast(/rotated/i);
    await press(user, /copy webhook url/i, page);
    expect(await navigator.clipboard.readText()).toMatch(/\/hooks\//);
    await press(user, /^remove$/i, page);
    await toast(/store locked/i);
    mockApi({ "DELETE /workflows/:id/webhook": {}, "POST /workflows/:id/webhook": reply(500, { error: "no entropy" }) });
    await press(user, /^remove$/i, page);
    await toast(/webhook removed/i);
    await press(user, /^rotate$/i, page);
    await toast(/no entropy/i);
  });
});
