import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests } from "../../test/api";
import { renderApp } from "../../test/render";

const deployed = {
  deploymentId: "d-reg", registryId: "r-reg", name: "registry", url: "https://registry.example.com",
  address: "registry.example.com", auth: true, proxy: "traefik", username: "kowan", password: "generated-secret-value",
};

const proxy = {
  kind: "traefik", containerId: "c-traefik", name: "traefik", image: "traefik:v3.6", state: "running", networks: ["kowan-proxy"],
  ports: [], entrypoints: { web: ":80", websecure: ":443" }, secureEntrypoint: "websecure", httpEntrypoint: "web",
  certResolvers: ["letsencrypt"], resolvers: {}, dockerNetwork: "kowan-proxy", exposedByDefault: false, apiEnabled: true,
};
const exposure = (proxies: unknown[]) => ({ hostId: "local", hostName: "local", ports: [], domains: [], proxies, summary: {} });

async function openDialog(proxies: unknown[]) {
  const view = renderApp("/registries", {
    api: { "POST /hosts/:id/registry": deployed, "GET /hosts/:id/exposure": exposure(proxies) },
  });
  await view.user.click(await screen.findByRole("button", { name: "Deploy a registry" }, { timeout: 10_000 }));
  return { ...view, dialog: await screen.findByRole("dialog") };
}

describe("deploy a registry", () => {
  it("deploys behind Traefik and shows the login once", async () => {
    const { user, dialog } = await openDialog([proxy]);
    expect(await within(dialog).findByText(/published through/i)).toBeInTheDocument();
    const deploy = within(dialog).getByRole("button", { name: "Deploy" });
    expect(deploy).toBeDisabled(); // no domain yet
    await user.type(within(dialog).getByLabelText(/^domain/i), "registry.example.com");

    await user.click(within(dialog).getByRole("switch", { name: /require a login/i }));
    expect(within(dialog).getByText(/anyone who can reach the registry/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("switch", { name: /require a login/i }));
    await user.type(within(dialog).getByLabelText(/^password/i), "short");
    expect(within(dialog).getByText("At least 8 characters")).toBeInTheDocument();
    expect(deploy).toBeDisabled();
    await user.clear(within(dialog).getByLabelText(/^password/i));

    await user.click(deploy);
    await waitFor(() => expect(requests("POST", "/hosts/local/registry")).toHaveLength(1));
    expect(requests("POST", "/hosts/local/registry")[0].body).toEqual({
      name: "registry", domain: "registry.example.com", auth: true, username: "kowan", password: "",
    });
    expect(await screen.findByText("generated-secret-value")).toBeInTheDocument();
    expect(screen.getByText("docker login registry.example.com -u kowan")).toBeInTheDocument();
    expect(screen.getByText("registry.example.com/my-app:1.0")).toBeInTheDocument();
  });

  it("asks for Traefik when the host has none", async () => {
    const { user, dialog } = await openDialog([]);
    await user.type(within(dialog).getByLabelText(/^domain/i), "registry.example.com");
    expect(await within(dialog).findByText(/no reverse proxy on this host/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Deploy" })).toBeDisabled();
  });
});
