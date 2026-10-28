import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { detectRegistry, imageHost } from "../../components/images/RegistryImagePicker";
import { requests } from "../../test/api";
import { containers } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";
import type { Registry } from "../../types";

const web = containers[0];
const updates = [{ containerId: web.id, containerName: "shop-web-1", image: "nginx:1.27", localDigest: "a", remoteDigest: "b", updateAvailable: true, checkedAt: "2026-10-26T10:00:00Z" }];
const tagRows = [
  { service: "web", image: "nginx:1.27", current: "1.27", newer: ["1.29", "1.28"], latest: "1.29" },
  { service: "db", image: "postgres:latest", current: "latest", newer: [], note: "latest is not a version tag; the update check covers it" },
];
const plan = {
  changes: [{ service: "web", from: "nginx:1.27", to: "nginx:1.29" }],
  diff: [{ op: "-", text: "    image: nginx:1.27" }, { op: "+", text: "    image: nginx:1.29" }],
};

describe("workload updates", () => {
  it("updates a whole deployment from its header", async () => {
    const { user } = renderApp("/workloads/dep~d-shop", {
      api: {
        "GET /hosts/:id/updates": updates,
        "GET /deployments/:id/tag-updates": { items: [] },
        "POST /deployments/:id/update": { deploymentId: "d-shop", deployment: "shop", outcome: "updated", message: "redeployed web and verified" },
      },
    });
    await user.click(await within(await waitForRoot()).findByRole("button", { name: "Update · 1" }, { timeout: 10_000 }));
    const d = await dialog(/update shop/i);
    expect(within(d).getByText("web")).toBeInTheDocument();
    await press(user, /^update$/i, d);
    await toast(/redeployed web and verified/i);
    expect(requests("POST", "/deployments/d-shop/update")).toHaveLength(1);
  });

  it("shows what moving to newer tags changes before applying it", async () => {
    const { user } = renderApp("/workloads/dep~d-shop", {
      api: {
        "GET /hosts/:id/updates": [],
        "GET /deployments/:id/tag-updates": { items: tagRows },
        "POST /deployments/:id/tag-updates/preview": plan,
        "POST /deployments/:id/tag-updates": plan,
      },
    });
    await user.click(await within(await waitForRoot()).findByRole("button", { name: "New versions · 1" }, { timeout: 10_000 }));
    const d = await dialog(/new versions for shop/i);
    expect(within(d).getByText(/latest is not a version tag/i)).toBeInTheDocument();
    const changes = await within(d).findByRole("list", { name: "Changes" });
    expect(changes).toHaveTextContent("web 1.27 → 1.29");
    expect(within(d).getByRole("table", { name: "Diff" })).toHaveTextContent("image: nginx:1.29");
    expect(requests("POST", "/deployments/d-shop/tag-updates/preview")[0].body).toEqual({ tags: { web: "1.29" } });

    await choose(user, /new tag for web/i, /keep 1\.27/i, d);
    expect(await within(d).findByText(/every service keeps its tag/i)).toBeInTheDocument();
    expect(within(d).getByRole("button", { name: /^apply/i })).toBeDisabled();
    await choose(user, /new tag for web/i, /^1\.28$/i, d);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/tag-updates/preview").at(-1)!.body).toEqual({ tags: { web: "1.28" } }));
    await press(user, /apply 1 and redeploy/i, d);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/tag-updates")).toHaveLength(1));
    expect(requests("POST", "/deployments/d-shop/tag-updates")[0].body).toEqual({ tags: { web: "1.28" }, deploy: true });
  });

  it("hides both buttons when nothing is newer", async () => {
    renderApp("/workloads/dep~d-shop", { api: { "GET /hosts/:id/updates": [], "GET /deployments/:id/tag-updates": { items: [] } } });
    await waitForRoot();
    expect(screen.queryByRole("button", { name: /^update ·/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /new versions/i })).toBeNull();
  });
});

describe("pull dialog", () => {
  it("browses a registry's repositories and tags", async () => {
    const { user } = renderApp("/images", {
      api: {
        "GET /credentials/:id/catalog": { repositories: ["acme/api", "acme/web"] },
        "GET /credentials/:id/tags": { tags: ["1.0", "1.10", "1.9", "latest"] },
        "POST /hosts/:h/images/pull": "{}\n",
      },
    });
    await press(user, /pull image/i, await waitForRoot());
    const d = await dialog(/pull image/i);
    await choose(user, /^registry/i, /ghcr/i, d);
    await choose(user, /^repository/i, "acme/web", d);
    await waitFor(() => expect(within(d).getByText(/with the credentials of/i)).toHaveTextContent("ghcr.io/acme/web:latest"));
    await choose(user, /^tag/i, "1.10", d);
    await press(user, /^pull$/i, d);
    await waitFor(() => expect(requests("POST", "/hosts/local/images/pull")).toHaveLength(1));
    expect(requests("POST", "/hosts/local/images/pull")[0].body).toMatchObject({ ref: "ghcr.io/acme/web:1.10", registryId: "r-ghcr" });
  });

  it("finds the registry from the image name", () => {
    const regs = [
      { id: "r-hub", name: "hub", url: "https://docker.io" },
      { id: "r-ecr", name: "ecr", url: "123.dkr.ecr.eu-west-1.amazonaws.com" },
    ] as Registry[];
    expect(imageHost("nginx:1.27")).toBe("docker.io");
    expect(imageHost("acme/app:1")).toBe("docker.io");
    expect(imageHost("localhost:5000/x")).toBe("localhost:5000");
    expect(detectRegistry("123.dkr.ecr.eu-west-1.amazonaws.com/app:latest", regs)?.id).toBe("r-ecr");
    expect(detectRegistry("redis:7", regs)?.id).toBe("r-hub");
    expect(detectRegistry("ghcr.io/o/a:1", regs)).toBeUndefined();
    expect(detectRegistry("", regs)).toBeUndefined();
  });
});

async function waitForRoot() {
  await waitFor(() => expect(pageRoot().textContent?.length).toBeGreaterThan(0), { timeout: 10_000 });
  return pageRoot();
}
