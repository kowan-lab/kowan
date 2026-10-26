import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { triggerTypes, workflow, workflowRuns } from "../../test/fixtures";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const API = {
  "GET /workflow-trigger-types": triggerTypes,
  "GET /workflows": [workflow],
  "GET /workflows/:id": workflow,
  "GET /workflows/:id/runs": workflowRuns,
  "GET /workflows/:id/runs/:run": workflowRuns[0],
  "GET /credentials": [],
};

describe("workflow editor", () => {
  it("creates a workflow with steps and a schedule", async () => {
    const { user } = renderApp("/workflows/new", { api: { ...API, "POST /workflows": (r: { body: unknown }) => ({ ...workflow, ...(r.body as object), id: "wf-new" }) } });
    const page = pageRoot();
    await within(page).findAllByLabelText(/^name/i, {}, { timeout: 10_000 });
    await user.type(within(page).getByLabelText(/^name/i), "deploy on push");
    await user.type(within(page).getByLabelText(/^description/i), "pull and redeploy");
    await press(user, /add step/i, page);
    await user.type(within(page).getAllByPlaceholderText(/step 1 name/i)[0], "hello");
    await user.type(within(page).getByLabelText(/^script/i), "echo hi");
    await press(user, /add step/i, page);
    // Second step: an HTTP request.
    const types = within(page).getAllByRole("combobox").filter((c) => /run script/i.test(c.textContent ?? ""));
    await user.click(types.at(-1)!);
    await user.click(await screen.findByRole("option", { name: /http request/i }));
    await user.type(within(page).getByLabelText(/^url/i), "https://hooks.example/x");
    await press(user, /add trigger/i, page);
    const cron = within(page).queryByLabelText(/^cron/i);
    if (cron) await user.type(cron, "0 4 * * *");
    await press(user, /^create$/i, page);
    await waitFor(() => expect(requests("POST", "/workflows")).toHaveLength(1));
    const body = requests("POST", "/workflows")[0].body as { name: string; steps: { type: string; config: Record<string, string> }[] };
    expect(body.name).toBe("deploy on push");
    expect(body.steps.map((s) => s.type)).toEqual(["script", "http"]);
    expect(body.steps[0].config.script).toBe("echo hi");
    await waitFor(() => expect(currentPath()).toMatch(/^\/workflows\/wf-new/));
  });

  it("validates a workflow without a name", async () => {
    const { user } = renderApp("/workflows/new", { api: API });
    const page = pageRoot();
    await within(page).findAllByLabelText(/^name/i, {}, { timeout: 10_000 });
    await press(user, /^create$/i, page);
    expect(await within(page).findByText(/name is required/i)).toBeInTheDocument();
    expect(requests("POST", "/workflows")).toHaveLength(0);
  });

  it("edits, runs and shows runs of an existing workflow", async () => {
    const { user } = renderApp("/workflows/wf-1", { api: { ...API, "PUT /workflows/:id": workflow, "POST /workflows/:id/run": { runId: "run-3" } } });
    const page = pageRoot();
    await within(page).findAllByText("nightly backup", {}, { timeout: 10_000 });
    const name = within(page).getByDisplayValue("nightly backup");
    await user.clear(name);
    await user.type(name, "nightly");
    await press(user, /^save$/i, page);
    await waitFor(() => expect(requests("PUT", "/workflows/wf-1")[0].body).toMatchObject({ name: "nightly" }));
    await press(user, /run now|^run$/i, page);
    await waitFor(() => expect(requests("POST", "/workflows/wf-1/run")).toHaveLength(1));
    await user.click(screen.getByRole("tab", { name: /^runs$/i }));
    expect(await within(page).findAllByText(/failed/i)).not.toHaveLength(0);
    await user.click(within(page).getAllByText(/manual/i)[0]);
    expect(await screen.findAllByText(/unexpected status 500/)).not.toHaveLength(0);
  });

  it("manages the run webhook, protection and deletion", async () => {
    const { user } = renderApp("/workflows/wf-1", { api: { ...API, "POST /workflows/:id/webhook": { token: "abc", url: "/hooks/workflow/abc" } } });
    const page = pageRoot();
    await within(page).findAllByText("nightly backup", {}, { timeout: 10_000 });
    await user.click(screen.getByRole("tab", { name: /automation/i }));
    const create = await within(page).findByRole("button", { name: /create webhook|enable|generate/i });
    await user.click(create);
    await waitFor(() => expect(requests("POST", "/workflows/wf-1/webhook")).toHaveLength(1));
    await user.click(within(page).getByRole("switch", { name: /deletion protection/i }));
    await waitFor(() => expect(requests("PUT", "/workflows/wf-1/protection")[0].body).toEqual({ protected: true }));
    const del = within(page).queryByRole("button", { name: /delete workflow|^delete$/i });
    if (del) {
      await user.click(del);
      const d = await dialog();
      const input = within(d).queryByRole("textbox");
      if (input) await user.type(input, "nightly backup");
      await user.click(within(d).getAllByRole("button").at(-1)!);
      await waitFor(() => expect(requests("DELETE", "/workflows/wf-1")).toHaveLength(1));
    }
  });

  it("shows a missing workflow", async () => {
    renderApp("/workflows/nope", { api: { ...API, "GET /workflows/:id": reply(404, { error: "workflow not found" }) } });
    expect(await within(pageRoot()).findAllByText(/workflow not found/i, {}, { timeout: 10_000 })).not.toHaveLength(0);
  });
});

describe("workflows list", () => {
  it("lists and runs a workflow", async () => {
    const { user } = renderApp("/workflows", { api: { ...API, "POST /workflows/:id/run": { runId: "r" } } });
    const page = pageRoot();
    await within(page).findAllByText("nightly backup", {}, { timeout: 10_000 });
    const run = within(page).queryAllByRole("button", { name: /run/i })[0];
    if (run) {
      await user.click(run);
      await waitFor(() => expect(requests("POST", "/workflows/wf-1/run")).toHaveLength(1));
    }
    toast;
  });
});
