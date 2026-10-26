// Mount the real app (router, auth, host selection, react-query, theme) at a
// given URL against the fake backend in ./api, signed in as a given role.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, type RenderResult } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import App from "../App";
import { TOKEN_KEY } from "../api/client";
import { AuthProvider } from "../auth/AuthContext";
import { Toaster } from "../components/ui/sonner";
import { HostProvider } from "../HostContext";
import { TooltipProvider } from "../ui/kit";
import { ThemeProvider } from "../ui/theme";
import { mockApi, type Handler } from "./api";
import catalog from "./catalog.json";
import { clients } from "./clients";
import * as fx from "./fixtures";

export type Role = "admin" | "operator" | "viewer";

/** An auto-update policy view: nothing stored, everything from the defaults. */
export function policy(mode = "off") {
  const src = { mode: "default", window: "default", minAge: "default", scanBlock: "default", notify: "default" };
  return {
    kind: "container",
    hostId: "local",
    target: "shop-web-1",
    stored: null,
    effective: { mode, window: "", minAge: "", scanBlock: "never", notify: true, sources: src, errors: [] },
    window: { valid: true, always: true },
    labels: {},
    state: null,
    history: [],
    deploymentId: "",
    managedByDeployment: false,
  };
}

/** The endpoints the shell (sidebar, top bar, bell) and most pages read. */
export function baseApi(role: Role = "admin"): void {
  const user = fx.users[role];
  mockApi({
    "GET /me": user,
    "GET /me/permissions": { username: user.username, role, mode: role === "admin" ? "admin" : "global", scoped: false, grants: [], teams: [] },
    "GET /auth/status": { initialized: true, setupTokenRequired: false },
    "GET /auth/sso/providers": { providers: [], ssoOnly: false },
    "GET /hosts": fx.hosts,
    "GET /hosts/:id": (r: { params: Record<string, string> }) => fx.hosts.find((h) => h.id === r.params.id) ?? fx.hosts[0],
    "GET /notifications/unread-count": { count: 2 },
    "GET /notifications": [],
    "GET /hosts/:id/containers": fx.containers,
    "GET /hosts/:id/images": fx.images,
    "GET /hosts/:id/volumes": fx.volumes,
    "GET /hosts/:id/networks": fx.networks,
    "GET /hosts/:id/system": fx.systemInfo,
    "GET /hosts/:id/df": fx.diskUsage,
    "GET /hosts/:id/metrics": [],
    "GET /hosts/:id/scans": (r: { params: Record<string, string> }) => ({
      hostId: r.params.id, images: [], containers: {}, totals: { critical: 0, high: 0, medium: 0, low: 0, unknown: 0 },
      scanned: 0, imageCount: 0,
    }),
    "GET /scans/summary": [],
    "GET /hosts/:id/images/:img/scan": (r: { params: Record<string, string> }) => ({
      imageId: r.params.img, running: false, fresh: false, result: null,
      scanner: { image: "aquasec/trivy:0.75.0", version: "0.75.0" },
    }),
    "GET /hosts/:id/containers/:cid/autoupdate": policy(),
    "GET /deployments/:id/autoupdate": policy(),
    "GET /deployments": fx.deployments,
    "GET /deployments/:id": (r: { params: Record<string, string> }) => fx.deployments.find((d) => d.id === r.params.id) ?? fx.deployments[0],
    "GET /deployments/:id/status": [{ hostId: "local", hostName: "local", state: "running", containerId: fx.containers[0].id, message: "" }],
    "GET /deployments/:id/services": [
      { service: "web", containerId: fx.containers[0].id, name: "shop-web-1", state: "running", image: "nginx:1.27", ports: [] },
      { service: "db", containerId: fx.containers[1].id, name: "shop-db-1", state: "running", image: "postgres:16", ports: [] },
    ],
    "POST /compose/validate": { diagnostics: [], engine: "lint", checks: [], summary: { errors: 0, warnings: 0, infos: 0 } },
    "GET /audit": { items: [], total: 0 },
    "GET /schedules": [],
    "POST /schedules/preview": { nextRuns: ["2026-10-27T03:00:00Z", "2026-10-28T03:00:00Z"] },
    "GET /credentials": (r: { query: Record<string, string> }) =>
      r.query.kind ? (fx.credentials as Record<string, unknown[]>)[r.query.kind] ?? [] : Object.values(fx.credentials).flat(),
    "GET /credentials/types": (r: { query: Record<string, string> }) =>
      (catalog as Record<string, unknown>)["credentialTypes_" + (r.query.kind || "secret")] ?? [],
    "GET /volume-plugins": catalog.volumePlugins,
    "GET /workflow-trigger-types": catalog.triggerTypes,
    "GET /alert-rule-types": catalog.alertRuleTypes,
    "GET /gitops/kinds": catalog.gitopsKinds,
    "GET /volbackups/info": catalog.volbackupInfo,
    "GET /autoupdate/settings": catalog.autoupdateSettings,
    "GET /sso/settings": catalog.ssoSettings,
    "GET /templates": catalog.templates,
    "GET /users": Object.values(fx.users),
    "GET /teams": fx.teams,
    "GET /apikeys": fx.apiKeys,
    "GET /alerts": [],
    "GET /capabilities": {},
    "GET /system/version": { version: "0.1.0" },
  });
}

let lastLocation = "";
function LocationProbe() {
  const loc = useLocation();
  lastLocation = loc.pathname + loc.search;
  return null;
}

/** The router location after the last render/navigation. */
export function currentPath(): string {
  return lastLocation;
}

export function newQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0, gcTime: 0 }, mutations: { retry: false } },
  });
  clients.push(client);
  return client;
}

export function Providers({ children, path = "/", client }: { children: ReactNode; path?: string; client?: QueryClient }) {
  return (
    <ThemeProvider>
      <TooltipProvider>
        <QueryClientProvider client={client ?? newQueryClient()}>
          <MemoryRouter initialEntries={[path]}>
            <AuthProvider>
              <HostProvider>
                {children}
                <Routes>
                  <Route path="*" element={<LocationProbe />} />
                </Routes>
              </HostProvider>
            </AuthProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </TooltipProvider>
      <Toaster />
    </ThemeProvider>
  );
}

export interface AppRender extends RenderResult {
  user: ReturnType<typeof userEvent.setup>;
}

/**
 * Render the whole app at ``path``; ``role: null`` renders signed out. ``api``
 * routes override the defaults before the first request goes out.
 */
export function renderApp(path: string, opts: { role?: Role | null; api?: Record<string, Handler> } = {}): AppRender {
  const role = opts.role === undefined ? "admin" : opts.role;
  if (role) {
    localStorage.setItem(TOKEN_KEY, "test-token");
    baseApi(role);
  } else {
    baseApi("admin");
  }
  if (opts.api) mockApi(opts.api);
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  const result = render(
    <Providers path={path}>
      <App />
    </Providers>,
  );
  return { ...result, user };
}

/** Render a single component inside the providers (no App routes). */
export function renderWithProviders(ui: ReactElement, path = "/"): AppRender {
  localStorage.setItem(TOKEN_KEY, "test-token");
  baseApi("admin");
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  return { ...render(<Providers path={path}>{ui}</Providers>), user };
}

/** Wait for the page heading (the shell renders before the lazy page). */
export async function heading(name: RegExp | string): Promise<HTMLElement> {
  return screen.findByRole("heading", { name }, { timeout: 10_000 });
}

/** The page area (the innermost <main>, inside the app shell). */
export function pageRoot(): HTMLElement {
  const mains = document.querySelectorAll("main");
  return (mains[mains.length - 1] as HTMLElement) ?? document.body;
}
