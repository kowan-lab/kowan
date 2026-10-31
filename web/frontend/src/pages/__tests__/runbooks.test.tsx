import { screen, waitFor, within } from "@testing-library/react";
import { act } from "react";
import { describe, expect, it } from "vitest";
import { requests, sockets } from "../../test/api";
import { renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const rb = {
  id: "rb-1", name: "Vacuum", description: "Reclaim space", confirm: true, source: "kowan", workloads: ["dep~d-shop"],
  params: [{ name: "TABLE", label: "Table", type: "text", choices: [], default: "", required: true }],
  steps: [{ title: "", workload: "dep~d-shop", service: "db", mode: "exec", replicas: "one", command: 'vacuumdb -t "$TABLE"',
    continueOnError: false, timeoutSeconds: 600 }],
  createdBy: "root", createdAt: "", updatedAt: "", lastRun: null,
};

describe("runbooks", () => {
  it("runs a runbook with its parameters and follows the output live", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=runbooks", { api: {
      "GET /runbooks": [rb],
      "POST /runbooks/:id/run": { id: "run-1", status: "running" },
    } });
    await user.click(await screen.findByRole("button", { name: "Run Vacuum" }, { timeout: 10_000 }));
    const d = await dialog(/vacuum/i);
    expect(within(d).getByText('vacuumdb -t "$TABLE"')).toBeInTheDocument();
    const run = within(d).getByRole("button", { name: /^run$/i });
    expect(run).toBeDisabled(); // the table is required, and it asks to confirm
    await user.type(within(d).getByLabelText(/^table/i), "orders");
    expect(run).toBeDisabled();
    await user.click(within(d).getByLabelText(/i know what it does/i));
    await user.click(run);
    await waitFor(() => expect(requests("POST", "/runbooks/rb-1/run")[0].body).toEqual({ params: { TABLE: "orders" }, confirm: true }));
    const ws = await waitFor(() => {
      const s = sockets.find((x) => x.url.includes("/runbook-runs/run-1/ws"));
      expect(s).toBeTruthy();
      return s!;
    });
    const rec = { step: 0, title: "", workload: "dep~d-shop", service: "db", mode: "exec", host: "local", container: "shop-db-1",
      status: "running", exitCode: null, error: "", startedAt: "", finishedAt: "" };
    act(() => {
      ws.emit(JSON.stringify({ type: "step", record: 0, data: rec }));
      ws.emit(JSON.stringify({ type: "output", record: 0, text: "vacuuming orders\n" }));
      ws.emit(JSON.stringify({ type: "step", record: 0, data: { ...rec, status: "succeeded", exitCode: 0 } }));
      ws.emit(JSON.stringify({ type: "done", run: { id: "run-1", status: "succeeded", steps: [] } }));
    });
    expect(await within(d).findByText("vacuuming orders")).toBeInTheDocument();
    expect(within(d).getByText("shop-db-1")).toBeInTheDocument();
    expect(within(d).getAllByText("Succeeded").length).toBeGreaterThan(0);
    const closes = within(d).getAllByRole("button", { name: /^close$/i });
    await user.click(closes[closes.length - 1]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("creates a runbook from the workload, and adds a suggested one", async () => {
    const { user } = renderApp("/workloads/dep~d-shop?tab=runbooks", { api: {
      "GET /runbooks": [],
      "GET /runbooks/suggestions": [{ name: "Redis memory", description: "How much memory", confirm: false, params: [],
        steps: [{ title: "", workload: "dep~d-shop", service: "db", mode: "exec", replicas: "one", command: "redis-cli INFO memory",
          continueOnError: false, timeoutSeconds: 600 }] }],
      "POST /runbooks": { ...rb, id: "rb-2" },
    } });
    expect(await screen.findByText(/no runbooks yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New runbook" }));
    let d = await dialog(/new runbook/i);
    await user.type(within(d).getByLabelText(/^name/i), "Migrate");
    await user.type(within(d).getByLabelText(/^command/i), "python manage.py migrate");
    await user.click(within(d).getByLabelText(/^may fail/i));
    await press(user, /^create$/i, d);
    await toast(/runbook created/i);
    const body = requests("POST", "/runbooks")[0].body as { name: string; steps: { workload: string; command: string; continueOnError: boolean }[] };
    expect(body.name).toBe("Migrate");
    expect(body.steps[0]).toMatchObject({ workload: "dep~d-shop", command: "python manage.py migrate", continueOnError: true });

    await user.click(await screen.findByRole("button", { name: "Add Redis memory" }));
    d = await dialog(/new runbook/i);
    expect(within(d).getByLabelText(/^name/i)).toHaveValue("Redis memory");
    expect(within(d).getByLabelText(/^command/i)).toHaveValue("redis-cli INFO memory");
  });

  it("lists every runbook on its page, label ones read-only, with their history", async () => {
    const labels = { ...rb, id: "label:dep~d-shop:web:hello", name: "Say hello", source: "labels", confirm: false, params: [],
      lastRun: { id: "run-9", status: "failed", startedAt: "2026-10-31T10:00:00Z", finishedAt: "", user: "root", trigger: "manual" } };
    const { user } = renderApp("/runbooks", { api: {
      "GET /runbooks": [rb, labels],
      "GET /runbooks/:id/runs": [{ id: "run-9", status: "failed", startedAt: "2026-10-31T10:00:00Z", finishedAt: "", user: "root",
        trigger: "schedule", params: {}, steps: [], runbookId: labels.id, runbookName: "Say hello", workloads: [] }],
    } });
    expect(await screen.findByText("Say hello", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("From labels")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit Say hello" })).toBeNull();
    expect(screen.getByRole("button", { name: "Edit Vacuum" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "History of Say hello" }));
    expect(await screen.findByText("scheduled")).toBeInTheDocument();
  });
});
