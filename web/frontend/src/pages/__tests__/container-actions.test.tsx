// Container actions: Kill on the container page, and the "⋯" menu of each
// container in a workload page and in the Workloads table.
import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { containerInspect, containers, containerSpec } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const blog = [
  { ...containers[1], id: "b1".padEnd(64, "0"), names: ["/blog-app-1"], state: "running",
    labels: { "com.docker.compose.project": "blog", "com.docker.compose.service": "app" } },
];
const api = { "GET /hosts/:id/containers": [...containers, ...blog] };

describe("container actions", () => {
  it("kills a container from its page after a confirmation", async () => {
    const { user } = renderApp(`/containers/${containers[0].id}?host=local`, {
      api: { "GET /hosts/:h/containers/:id": containerInspect, "GET /hosts/:h/containers/:id/spec": containerSpec, "GET /hosts/:h/updates": [] },
    });
    await screen.findByRole("heading", { name: "shop-web-1" }, { timeout: 10_000 });
    await press(user, "More actions");
    await user.click(await screen.findByRole("menuitem", { name: /kill/i }));
    const d = await dialog(/kill shop-web-1/i);
    expect(within(d).getByText(/SIGKILL/)).toBeInTheDocument();
    await user.click(within(d).getByRole("button", { name: "Kill container" }));
    await toast(/killed/i);
    expect(requests("POST", "/hosts/local/containers/:id/kill")).toHaveLength(1);
  });

  it("acts on one container of a stack from its workload page", async () => {
    const { user } = renderApp("/workloads/stack~local~blog", { api });
    const menu = await screen.findByRole("button", { name: "Actions for blog-app-1" }, { timeout: 10_000 });
    await user.click(menu);
    // A running container: no Start or Unpause.
    expect(screen.queryByRole("menuitem", { name: "Start" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Unpause" })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "Pause" }));
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/pause")).toHaveLength(1));
    await user.click(menu);
    await user.click(screen.getByRole("menuitem", { name: /kill/i }));
    await user.click(within(await dialog(/kill blog-app-1/i)).getByRole("button", { name: "Kill container" }));
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/kill")).toHaveLength(1));
    await user.click(menu);
    await user.click(screen.getByRole("menuitem", { name: /remove/i }));
    const d = await dialog(/remove blog-app-1/i);
    // Running: forced by default, and it says the stack brings it back.
    expect(within(d).getByRole("checkbox", { name: /force/i })).toBeChecked();
    expect(within(d).getByText(/next deploy creates it again/i)).toBeInTheDocument();
    await user.click(within(d).getByRole("button", { name: "Remove container" }));
    await toast(/container removed/i);
    expect(requests("DELETE", "/hosts/local/containers/:id")[0].query).toEqual({ force: "true" });
  });

  it("offers start for a stopped container and reports a refusal", async () => {
    const stopped = [{ ...blog[0], state: "exited", status: "Exited (0) 1 hour ago" }];
    const { user } = renderApp("/workloads/stack~local~blog", {
      api: { "GET /hosts/:id/containers": [...containers, ...stopped], "POST /hosts/:h/containers/:id/start": reply(500, { error: "port is busy" }) },
    });
    await user.click(await screen.findByRole("button", { name: "Actions for blog-app-1" }, { timeout: 10_000 }));
    expect(screen.queryByRole("menuitem", { name: /kill/i })).toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "Start" }));
    await toast(/port is busy/i);
  });

  it("acts from a container row of the Workloads table", async () => {
    const { user } = renderApp("/workloads?view=table", { api });
    await within(pageRoot()).findAllByText("blog", {}, { timeout: 10_000 });
    await user.click(within(screen.getByRole("group", { name: /^view$/i })).getAllByRole("button")[1]);
    await user.click(await screen.findByRole("button", { name: /show containers of blog$/i }));
    await user.click(screen.getByRole("button", { name: /show containers of service app/i }));
    await user.click(screen.getByRole("button", { name: "Actions for blog-app-1" }));
    await user.click(screen.getByRole("menuitem", { name: "Restart" }));
    await waitFor(() => expect(requests("POST", "/hosts/local/containers/:id/restart")).toHaveLength(1));
  });

  it("shows no actions to viewers", async () => {
    renderApp("/workloads/stack~local~blog", { role: "viewer", api });
    await screen.findByRole("heading", { name: "blog" }, { timeout: 10_000 });
    expect(screen.queryByRole("button", { name: "Actions for blog-app-1" })).toBeNull();
  });
});
