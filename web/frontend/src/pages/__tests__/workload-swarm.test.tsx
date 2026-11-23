import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MockRequest } from "../../test/api";
import { containers, deployments, hosts } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";

// A swarm deployment targets its manager, but its tasks run on every node.
const nodes = [hosts[0], { ...hosts[0], id: "w1", name: "worker-1", type: "agent" as const }];
const task = (id: string, svc: string, host: string) => ({
  ...containers[0], id: id.padEnd(64, "0"), names: [`/flow_${svc}.1.${id}`], hostId: host,
  labels: { "com.docker.stack.namespace": "flow", "com.docker.swarm.service.name": `flow_${svc}`, "com.docker.swarm.task.name": `flow_${svc}.1.${id}` },
});
const flow = { ...deployments[0], id: "d-flow", name: "flow", strategy: "swarm" as const, compose: "services:\n  ui:\n    image: x\n  sync:\n    image: x\n" };
const api = {
  "GET /hosts": nodes,
  "GET /deployments": [...deployments, flow],
  "GET /deployments/:id": flow,
  "GET /hosts/:id/containers": (req: MockRequest) => (req.params.id === "w1" ? [task("t2", "sync", "w1")] : [task("t1", "ui", "local")]),
};

describe("a swarm deployment", () => {
  it("lists its tasks on every node, not only on the manager", async () => {
    renderApp("/workloads/dep~d-flow", { api });
    await screen.findByRole("heading", { name: "flow" }, { timeout: 10_000 });
    expect(await within(pageRoot()).findByText("sync", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(within(pageRoot()).getByText("ui")).toBeInTheDocument();
  });
});
