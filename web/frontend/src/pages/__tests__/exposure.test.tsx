import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ExposeButton from "../../components/exposure/ExposeDialog";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp, renderWithProviders } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const owner = { containerId: "c-web", containerName: "shop-web-1", image: "nginx", state: "running", stack: "shop", service: "web", deploymentId: "d-shop", deploymentName: "shop", deploymentType: "compose" };
const traefik = {
  kind: "traefik", containerId: "c-traefik", name: "traefik", image: "traefik:v3.5", state: "running", networks: ["proxy"], ports: [],
  entrypoints: { web: ":80", websecure: ":443" }, secureEntrypoint: "websecure", httpEntrypoint: "web", certResolvers: ["le"],
  resolvers: {}, dockerNetwork: "proxy", exposedByDefault: false, apiEnabled: true,
};
const view = (over: Record<string, unknown> = {}) => ({
  hostId: "local", hostName: "local",
  ports: [
    { ...owner, ip: "0.0.0.0", publicPort: 8080, privatePort: 80, protocol: "tcp", scope: "public", risk: null, warn: false },
    { ...owner, containerId: "c-db", containerName: "shop-db-1", deploymentId: "", ip: "0.0.0.0", publicPort: 5432, privatePort: 5432, protocol: "tcp", scope: "public", risk: { kind: "database", label: "PostgreSQL" }, warn: true },
    { ...owner, containerId: "c-job", containerName: "job", deploymentId: "", stack: "", ip: "::1", publicPort: 9000, privatePort: 9000, protocol: "tcp", scope: "local", risk: null, warn: false },
  ],
  domains: [
    { ...owner, domain: "shop.example.com", proxy: "traefik", router: "shop", protocol: "http", entrypoints: ["websecure"], tls: true, certResolver: "le", rule: "", enabled: true, port: 80, proxyRunning: true, managedByKowan: true },
    { ...owner, containerId: "c-db", domain: "plain.example.com", proxy: "traefik", router: "plain", protocol: "http", entrypoints: [], tls: false, certResolver: "", rule: "", enabled: true, port: 0, proxyRunning: false, managedByKowan: false },
  ],
  proxies: [traefik],
  summary: { publicPorts: 2, localPorts: 1, interfacePorts: 0, riskyPublic: 1, domains: 2 },
  ...over,
});

describe("exposure page", () => {
  it("shows ports by reach, risky services and domains, then checks certificates", async () => {
    const { user } = renderApp("/exposure", {
      api: {
        "GET /hosts/:id/exposure": view(),
        "GET /hosts/:id/exposure/certificates": {
          hostId: "local", notes: ["Traefik API reached"],
          domains: [{ domain: "shop.example.com", proxy: "traefik", router: "shop", containerName: "shop-web-1", tls: true, certResolver: "le", routerStatus: "enabled", routerErrors: [],
            certificate: { issuer: "R11", notAfter: "2026-12-01", daysLeft: 36, status: "valid", staging: false, resolver: "le", source: "acme" } }],
        },
      },
    });
    expect(await within(pageRoot()).findByText("2 public ports", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getAllByText(/1 sensitive service/).length).toBeGreaterThan(0);
    expect(screen.getByText("PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText("[::1]:9000/tcp")).toBeInTheDocument();
    expect(screen.getByText(/http only/i)).toBeInTheDocument();
    expect(screen.getByText(/no proxy/i)).toBeInTheDocument();
    expect(screen.getByText("labels")).toBeInTheDocument();
    await press(user, /check certificates/i, pageRoot());
    expect(await screen.findByText("36d")).toBeInTheDocument();
    expect(screen.getByText("Traefik API reached")).toBeInTheDocument();
    await user.click(screen.getAllByText("shop-db-1")[0]);
    await waitFor(() => expect(currentPath()).toBe("/containers/c-db?host=local"));
  });

  it("offers to deploy Traefik when none runs", async () => {
    const { user } = renderApp("/exposure", {
      api: { "GET /hosts/:id/exposure": view({ proxies: [], domains: [] }), "POST /hosts/:id/exposure/proxy": { deploymentId: "d-t", name: "traefik" } },
    });
    await within(pageRoot()).findByText("2 public ports", {}, { timeout: 10_000 });
    expect(screen.getByText(/no traefik running/i)).toBeInTheDocument();
    await press(user, /deploy traefik/i, pageRoot());
    const d = await dialog(/deploy traefik on local/i);
    expect(within(d).getByRole("button", { name: /deploy traefik/i })).toBeDisabled();
    await user.type(within(d).getByLabelText(/email for let/i), "ops@example.com");
    const http = within(d).getByLabelText(/^http port/i);
    await user.clear(http);
    await user.type(http, "8080");
    expect(within(d).getByText(/already published/i)).toBeInTheDocument();
    await user.click(within(d).getByRole("checkbox", { name: /staging ca/i }));
    await press(user, /deploy traefik/i, d);
    await toast(/deploying traefik/i);
    expect(requests("POST", "/hosts/local/exposure/proxy")[0].body).toEqual({ kind: "traefik", email: "ops@example.com", httpPort: 8080, httpsPort: 443, staging: true });
    expect(within(d).getByRole("button", { name: /deploying/i })).toBeDisabled();
  });

  it("aggregates all hosts and shows per-host errors", async () => {
    localStorage.setItem("kowan_host", "all");
    renderApp("/exposure", {
      api: { "GET /exposure": { hosts: [view(), { ...view(), hostId: "h-edge", hostName: "edge-1", error: "agent offline", ports: [], domains: [], proxies: [], summary: {} }] } },
    });
    expect(await screen.findByText("agent offline", {}, { timeout: 10_000 })).toBeInTheDocument();
    expect(screen.getByText("edge-1")).toBeInTheDocument();
  });

  it("shows a load error", async () => {
    renderApp("/exposure", { api: { "GET /hosts/:id/exposure": reply(502, { error: "docker unreachable" }) } });
    expect(await screen.findByText(/docker unreachable/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("expose dialog", () => {
  it("exposes a container through the running proxy", async () => {
    const { user } = renderWithProviders(<ExposeButton hostId="local" target={{ kind: "container", id: "c-job" }} name="job" defaultPort={8000} />);
    mockApi({
      "GET /hosts/:id/exposure": view(),
      "POST /hosts/:id/exposure/expose": { status: "ok", kind: "container", containerId: "c-new", domain: "job.example.com", proxyName: "traefik", warnings: ["DNS not pointing here yet"] },
    });
    await user.click(await screen.findByRole("button", { name: /^expose$/i }));
    const d = await dialog(/expose job/i);
    const domain = await within(d).findByLabelText(/^domain/i);
    expect(within(d).getByLabelText(/container port/i)).toHaveValue("8000");
    expect(within(d).getByRole("button", { name: /^expose$/i })).toBeDisabled();
    await user.type(domain, "job.example.com");
    await choose(user, /^entrypoint/i, /^web \(/i, d);
    await choose(user, /certificate resolver/i, /^none$/i, d);
    expect(within(d).getByText(/self-signed certificate/i)).toBeInTheDocument();
    await press(user, /^expose$/i, d);
    await toast(/dns not pointing here yet/i);
    expect(requests("POST", "/hosts/local/exposure/expose")[0].body).toMatchObject({
      target: { kind: "container", id: "c-job" }, domain: "job.example.com", port: 8000, proxyId: "c-traefik", entrypoint: "web", tls: true,
    });
  });

  it("updates and unexposes a managed deployment route", async () => {
    const { user } = renderWithProviders(
      <ExposeButton hostId="local" target={{ kind: "deployment", id: "d-shop" }} name="shop" services={["web", "db"]} />,
    );
    mockApi({
      "GET /hosts/:id/exposure": view({ proxies: [traefik, { ...traefik, containerId: "c-t2", name: "edge-proxy" }] }),
      "POST /hosts/:id/exposure/unexpose": { status: "redeploying", domain: "shop.example.com" },
      "POST /hosts/:id/exposure/expose": reply(400, { error: "domain already routed" }),
    });
    await user.click(await screen.findByRole("button", { name: /^expose$/i }));
    const d = await dialog(/expose shop/i);
    expect(await within(d).findByLabelText(/^domain/i)).toHaveValue("shop.example.com");
    expect(within(d).getByText(/its deployment is updated and redeployed/i)).toBeInTheDocument();
    await choose(user, /^service/i, /^db$/i, d);
    await choose(user, /^proxy/i, /edge-proxy/i, d);
    await press(user, /^update$/i, d);
    await toast(/domain already routed/i);
    expect(requests("POST", "/hosts/local/exposure/expose")[0].body).toMatchObject({ target: { kind: "deployment", id: "d-shop", service: "db" }, proxyId: "c-t2" });
    await press(user, /unexpose shop.example.com/i, d);
    await toast(/unexposing: redeploy started/i);
    expect(requests("POST", "/hosts/local/exposure/unexpose")[0].body).toEqual({ target: { kind: "deployment", id: "d-shop", service: "web" } });
  });

  it("offers the proxy deploy inside the dialog and handles errors", async () => {
    const { user } = renderWithProviders(<ExposeButton hostId="local" target={{ kind: "container", id: "c-job" }} name="job" />);
    mockApi({ "GET /hosts/:id/exposure": view({ proxies: [] }), "POST /hosts/:id/exposure/proxy": reply(409, { error: "port 80 in use" }) });
    await user.click(await screen.findByRole("button", { name: /^expose$/i }));
    const d = await dialog(/expose job/i);
    await user.type(await within(d).findByLabelText(/email for let/i), "a@b.c");
    await press(user, /deploy traefik/i, d);
    await toast(/port 80 in use/i);
  });

  it("shows host read errors and hides for viewers", async () => {
    const { user } = renderWithProviders(<ExposeButton hostId="local" target={{ kind: "container", id: "c-job" }} name="job" />);
    mockApi({ "GET /hosts/:id/exposure": reply(502, { error: "agent offline" }) });
    await user.click(await screen.findByRole("button", { name: /^expose$/i }));
    expect(await within(await dialog(/expose job/i)).findByText(/agent offline/i)).toBeInTheDocument();
  });
});
