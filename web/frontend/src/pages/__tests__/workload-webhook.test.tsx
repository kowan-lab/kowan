import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { containers, deployments } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";

describe("the redeploy webhook card", () => {
  it("shows the new URL as soon as the webhook is created", async () => {
    let shop = { ...deployments[0], redeployToken: "" };
    const api = {
      "GET /deployments": () => [shop, deployments[1]],
      "GET /deployments/:id": () => shop,
      "GET /hosts/:id/containers": containers,
      "POST /deployments/:id/webhook": () => {
        shop = { ...shop, redeployToken: "tok123" };
        return { token: "tok123", url: "/hooks/deploy/tok123" };
      },
    };
    const { user } = renderApp("/workloads/dep~d-shop", { api });
    const create = await within(pageRoot()).findByRole("button", { name: "Create webhook" }, { timeout: 10_000 });
    await user.click(create);
    expect((await screen.findAllByText(/\/hooks\/deploy\/tok123/, {}, { timeout: 5_000 })).length).toBeGreaterThan(0);
  });
});
