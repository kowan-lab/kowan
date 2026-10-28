import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi } from "../../test/api";
import * as fx from "../../test/fixtures";
import { currentPath, renderWithProviders } from "../../test/render";
import CommandPalette from "../../components/shell/CommandPalette";
import type { Container } from "../../types";

/** A compose stack Kowan did not deploy, found from its Docker labels. */
const grafana: Container = {
  id: "c-graf".padEnd(64, "0"), names: ["/monitoring-grafana-1"], image: "grafana/grafana:11", state: "running", status: "Up 1 hour",
  created: 1_761_000_000, ports: [], labels: { "com.docker.compose.project": "monitoring", "com.docker.compose.service": "grafana" },
  hostId: "local",
};

async function open() {
  const r = renderWithProviders(<CommandPalette open onOpenChange={() => {}} />);
  mockApi({ "GET /hosts/:id/containers": [...fx.containers, grafana] });
  const d = await screen.findByRole("dialog", { name: /command palette/i });
  return { ...r, d, input: within(d).getByRole("combobox") };
}

function groupOf(d: HTMLElement, heading: RegExp): HTMLElement {
  const g = [...d.querySelectorAll<HTMLElement>("[cmdk-group]")].find((x) => heading.test(x.querySelector("[cmdk-group-heading]")?.textContent ?? ""));
  if (!g) throw new Error(`no group ${heading}`);
  return g;
}

describe("command palette", () => {
  it("finds compose stacks and deployments, not only containers", async () => {
    const { user, d, input } = await open();
    await user.type(input, "monitoring");
    const top = await waitFor(() => groupOf(d, /top result/i));
    expect(within(top).getByText(/Compose stack/)).toBeInTheDocument();
    expect(groupOf(d, /containers/i).textContent).toMatch(/monitoring-grafana-1/);

    await user.clear(input);
    await user.type(input, "shop");
    await waitFor(() => expect(within(groupOf(d, /top result/i)).getByText(/Compose deployment/)).toBeInTheDocument());
    expect(within(groupOf(d, /containers/i)).getAllByRole("option")).toHaveLength(2);

    // A lone container is its own workload: listed once.
    await user.clear(input);
    await user.type(input, "nightly");
    await waitFor(() => expect(groupOf(d, /top result/i).textContent).toMatch(/nightly-job/));
    expect(() => groupOf(d, /containers/i)).toThrow();
  });

  it("narrows to one kind with the filters and Tab", async () => {
    const { user, d, input } = await open();
    await user.type(input, "shop");
    await user.click(within(d).getByRole("tab", { name: /containers/i }));
    await waitFor(() => expect(d.querySelectorAll("[cmdk-group]")).toHaveLength(1));
    const names = within(d).getAllByRole("option").map((o) => o.textContent);
    expect(names).toHaveLength(2);
    expect(names).toEqual(expect.arrayContaining([expect.stringMatching(/^shop-web-1/), expect.stringMatching(/^shop-db-1/)]));

    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(within(d).getByRole("tab", { name: /workloads/i })).toHaveAttribute("aria-selected", "true");
    await user.clear(input);
    await user.keyboard("{Backspace}");
    expect(within(d).getByRole("tab", { name: /^all/i })).toHaveAttribute("aria-selected", "true");
  });

  it("shows what needs attention first and opens a workload", async () => {
    const { user, d } = await open();
    const attention = await waitFor(() => groupOf(d, /needs attention/i));
    expect(within(attention).getByText("nightly-job")).toBeInTheDocument();
    await user.click(within(groupOf(d, /other workloads/i)).getByText("shop"));
    expect(currentPath()).toBe("/workloads/dep~d-shop");
  });
});
