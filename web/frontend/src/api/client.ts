import { createHttp } from "./http";
import type {
  Alert,
  AlertRule,
  AlertRuleType,
  AlertRuleTypeSpec,
  APIKey,
  AuditEntry,
  BuildOptions,
  BuildResult,
  Connection,
  ConnectionType,
  Container,
  ContainerActionType,
  ContainerInspect,
  ContainerSpec,
  Deployment,
  DeploymentService,
  DeploymentSyncResult,
  DeploymentTarget,
  DiskUsage,
  EnrollResponse,
  Host,
  HostSwarm,
  Image,
  ImageInspectResult,
  LoginResponse,
  MetricSample,
  Network,
  NetworkCreateOptions,
  Notification,
  NotificationChannel,
  VolumePluginType,
  PluginInstallOptions,
  Registry,
  ReferencePreview,
  SecretProviderType,
  RegistryTestResult,
  Secret,
  SystemInfo,
  TriggerSpec,
  User,
  Volume,
  VolumeCreateOptions,
  VolumeFile,
  Workflow,
  WorkflowRun,
} from "../types";

export type PruneKind = "containers" | "images" | "volumes" | "networks";

export const TOKEN_KEY = "kowan_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export const api = createHttp({
  baseURL: "/api/v1",
  authHeader: () => {
    const token = getToken();
    return token ? `Bearer ${token}` : null;
  },
  onUnauthorized: () => {
    clearToken();
    if (window.location.pathname !== "/login") {
      window.location.assign("/login");
    }
  },
});

// ---------------------------------------------------------------------------
// Auth & users
// ---------------------------------------------------------------------------

export async function login(username: string, password: string): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>("/auth/login", { username, password });
  return data;
}

/** Whether the control plane has been initialized (has any user). Public. */
export async function getAuthStatus(): Promise<{ initialized: boolean }> {
  const { data } = await api.get<{ initialized: boolean }>("/auth/status");
  return data;
}

/** Revoke the current token on the server (best effort: the SPA forgets it anyway). */
export function revokeSession(): void {
  // Read the token now: the caller clears it right after this returns.
  const token = getToken();
  if (!token) return;
  void fetch("/api/v1/auth/logout", { method: "POST", headers: { Authorization: `Bearer ${token}` } })
    // Already expired or offline: nothing left to revoke.
    .catch(() => undefined);
}

/** First-run setup: create the initial admin. Returns a token + user like login. */
export async function initAdmin(
  username: string,
  password: string,
  setupToken: string,
): Promise<LoginResponse> {
  const { data } = await api.post<LoginResponse>("/auth/init", { username, password, setupToken });
  return data;
}

export async function getMe(): Promise<User> {
  const { data } = await api.get<User>("/me");
  return data;
}

export async function listUsers(): Promise<User[]> {
  const { data } = await api.get<User[]>("/users");
  return data;
}

export async function createUser(body: {
  username: string;
  password: string;
  role: string;
  email?: string;
}): Promise<User> {
  const { data } = await api.post<User>("/users", body);
  return data;
}

export async function updateUser(
  id: string,
  body: Partial<{ username: string; password: string; role: string; email: string }>,
): Promise<User> {
  const { data } = await api.put<User>(`/users/${id}`, body);
  return data;
}

/** Change your own password. The server ends your other sessions and hands
 *  back a token for this one, so the tab stays signed in. */
export async function changeOwnPassword(id: string, password: string): Promise<{ user: User; token: string | null }> {
  const res = await api.put<User>(`/users/${id}`, { password });
  return { user: res.data, token: res.headers["x-session-token"] ?? null };
}

export interface NetworkAccess {
  env: { uiAllow: string[]; edgeAllow: string[]; trustedProxies: string[]; proxyHeader: string };
  uiAllow: string[];
  edgeAllow: string[];
  yourAddress: string;
  peerAddress: string;
  warnings?: string[];
}

export async function getNetworkAccess(): Promise<NetworkAccess> {
  const { data } = await api.get<NetworkAccess>("/settings/network");
  return data;
}

export async function saveNetworkAccess(body: { uiAllow: string[]; edgeAllow: string[]; password?: string }): Promise<NetworkAccess> {
  const { data } = await api.put<NetworkAccess>("/settings/network", body);
  return data;
}

export async function deleteUser(id: string): Promise<void> {
  await api.delete(`/users/${id}`);
}


// ---------------------------------------------------------------------------
// API keys (admin only)
//
// Admin-managed, role-scoped keys that authenticate API requests (kowanctl,
// webhook scripts, CI) without a login. Keys are stored hashed: the list/create
// endpoints never return the full secret except createScopedApiKey, which returns it
// EXACTLY ONCE at creation. Afterwards only the `prefix` is shown.
// ---------------------------------------------------------------------------

export async function listApiKeys(): Promise<APIKey[]> {
  const { data } = await api.get<APIKey[]>("/apikeys");
  return data;
}

export async function deleteApiKey(id: string): Promise<void> {
  await api.delete(`/apikeys/${id}`);
}

// ---------------------------------------------------------------------------
// Hosts
// ---------------------------------------------------------------------------

export async function listHosts(): Promise<Host[]> {
  const { data } = await api.get<Host[]>("/hosts");
  return data;
}

export async function createHost(body: {
  name: string;
  mode?: string;
  address?: string;
}): Promise<EnrollResponse> {
  const { data } = await api.post<EnrollResponse>("/hosts", body);
  return data;
}

export async function getEdgeCommand(hostId: string): Promise<EnrollResponse> {
  const { data } = await api.get<EnrollResponse>(`/hosts/${hostId}/edge-command`);
  return data;
}

/**
 * Fetch a host's Docker Swarm topology. `active` is true only when the host is a
 * reachable swarm manager; otherwise `nodes` is empty (a non-manager cannot list
 * the cluster's nodes).
 */
export async function getHostSwarm(hostId: string): Promise<HostSwarm> {
  const { data } = await api.get<HostSwarm>(`/hosts/${hostId}/swarm`);
  return data;
}

export async function renameHost(id: string, name: string): Promise<Host> {
  const { data } = await api.patch<Host>(`/hosts/${id}`, { name });
  return data;
}

export async function deleteHost(id: string): Promise<void> {
  await api.delete(`/hosts/${id}`);
}

// ---------------------------------------------------------------------------
// Registries (admin only)
// ---------------------------------------------------------------------------

// A credential row as returned by the unified /credentials API. Registries,
// secrets and notification channels are all stored as one record type
// discriminated by `kind`; the non-sensitive per-kind attributes live in `meta`,
// the sensitive values are never returned (only field NAMES via `configKeys`,
// and — for registry/channel — a masked `config`). The client maps these back to
// the kind-specific Registry / Secret / NotificationChannel shapes so callers and
// the pickers keep their existing contract.
interface RawCredential {
  id: string;
  kind: string;
  type: string;
  name: string;
  meta: Record<string, unknown>;
  configKeys?: string[];
  config?: Record<string, string>;
  createdAt: string;
  updatedAt: string;
}

function toRegistry(c: RawCredential): Registry {
  const m = c.meta ?? {};
  return {
    id: c.id,
    name: c.name,
    type: c.type,
    url: String(m.url ?? ""),
    username: String(m.username ?? ""),
    hasPassword: Boolean(m.hasPassword),
    insecure: Boolean(m.insecure),
    verified: Boolean(m.verified),
    lastError: m.lastError ? String(m.lastError) : undefined,
    config: c.config,
    configKeys: c.configKeys,
    connectionRef: m.connectionRef ? String(m.connectionRef) : undefined,
    createdAt: c.createdAt,
  };
}

export async function listRegistries(): Promise<Registry[]> {
  const { data } = await api.get<RawCredential[]>("/credentials", {
    params: { kind: "registry" },
  });
  return data.map(toRegistry);
}

export async function createRegistry(body: {
  name: string;
  url: string;
  username?: string;
  password?: string;
  type: string;
  config?: Record<string, string>;
  // When delegating auth to a shared connection, send only the connection id
  // (and omit username/password/config).
  connectionRef?: string;
}): Promise<Registry> {
  const { data } = await api.post<RawCredential>("/credentials", {
    kind: "registry",
    ...body,
  });
  return toRegistry(data);
}

export async function updateRegistry(
  id: string,
  body: Partial<{
    name: string;
    url: string;
    username: string;
    password: string;
    type: string;
    // Sensitive config fields left blank keep the stored value.
    config: Record<string, string>;
    // When delegating auth to a shared connection, send only the connection id.
    connectionRef: string;
  }>,
): Promise<Registry> {
  const { data } = await api.put<RawCredential>(`/credentials/${id}`, body);
  return toRegistry(data);
}

export async function deleteRegistry(id: string): Promise<void> {
  await api.delete(`/credentials/${id}`);
}

/** Run a connectivity/credentials test and return the validation result. */
export async function testRegistry(id: string): Promise<RegistryTestResult> {
  const { data } = await api.post<RegistryTestResult>(`/credentials/${id}/test`);
  return data;
}

/** List repositories in a registry (v2 _catalog). Unsupported on Docker Hub. */
export async function registryCatalog(id: string): Promise<string[]> {
  const { data } = await api.get<{ repositories: string[] }>(
    `/credentials/${id}/catalog`,
  );
  return data.repositories ?? [];
}

/** List the tags of a repository in a registry (v2 tags/list). */
export async function registryTags(
  id: string,
  repo: string,
): Promise<string[]> {
  const { data } = await api.get<{ tags: string[] }>(
    `/credentials/${id}/tags`,
    { params: { repo } },
  );
  return data.tags ?? [];
}

// ---------------------------------------------------------------------------
// Connections (admin only)
//
// A connection is a reusable, typed credential (aws/azure/gcp/ghcr/vault/smtp/
// slack/…) that other subsystems reference by id. Stored under the unified
// /credentials endpoint with kind="connection"; sensitive config values are
// masked on read (only field NAMES via configKeys), blank-on-update preserves.
// ---------------------------------------------------------------------------

function toConnection(c: RawCredential): Connection {
  return {
    id: c.id,
    type: c.type,
    name: c.name,
    meta: c.meta ?? {},
    configKeys: c.configKeys ?? [],
    config: c.config,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
  };
}

export async function listConnections(): Promise<Connection[]> {
  const { data } = await api.get<RawCredential[]>("/credentials", {
    params: { kind: "connection" },
  });
  return data.map(toConnection);
}

/** Per-type capability + field schemas used to drive the connection form. */
export async function connectionTypes(): Promise<ConnectionType[]> {
  const { data } = await api.get<ConnectionType[]>("/credentials/types", {
    params: { kind: "connection" },
  });
  return data;
}

export async function createConnection(body: {
  type: string;
  name: string;
  config: Record<string, string>;
}): Promise<Connection> {
  const { data } = await api.post<RawCredential>("/credentials", {
    kind: "connection",
    ...body,
  });
  return toConnection(data);
}

export async function updateConnection(
  id: string,
  body: Partial<{
    name: string;
    // Sensitive fields left blank keep the stored value.
    config: Record<string, string>;
  }>,
): Promise<Connection> {
  const { data } = await api.put<RawCredential>(`/credentials/${id}`, body);
  return toConnection(data);
}

export async function deleteConnection(id: string): Promise<void> {
  await api.delete(`/credentials/${id}`);
}

/**
 * List the repositories reachable through a registry connection (via the
 * registry v2 `/v2/_catalog` endpoint, using the connection's auth). Best-effort:
 * the backend returns an empty list rather than erroring when the registry does
 * not support catalog (e.g. Docker Hub, or a 404/401). Operator role required.
 */
export async function listRegistryRepositories(
  connectionId: string,
): Promise<string[]> {
  const { data } = await api.get<{ repositories: string[] }>(
    `/credentials/${connectionId}/registry/repositories`,
  );
  return data.repositories ?? [];
}

/**
 * List the tags of a repository reachable through a registry connection (via the
 * registry v2 `/v2/<repo>/tags/list` endpoint, using the connection's auth).
 * Best-effort: the backend returns an empty list on failure. Operator role
 * required.
 */
export async function listRegistryTags(
  connectionId: string,
  repository: string,
): Promise<string[]> {
  const { data } = await api.get<{ tags: string[] }>(
    `/credentials/${connectionId}/registry/tags`,
    { params: { repository } },
  );
  return data.tags ?? [];
}

// ---------------------------------------------------------------------------
// Secrets (admin only)
//
// Secret values are write-only: the `fields` map (field name -> value) is sent
// on create/update but never returned. The list/get endpoints return metadata
// only (with `fields` as the list of field names). Decrypted values are
// available exclusively via revealSecret().
// ---------------------------------------------------------------------------

function toSecret(c: RawCredential): Secret {
  const m = c.meta ?? {};
  return {
    id: c.id,
    name: c.name,
    type: c.type,
    description: String(m.description ?? ""),
    fields: c.configKeys ?? [],
    connectionRef: m.connectionRef ? String(m.connectionRef) : undefined,
    deploymentId: m.deploymentId ? String(m.deploymentId) : undefined,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
  };
}

export async function listSecrets(): Promise<Secret[]> {
  const { data } = await api.get<RawCredential[]>("/credentials", {
    params: { kind: "secret" },
  });
  return data.map(toSecret);
}

export async function createSecret(body: {
  name: string;
  type: string;
  description: string;
  fields: Record<string, string>;
  // When delegating the external backend to a shared connection, send the
  // connection id; the secret then carries only its ref-specific selector
  // fields and omits the backend's sensitive fields.
  connectionRef?: string;
}): Promise<Secret> {
  const { data } = await api.post<RawCredential>("/credentials", {
    kind: "secret",
    ...body,
  });
  return toSecret(data);
}

export async function updateSecret(
  id: string,
  body: Partial<{
    name: string;
    type: string;
    description: string;
    // An empty fields map keeps the stored values untouched.
    fields: Record<string, string>;
    // When delegating the external backend to a shared connection, send the
    // connection id (or "" to revert to inline fields).
    connectionRef: string;
  }>,
): Promise<Secret> {
  const { data } = await api.put<RawCredential>(`/credentials/${id}`, body);
  return toSecret(data);
}

export async function deleteSecret(id: string): Promise<void> {
  await api.delete(`/credentials/${id}`);
}

export async function revealSecret(
  id: string,
): Promise<{ fields: Record<string, string> }> {
  const { data } = await api.post<{ fields: Record<string, string> }>(
    `/credentials/${id}/reveal`,
  );
  return data;
}

/**
 * Resolve the [[...]] reference tokens in arbitrary text, returning a masked,
 * per-token preview (admin-only). External/plugin-backed secrets are fetched
 * live; resolved values are never returned in clear.
 */
export async function resolveReferences(
  text: string,
): Promise<ReferencePreview[]> {
  const { data } = await api.post<{ references: ReferencePreview[] }>(
    "/credentials/resolve",
    { text },
  );
  return data.references ?? [];
}

// ---------------------------------------------------------------------------
// Containers
// ---------------------------------------------------------------------------

export async function listContainers(hostId: string, all = true): Promise<Container[]> {
  const { data } = await api.get<Container[]>(`/hosts/${hostId}/containers`, {
    params: { all },
  });
  return data;
}

export async function createContainer(
  hostId: string,
  name: string,
  spec: ContainerSpec,
  registryId?: string,
): Promise<{ id: string }> {
  const { data } = await api.post<{ id: string }>(
    `/hosts/${hostId}/containers`,
    { ...spec, name, ...(registryId ? { registryId } : {}) },
  );
  return data;
}

export async function pauseContainer(hostId: string, id: string): Promise<void> {
  await api.post(`/hosts/${hostId}/containers/${id}/pause`);
}

export async function unpauseContainer(hostId: string, id: string): Promise<void> {
  await api.post(`/hosts/${hostId}/containers/${id}/unpause`);
}

export async function inspectContainer(
  hostId: string,
  id: string,
): Promise<ContainerInspect> {
  const { data } = await api.get<ContainerInspect>(`/hosts/${hostId}/containers/${id}`);
  return data;
}

/**
 * Reconstruct a ContainerSpec from an existing (running or stopped) container.
 * Used to prefill the spec form for an "edit & recreate" flow: the user edits
 * the returned spec and POSTs it back with the same name to recreate the
 * container in place (createContainer already removes any same-named container).
 */
export async function getContainerSpec(
  hostId: string,
  id: string,
): Promise<ContainerSpec> {
  const { data } = await api.get<ContainerSpec>(
    `/hosts/${hostId}/containers/${id}/spec`,
  );
  return data;
}

export async function containerAction(
  hostId: string,
  id: string,
  action: ContainerActionType,
): Promise<void> {
  await api.post(`/hosts/${hostId}/containers/${id}/${action}`);
}

export async function removeContainer(
  hostId: string,
  id: string,
  force = false,
): Promise<void> {
  await api.delete(`/hosts/${hostId}/containers/${id}`, { params: { force } });
}

/**
 * Assemble a ws:// or wss:// URL for an /api/v1 endpoint. Browsers cannot set
 * custom headers on a WS handshake, so the auth token is injected as a `token`
 * query parameter here; each builder only supplies its endpoint-specific params.
 */
function wsUrl(path: string, params?: URLSearchParams): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const p = params ?? new URLSearchParams();
  const token = getToken() ?? "";
  if (token) p.set("token", token);
  const qs = p.toString();
  return `${proto}//${window.location.host}/api/v1${path}${qs ? `?${qs}` : ""}`;
}

/**
 * Build a ws:// or wss:// URL for the container logs websocket.
 * Browsers cannot set custom headers on a WS handshake, so the token is passed
 * as a query parameter. The backend may read it from there; if not, callers
 * should degrade gracefully.
 */
export function logsWebSocketURL(
  hostId: string,
  id: string,
  opts: { tail?: number; follow?: boolean } = {},
): string {
  const params = new URLSearchParams();
  if (opts.tail !== undefined) params.set("tail", String(opts.tail));
  if (opts.follow !== undefined) params.set("follow", String(opts.follow));
  return wsUrl(`/hosts/${hostId}/containers/${id}/logs`, params);
}

/**
 * Build a ws:// or wss:// URL for the live container stats websocket. Each
 * message is one JSON ContainerStats sample (~1/s).
 */
export function statsStreamWebSocketURL(hostId: string, id: string): string {
  return wsUrl(`/hosts/${hostId}/containers/${id}/stats/stream`);
}

/**
 * Build a ws:// or wss:// URL for the interactive container exec websocket.
 * The token, the command (repeatable `cmd` param) and the TTY flag are passed
 * as query parameters since browsers cannot set WS handshake headers.
 */
export function execWebSocketURL(
  hostId: string,
  id: string,
  cmd: string[] = ["/bin/sh"],
  tty = true,
): string {
  const params = new URLSearchParams();
  params.set("tty", String(tty));
  for (const c of cmd) params.append("cmd", c);
  return wsUrl(`/hosts/${hostId}/containers/${id}/exec`, params);
}

// ---------------------------------------------------------------------------
// Images
// ---------------------------------------------------------------------------

export async function listImages(hostId: string): Promise<Image[]> {
  const { data } = await api.get<Image[]>(`/hosts/${hostId}/images`);
  return data;
}

/** Inspect an image: full inspect JSON + build history. The image id is a
 * `sha256:<hex>` reference (no slashes); it is sent unescaped because the router
 * does not decode a percent-encoded colon. */
export async function inspectImage(
  hostId: string,
  id: string,
): Promise<ImageInspectResult> {
  const { data } = await api.get<ImageInspectResult>(
    `/hosts/${hostId}/images/${id}/inspect`,
  );
  return data;
}

export async function pullImage(
  hostId: string,
  ref: string,
  registryId?: string,
): Promise<void> {
  await api.post(`/hosts/${hostId}/images/pull`, {
    ref,
    ...(registryId ? { registryId } : {}),
  });
}

export async function removeImage(
  hostId: string,
  id: string,
  force = false,
): Promise<void> {
  await api.delete(`/hosts/${hostId}/images/${id}`, { params: { force } });
}

/**
 * Build an image on a host from an inline Dockerfile or a git repo and
 * optionally push it. The endpoint returns 200 with the BuildResult even when
 * the build fails (success=false) so the UI can show the build log either way.
 */
export async function buildImage(
  hostId: string,
  opts: BuildOptions,
): Promise<BuildResult> {
  const { data } = await api.post<BuildResult>(`/hosts/${hostId}/build`, opts);
  return data;
}

// ---------------------------------------------------------------------------
// Volumes
// ---------------------------------------------------------------------------

export async function listVolumes(hostId: string): Promise<Volume[]> {
  const { data } = await api.get<Volume[]>(`/hosts/${hostId}/volumes`);
  return data;
}

export async function createVolume(
  hostId: string,
  body: VolumeCreateOptions,
): Promise<Volume> {
  const { data } = await api.post<Volume>(`/hosts/${hostId}/volumes`, body);
  return data;
}

export async function removeVolume(hostId: string, name: string): Promise<void> {
  await api.delete(`/hosts/${hostId}/volumes/${encodeURIComponent(name)}`);
}

/** Inspect a volume (raw `docker volume inspect` JSON). */
export async function inspectVolume(
  hostId: string,
  name: string,
): Promise<Record<string, unknown>> {
  const { data } = await api.get<Record<string, unknown>>(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/inspect`,
  );
  return data;
}

/** List the immediate entries at a path within a volume. */
export async function browseVolume(
  hostId: string,
  name: string,
  path: string,
): Promise<VolumeFile[]> {
  const { data } = await api.get<VolumeFile[]>(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/browse`,
    { params: { path } },
  );
  return data;
}

/** Read a single file's contents (as text) from within a volume. */
export async function readVolumeFile(
  hostId: string,
  name: string,
  path: string,
): Promise<string> {
  const { data } = await api.get(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/file`,
    { params: { path }, responseType: "text" },
  );
  return data as string;
}

/** Download a single file's raw bytes from within a volume as a Blob. */
export async function downloadVolumeFile(
  hostId: string,
  name: string,
  path: string,
): Promise<Blob> {
  const { data } = await api.get<Blob>(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/download`,
    { params: { path }, responseType: "blob" },
  );
  return data;
}

/** Download a zip archive of a file or directory within a volume as a Blob. */
export async function downloadVolumeArchive(
  hostId: string,
  name: string,
  path: string,
): Promise<Blob> {
  const { data } = await api.get<Blob>(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/archive`,
    { params: { path }, responseType: "blob" },
  );
  return data;
}

/** Upload a single file into a directory within a volume. */
export async function uploadVolumeFile(
  hostId: string,
  name: string,
  dir: string,
  file: File,
): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  await api.post(
    `/hosts/${hostId}/volumes/${encodeURIComponent(name)}/upload`,
    form,
    { params: { path: dir } },
  );
}

/** Trigger a browser "save as" for a Blob under the given file name. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Networks
// ---------------------------------------------------------------------------

export async function listNetworks(hostId: string): Promise<Network[]> {
  const { data } = await api.get<Network[]>(`/hosts/${hostId}/networks`);
  return data;
}

export async function createNetwork(
  hostId: string,
  body: NetworkCreateOptions,
): Promise<Network> {
  const { data } = await api.post<Network>(`/hosts/${hostId}/networks`, body);
  return data;
}

export async function inspectNetwork(
  hostId: string,
  id: string,
): Promise<Network> {
  const { data } = await api.get<Network>(`/hosts/${hostId}/networks/${id}`);
  return data;
}

export async function connectContainer(
  hostId: string,
  networkId: string,
  containerId: string,
): Promise<void> {
  await api.post(`/hosts/${hostId}/networks/${networkId}/connect`, {
    containerId,
  });
}

export async function disconnectContainer(
  hostId: string,
  networkId: string,
  containerId: string,
  force = false,
): Promise<void> {
  await api.post(`/hosts/${hostId}/networks/${networkId}/disconnect`, {
    containerId,
    force,
  });
}

export async function removeNetwork(hostId: string, id: string): Promise<void> {
  await api.delete(`/hosts/${hostId}/networks/${id}`);
}

// ---------------------------------------------------------------------------
// System
// ---------------------------------------------------------------------------

export async function getSystem(hostId: string): Promise<SystemInfo> {
  const { data } = await api.get<SystemInfo>(`/hosts/${hostId}/system`);
  return data;
}

export async function prune(
  hostId: string,
  kind: PruneKind,
  all?: boolean,
): Promise<{ spaceReclaimed: number }> {
  const { data } = await api.post<{ spaceReclaimed: number }>(
    `/hosts/${hostId}/prune/${kind}`,
    null,
    kind === "images" && all !== undefined ? { params: { all } } : undefined,
  );
  return data;
}

export async function getDiskUsage(hostId: string): Promise<DiskUsage> {
  const { data } = await api.get<DiskUsage>(`/hosts/${hostId}/df`);
  return data;
}

// ---------------------------------------------------------------------------
// Metrics (time-series)
//
// Per-host samples recorded by the monitor each tick. `since` is an RFC3339
// timestamp; when omitted the server defaults to the last hour. Samples are
// returned ordered by time ascending.
// ---------------------------------------------------------------------------

export async function getMetrics(
  hostId: string,
  since?: string,
): Promise<MetricSample[]> {
  const { data } = await api.get<MetricSample[]>(`/hosts/${hostId}/metrics`, {
    params: since ? { since } : undefined,
  });
  return data;
}

// Field schema of the external secret backends (vault/infisical/...), used by
// the secret create form.
export async function listSecretTypes(): Promise<SecretProviderType[]> {
  const { data } = await api.get<SecretProviderType[]>("/credentials/types", {
    params: { kind: "secret" },
  });
  return data;
}

// ---------------------------------------------------------------------------
// Host (Docker managed) plugins — volume drivers such as s3fs/sshfs
// ---------------------------------------------------------------------------

// Built-in catalog of supported volume-driver types the volume UI offers as a
// pick-list (host-independent).
export async function listVolumePluginTypes(): Promise<VolumePluginType[]> {
  const { data } = await api.get<VolumePluginType[]>("/volume-plugins");
  return data;
}

export async function installHostPlugin(
  hostId: string,
  opts: PluginInstallOptions,
): Promise<void> {
  await api.post(`/hosts/${hostId}/plugins/install`, opts);
}

// ---------------------------------------------------------------------------
// Deployments
// ---------------------------------------------------------------------------

export type DeploymentInput = Omit<
  Deployment,
  "id" | "status" | "protected" | "createdAt" | "updatedAt"
>;

export async function listDeployments(): Promise<Deployment[]> {
  const { data } = await api.get<Deployment[]>("/deployments");
  return data;
}

export async function getDeployment(id: string): Promise<Deployment> {
  const { data } = await api.get<Deployment>(`/deployments/${id}`);
  return data;
}

/** Saved versions of a deployment, newest first. */
export async function listDeploymentRevisions(id: string): Promise<import("../types").DeploymentRevision[]> {
  return (await api.get<import("../types").DeploymentRevision[]>(`/deployments/${id}/revisions`)).data;
}

/** Line diff from revision ``against`` (default: the previous one) to ``n``. */
export async function diffDeploymentRevision(id: string, n: number, against?: number): Promise<import("../types").DeploymentRevisionDiff> {
  return (await api.get<import("../types").DeploymentRevisionDiff>(`/deployments/${id}/revisions/${n}/diff`, {
    params: against ? { against } : undefined,
  })).data;
}

/** Restore revision ``n`` as a new revision; redeploys unless ``deploy`` is false. */
export async function rollbackDeployment(id: string, n: number, deploy: boolean): Promise<Deployment> {
  return (await api.post<Deployment>(`/deployments/${id}/revisions/${n}/rollback`, { deploy })).data;
}

export async function listDeploymentPreviews(id: string): Promise<Deployment[]> {
  return (await api.get<Deployment[]>(`/deployments/${id}/previews`)).data;
}

/** Create (or redeploy) the preview of ``branch``. */
export async function createDeploymentPreview(id: string, branch: string): Promise<Deployment> {
  return (await api.post<Deployment>(`/deployments/${id}/previews`, { branch })).data;
}

/** Set (or clear with "") the deployment ``id`` promotes to. */
export async function setDeploymentPromotion(id: string, to: string): Promise<Deployment> {
  return (await api.put<Deployment>(`/deployments/${id}/promotion`, { to })).data;
}

export async function getPromotionPlan(id: string, to?: string): Promise<import("../types").PromotionPlan> {
  return (await api.get<import("../types").PromotionPlan>(`/deployments/${id}/promote`, { params: to ? { to } : undefined })).data;
}

export async function promoteDeployment(id: string, to: string, deploy: boolean): Promise<Deployment> {
  return (await api.post<Deployment>(`/deployments/${id}/promote`, { to, deploy })).data;
}

export async function createDeployment(
  body: DeploymentInput,
): Promise<Deployment> {
  const { data } = await api.post<Deployment>("/deployments", body);
  return data;
}

export async function updateDeployment(
  id: string,
  body: DeploymentInput,
): Promise<Deployment> {
  const { data } = await api.put<Deployment>(`/deployments/${id}`, body);
  return data;
}

export async function deleteDeployment(id: string): Promise<void> {
  await api.delete(`/deployments/${id}`);
}

/** Toggle deletion protection on a deployment. */
export async function setDeploymentProtection(
  id: string,
  protectedOn: boolean,
): Promise<Deployment> {
  const { data } = await api.put<Deployment>(`/deployments/${id}/protection`, {
    protected: protectedOn,
  });
  return data;
}

/**
 * Toggle the desired-state (keep-live) switch. When enabled, the reconcile loop
 * redeploys a target that goes down; when disabled, the deployment is left alone.
 */
export async function setDeploymentEnabled(
  id: string,
  enabled: boolean,
): Promise<Deployment> {
  const { data } = await api.put<Deployment>(`/deployments/${id}/enabled`, {
    enabled,
  });
  return data;
}

export async function deployDeployment(id: string): Promise<void> {
  await api.post(`/deployments/${id}/deploy`);
}

export async function stopDeployment(id: string): Promise<void> {
  await api.post(`/deployments/${id}/stop`);
}

export async function startDeployment(id: string): Promise<void> {
  await api.post(`/deployments/${id}/start`);
}

export async function destroyDeployment(id: string): Promise<void> {
  await api.post(`/deployments/${id}/destroy`);
}

export async function deploymentStatus(
  id: string,
): Promise<DeploymentTarget[]> {
  const { data } = await api.get<DeploymentTarget[]>(
    `/deployments/${id}/status`,
  );
  return data;
}

/**
 * Per-service runtime status for a deployment on a given host. For container
 * deployments this is a single entry; for compose deployments it is one entry
 * per compose service.
 */
export async function deploymentServices(
  id: string,
  hostId: string,
): Promise<DeploymentService[]> {
  const { data } = await api.get<DeploymentService[]>(
    `/deployments/${id}/services`,
    { params: { host: hostId } },
  );
  return data;
}

/**
 * Generate (or rotate) the per-deployment redeploy webhook token. Returns the
 * token and the public hook path (e.g. "/hooks/deploy/{token}").
 */
export async function createDeployHook(
  id: string,
): Promise<{ token: string; url: string }> {
  const { data } = await api.post<{ token: string; url: string }>(
    `/deployments/${id}/webhook`,
  );
  return data;
}

/** Clear the per-deployment redeploy webhook token. */
export async function deleteDeployHook(id: string): Promise<void> {
  await api.delete(`/deployments/${id}/webhook`);
}

/**
 * Build a ws:// or wss:// URL for the deployment logs websocket.
 * Browsers cannot set custom headers on a WS handshake, so the token and the
 * target host are passed as query parameters.
 */
export function deploymentLogsWebSocketURL(
  id: string,
  hostId: string,
  opts: { tail?: number; follow?: boolean; container?: string } = {},
): string {
  const params = new URLSearchParams();
  params.set("host", hostId);
  if (opts.container) params.set("container", opts.container);
  if (opts.tail !== undefined) params.set("tail", String(opts.tail));
  if (opts.follow !== undefined) params.set("follow", String(opts.follow));
  return wsUrl(`/deployments/${id}/logs`, params);
}

// ---------------------------------------------------------------------------
// Alerts & alert rules
// ---------------------------------------------------------------------------

export async function listAlerts(limit = 100): Promise<Alert[]> {
  const { data } = await api.get<Alert[]>("/alerts", { params: { limit } });
  return data;
}

export async function resolveAlert(id: string): Promise<void> {
  await api.post(`/alerts/${id}/resolve`);
}

export async function listAlertRules(): Promise<AlertRule[]> {
  const { data } = await api.get<AlertRule[]>("/alert-rules");
  return data;
}

export async function alertRuleTypes(): Promise<AlertRuleTypeSpec[]> {
  const { data } = await api.get<AlertRuleTypeSpec[]>("/alert-rule-types");
  return data;
}

// The full set of writable rule fields. Create sends them all; update sends a
// partial (the backend leaves any omitted field untouched).
export interface AlertRuleInput {
  name: string;
  type: AlertRuleType;
  operator: string;
  threshold: number;
  severity: string;
  forDuration: number;
  hostId: string;
  match: string;
  labelKey: string;
  labelValue: string;
  channelId: string;
  autoResolve: boolean;
  enabled: boolean;
}

export async function createAlertRule(
  body: Partial<AlertRuleInput>,
): Promise<AlertRule> {
  const { data } = await api.post<AlertRule>("/alert-rules", body);
  return data;
}

export async function updateAlertRule(
  id: string,
  body: Partial<AlertRuleInput>,
): Promise<AlertRule> {
  const { data } = await api.put<AlertRule>(`/alert-rules/${id}`, body);
  return data;
}

export async function deleteAlertRule(id: string): Promise<void> {
  await api.delete(`/alert-rules/${id}`);
}

// ---------------------------------------------------------------------------
// Notification center (the top-bar bell)
// ---------------------------------------------------------------------------

export async function listNotifications(limit = 50): Promise<Notification[]> {
  const { data } = await api.get<Notification[]>("/notifications", {
    params: { limit },
  });
  return data;
}

export async function unreadNotificationCount(): Promise<number> {
  const { data } = await api.get<{ count: number }>(
    "/notifications/unread-count",
  );
  return data.count;
}

export async function markNotificationRead(id: string): Promise<void> {
  await api.post(`/notifications/${id}/read`);
}

export async function markAllNotificationsRead(): Promise<void> {
  await api.post("/notifications/read-all");
}

// ---------------------------------------------------------------------------
// Notification channels (admin only)
//
// Channel config is write-only for sensitive fields: the full `config` map is
// sent on create/update, but list/get return non-sensitive values in clear and
// sensitive ones masked (with `configKeys` listing the populated keys). On
// update, leaving a sensitive value blank keeps the stored value.
// ---------------------------------------------------------------------------

function toChannel(c: RawCredential): NotificationChannel {
  const m = c.meta ?? {};
  return {
    id: c.id,
    name: c.name,
    type: c.type,
    enabled: Boolean(m.enabled),
    minLevel: String(m.minLevel ?? ""),
    configKeys: c.configKeys ?? [],
    config: c.config ?? {},
    connectionRef: m.connectionRef ? String(m.connectionRef) : undefined,
    createdAt: c.createdAt,
    updatedAt: c.updatedAt,
  };
}

export async function listChannels(): Promise<NotificationChannel[]> {
  const { data } = await api.get<RawCredential[]>("/credentials", {
    params: { kind: "channel" },
  });
  return data.map(toChannel);
}

export async function createChannel(body: {
  name: string;
  type: string;
  enabled: boolean;
  minLevel: string;
  config: Record<string, string>;
  // When delegating the transport to a shared connection, send the connection
  // id; the channel then omits the transport's sensitive config fields.
  connectionRef?: string;
}): Promise<NotificationChannel> {
  const { data } = await api.post<RawCredential>("/credentials", {
    kind: "channel",
    ...body,
  });
  return toChannel(data);
}

export async function updateChannel(
  id: string,
  body: Partial<{
    name: string;
    type: string;
    enabled: boolean;
    minLevel: string;
    // Sensitive fields left blank keep the stored value.
    config: Record<string, string>;
    // When delegating the transport to a shared connection, send the connection
    // id (or "" to revert to inline config).
    connectionRef: string;
  }>,
): Promise<NotificationChannel> {
  const { data } = await api.put<RawCredential>(`/credentials/${id}`, body);
  return toChannel(data);
}

export async function deleteChannel(id: string): Promise<void> {
  await api.delete(`/credentials/${id}`);
}

export async function testChannel(id: string): Promise<{ ok: boolean }> {
  const { data } = await api.post<{ ok: boolean }>(`/credentials/${id}/test`);
  return data;
}

// ---------------------------------------------------------------------------
// Workflows (automation / procedures)
//
// A workflow is a named, ordered list of typed steps run sequentially with run
// history and per-step output. Triggers: manual ("Run now"), a public webhook
// token, or a cron schedule.
// ---------------------------------------------------------------------------

export type WorkflowInput = Omit<
  Workflow,
  "id" | "runToken" | "protected" | "createdAt" | "updatedAt"
>;

export async function listWorkflows(): Promise<Workflow[]> {
  const { data } = await api.get<Workflow[]>("/workflows");
  return data;
}

/** The pluggable trigger catalog (cron/webhook/rss/docker-image/git-*). */
export async function listWorkflowTriggerTypes(): Promise<TriggerSpec[]> {
  const { data } = await api.get<TriggerSpec[]>("/workflow-trigger-types");
  return data;
}

export async function getWorkflow(id: string): Promise<Workflow> {
  const { data } = await api.get<Workflow>(`/workflows/${id}`);
  return data;
}

export async function createWorkflow(body: WorkflowInput): Promise<Workflow> {
  const { data } = await api.post<Workflow>("/workflows", body);
  return data;
}

export async function updateWorkflow(
  id: string,
  body: WorkflowInput,
): Promise<Workflow> {
  const { data } = await api.put<Workflow>(`/workflows/${id}`, body);
  return data;
}

export async function deleteWorkflow(id: string): Promise<void> {
  await api.delete(`/workflows/${id}`);
}

/** Toggle deletion protection on a workflow. */
export async function setWorkflowProtection(
  id: string,
  protectedOn: boolean,
): Promise<Workflow> {
  const { data } = await api.put<Workflow>(`/workflows/${id}/protection`, {
    protected: protectedOn,
  });
  return data;
}

/** Trigger a manual (async) run; returns the new run id. */
export async function runWorkflow(id: string): Promise<{ runId: string }> {
  const { data } = await api.post<{ runId: string }>(`/workflows/${id}/run`);
  return data;
}

export async function listWorkflowRuns(id: string): Promise<WorkflowRun[]> {
  const { data } = await api.get<WorkflowRun[]>(`/workflows/${id}/runs`);
  return data;
}

export async function getWorkflowRun(
  id: string,
  runId: string,
): Promise<WorkflowRun> {
  const { data } = await api.get<WorkflowRun>(`/workflows/${id}/runs/${runId}`);
  return data;
}

/**
 * Generate (or rotate) the workflow run webhook token. Returns the token and
 * the public hook path (e.g. "/hooks/workflow/{token}").
 */
export async function createWorkflowHook(
  id: string,
): Promise<{ token: string; url: string }> {
  const { data } = await api.post<{ token: string; url: string }>(
    `/workflows/${id}/webhook`,
  );
  return data;
}

/** Clear the workflow run webhook token. */
export async function deleteWorkflowHook(id: string): Promise<void> {
  await api.delete(`/workflows/${id}/webhook`);
}

// ---------------------------------------------------------------------------
// Audit / activity log (admin only)
//
// Records every mutating API action (POST/PUT/PATCH/DELETE under /api/v1) plus
// authentication events. Newest first, with optional user/since filtering and
// limit/offset pagination (plus a resource `path` filter). limit defaults to 100 server-side and is capped at
// 500.
// ---------------------------------------------------------------------------

export async function listAudit(
  opts: {
    limit?: number;
    offset?: number;
    user?: string;
    since?: string;
    /** One resource's entries: this path and every path below it. */
    path?: string;
  } = {},
): Promise<{ items: AuditEntry[]; total: number }> {
  const params: Record<string, string | number> = {};
  if (opts.limit !== undefined) params.limit = opts.limit;
  if (opts.offset !== undefined) params.offset = opts.offset;
  if (opts.user) params.user = opts.user;
  if (opts.since) params.since = opts.since;
  if (opts.path) params.path = opts.path;
  const { data } = await api.get<{ items: AuditEntry[]; total: number }>(
    "/audit",
    { params },
  );
  return data;
}

// ---------------------------------------------------------------------------
// Backup & restore (admin only)
//
// The backup is a gzip+tar containing the SQLite database snapshot and the
// stacks/ data subtree. Restore is staged on the server and applied on the
// next server start (the open database is never hot-swapped).
// ---------------------------------------------------------------------------

/**
 * Download a full configuration backup as a .tar.gz and trigger a browser
 * download. The filename is taken from the server's Content-Disposition header
 * when present, otherwise a date-stamped fallback is used.
 */
export async function downloadBackup(): Promise<void> {
  const response = await api.get<Blob>("/backup", { responseType: "blob" });

  const disposition = response.headers["content-disposition"];
  const filename =
    parseFilename(typeof disposition === "string" ? disposition : undefined) ??
    `kowan-backup-${new Date().toISOString().slice(0, 10)}.tar.gz`;

  saveBlob(response.data, filename);
}

/** Upload a backup archive to stage a restore (applied on next server start). */
export async function restoreBackup(file: File): Promise<{ status: string }> {
  const formData = new FormData();
  formData.append("file", file);
  const { data } = await api.post<{ status: string }>("/restore", formData);
  return data;
}

/** Extract the filename from a Content-Disposition header, if any. */
function parseFilename(disposition: string | undefined): string | null {
  if (!disposition) return null;
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  return match ? decodeURIComponent(match[1]) : null;
}

// ---------------------------------------------------------------------------
// Docker events (activity stream)
// ---------------------------------------------------------------------------

/**
 * Recent Docker events on a host, newest first. The server defaults to the last
 * hour and caps the result at 500. `since`/`until` are unix seconds; `type`
 * narrows to container/image/network/volume.
 */
export async function listHostEvents(
  hostId: string,
  opts: { since?: number; until?: number; type?: string } = {},
): Promise<import("../types").DockerEvent[]> {
  const { data } = await api.get<import("../types").DockerEvent[]>(
    `/hosts/${hostId}/events`,
    { params: opts },
  );
  return data;
}

/**
 * Build a ws:// or wss:// URL for a host's live Docker events stream. Each
 * message is one JSON DockerEvent. `since` (unix seconds) replays from that
 * point before following live, so a reconnect does not lose events.
 */
export function eventsWebSocketURL(
  hostId: string,
  opts: { since?: number; type?: string } = {},
): string {
  const params = new URLSearchParams();
  if (opts.since) params.set("since", String(opts.since));
  if (opts.type) params.set("type", opts.type);
  return wsUrl(`/hosts/${hostId}/events/ws`, params);
}

// ---------------------------------------------------------------------------
// Image update detection
// ---------------------------------------------------------------------------

/**
 * Per-container "update available" report for a host. Remote digests are cached
 * server-side (~10 min); `refresh` bypasses that cache.
 */
/** One tag of an image checked against its registry now. */
export interface ImageUpdateRow {
  tag: string;
  localDigest: string;
  remoteDigest: string;
  updateAvailable: boolean;
  checkedAt: string;
  error?: string;
}

export async function checkImageUpdate(hostId: string, imageId: string): Promise<ImageUpdateRow[]> {
  const { data } = await api.get<ImageUpdateRow[]>(`/hosts/${hostId}/images/${encodeURIComponent(imageId)}/update`);
  return data;
}

export async function getHostUpdates(
  hostId: string,
  refresh = false,
): Promise<import("../types").ContainerUpdateStatus[]> {
  const { data } = await api.get<import("../types").ContainerUpdateStatus[]>(
    `/hosts/${hostId}/updates`,
    { params: refresh ? { refresh: 1 } : undefined },
  );
  return data;
}

/** Pull the container's image tag again and recreate it with the same config. */
export async function updateContainer(
  hostId: string,
  id: string,
): Promise<import("../types").ContainerUpdateResult> {
  const { data } = await api.post<import("../types").ContainerUpdateResult>(
    `/hosts/${hostId}/containers/${id}/update`,
  );
  return data;
}

// ---------------------------------------------------------------------------
// App templates (catalog)
// ---------------------------------------------------------------------------

export async function listTemplates(): Promise<import("../types").AppTemplate[]> {
  const { data } = await api.get<import("../types").AppTemplate[]>("/templates");
  return data;
}

/** Substitute input values server-side; returns {type, spec, compose} for createDeployment. */
export async function renderTemplate(
  id: string,
  body: { name: string; values: Record<string, string> },
): Promise<import("../types").RenderedTemplate> {
  const { data } = await api.post<import("../types").RenderedTemplate>(
    `/templates/${encodeURIComponent(id)}/render`,
    body,
  );
  return data;
}

export async function deleteTemplate(id: string): Promise<void> {
  await api.delete(`/templates/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// Deployments: git source
// ---------------------------------------------------------------------------

/**
 * "Pull & redeploy" a git-sourced deployment: the server looks up the latest
 * commit of the configured ref and queues a redeploy when it changed (always
 * with `force`).
 */
export async function syncDeployment(
  id: string,
  force = false,
): Promise<DeploymentSyncResult> {
  const { data } = await api.post<DeploymentSyncResult>(
    `/deployments/${id}/sync`,
    undefined,
    { params: force ? { force: 1 } : undefined },
  );
  return data;
}

// ---------------------------------------------------------------------------
// Platform self-update and rollback (docs/updates-design.md)
// ---------------------------------------------------------------------------
type UpdateStarted = { id: string };

/** `refresh` skips the server's registry caches (the Check button). */
export async function getServerUpdateStatus(refresh = false): Promise<import("../types").ComponentUpdateStatus> {
  return (await api.get<import("../types").ComponentUpdateStatus>("/system/server/update", refresh ? { params: { refresh: "1" } } : undefined)).data;
}
export async function startServerUpdate(image?: string): Promise<UpdateStarted> {
  return (await api.post<UpdateStarted>("/system/server/update", image ? { image } : {})).data;
}
export async function rollbackServer(restoreDb = true): Promise<UpdateStarted> {
  return (await api.post<UpdateStarted>("/system/server/rollback", { restoreDb })).data;
}
export async function listServerUpdates(limit = 20): Promise<import("../types").PlatformUpdate[]> {
  return (await api.get<import("../types").PlatformUpdate[]>("/system/server/updates", { params: { limit } })).data;
}
export async function getPlatformUpdate(id: string): Promise<import("../types").PlatformUpdate> {
  return (await api.get<import("../types").PlatformUpdate>(`/system/updates/${encodeURIComponent(id)}`)).data;
}
export async function getAgentUpdateStatus(hostId: string, refresh = false): Promise<import("../types").ComponentUpdateStatus> {
  return (await api.get<import("../types").ComponentUpdateStatus>(`/hosts/${hostId}/agent/update`, refresh ? { params: { refresh: "1" } } : undefined)).data;
}
export async function startAgentUpdate(hostId: string, image?: string): Promise<UpdateStarted> {
  return (await api.post<UpdateStarted>(`/hosts/${hostId}/agent/update`, image ? { image } : {})).data;
}
export async function rollbackAgent(hostId: string): Promise<UpdateStarted> {
  return (await api.post<UpdateStarted>(`/hosts/${hostId}/agent/rollback`, {})).data;
}
export async function listAgentUpdates(hostId: string, limit = 20): Promise<import("../types").PlatformUpdate[]> {
  return (await api.get<import("../types").PlatformUpdate[]>(`/hosts/${hostId}/agent/updates`, { params: { limit } })).data;
}
export async function rollingAgentUpdate(hostIds: string[], image?: string): Promise<{ batchId: string; ids: string[] }> {
  return (await api.post<{ batchId: string; ids: string[] }>("/agents/update", { hostIds, ...(image ? { image } : {}) })).data;
}

// ---------------------------------------------------------------------------
// Image vulnerability scans (kowan/api/scan.py)
// ---------------------------------------------------------------------------
export async function getImageScan(hostId: string, imageId: string): Promise<import("../types").ImageScanStatus> {
  return (await api.get<import("../types").ImageScanStatus>(`/hosts/${hostId}/images/${encodeURIComponent(imageId)}/scan`)).data;
}
export async function startImageScan(hostId: string, imageId: string, force = false): Promise<import("../types").ImageScanStatus> {
  return (await api.post<import("../types").ImageScanStatus>(`/hosts/${hostId}/images/${encodeURIComponent(imageId)}/scan`, { force })).data;
}
export async function getHostScans(hostId: string): Promise<import("../types").HostScans> {
  return (await api.get<import("../types").HostScans>(`/hosts/${hostId}/scans`)).data;
}

// ---------------------------------------------------------------------------
// Auto-update policies (kowan/api/autoupdate.py)
// ---------------------------------------------------------------------------
export interface UpdatePolicyBody {
  mode: import("../types").UpdateMode;
  window?: string;
  minAge?: string;
  scanBlock?: string;
}
export async function putAutoUpdateSettings(body: Partial<import("../types").AutoUpdateSettings>): Promise<import("../types").AutoUpdateSettings> {
  return (await api.put<import("../types").AutoUpdateSettings>("/autoupdate/settings", body)).data;
}
export async function getAutoUpdateOverview(): Promise<import("../types").AutoUpdateOverview> {
  return (await api.get<import("../types").AutoUpdateOverview>("/autoupdate/overview")).data;
}
export async function getAutoUpdateHistory(params: {
  kind?: string; hostId?: string; target?: string; outcome?: string; limit?: number; offset?: number;
}): Promise<{ items: import("../types").AutoUpdateHistoryEntry[]; total: number }> {
  return (await api.get<{ items: import("../types").AutoUpdateHistoryEntry[]; total: number }>("/autoupdate/history", { params })).data;
}
export async function runAutoUpdateCheck(body: { kind?: string; hostId?: string; target?: string } = {}): Promise<{ started: boolean }> {
  return (await api.post<{ started: boolean }>("/autoupdate/check", body)).data;
}
export async function getContainerUpdatePolicy(hostId: string, id: string): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.get<import("../types").ObjectUpdatePolicy>(`/hosts/${hostId}/containers/${id}/autoupdate`)).data;
}
export async function putContainerUpdatePolicy(hostId: string, id: string, body: UpdatePolicyBody): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.put<import("../types").ObjectUpdatePolicy>(`/hosts/${hostId}/containers/${id}/autoupdate`, body)).data;
}
export async function deleteContainerUpdatePolicy(hostId: string, id: string): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.delete<import("../types").ObjectUpdatePolicy>(`/hosts/${hostId}/containers/${id}/autoupdate`)).data;
}
export async function getDeploymentUpdatePolicy(id: string): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.get<import("../types").ObjectUpdatePolicy>(`/deployments/${id}/autoupdate`)).data;
}
export async function putDeploymentUpdatePolicy(id: string, body: UpdatePolicyBody): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.put<import("../types").ObjectUpdatePolicy>(`/deployments/${id}/autoupdate`, body)).data;
}
export async function deleteDeploymentUpdatePolicy(id: string): Promise<import("../types").ObjectUpdatePolicy> {
  return (await api.delete<import("../types").ObjectUpdatePolicy>(`/deployments/${id}/autoupdate`)).data;
}

// ---------------------------------------------------------------------------
// Teams, grants and effective permissions (admin; /me/permissions for anyone)
// ---------------------------------------------------------------------------

export async function listTeams(): Promise<import("../types").Team[]> {
  return (await api.get<import("../types").Team[]>("/teams")).data;
}
export async function createTeam(body: { name: string; description?: string }): Promise<import("../types").Team> {
  return (await api.post<import("../types").Team>("/teams", body)).data;
}
export async function updateTeam(id: string, body: { name?: string; description?: string }): Promise<import("../types").Team> {
  return (await api.put<import("../types").Team>(`/teams/${id}`, body)).data;
}
export async function deleteTeam(id: string): Promise<void> {
  await api.delete(`/teams/${id}`);
}
export async function addTeamMember(teamId: string, userId: string): Promise<import("../types").Team> {
  return (await api.post<import("../types").Team>(`/teams/${teamId}/members`, { userId })).data;
}
export async function removeTeamMember(teamId: string, userId: string): Promise<void> {
  await api.delete(`/teams/${teamId}/members/${userId}`);
}
export async function createGrant(
  body: import("../types").GrantInput & { subjectType: import("../types").GrantSubjectType; subjectId: string },
): Promise<import("../types").Grant> {
  return (await api.post<import("../types").Grant>("/grants", body)).data;
}
export async function deleteGrant(id: string): Promise<void> {
  await api.delete(`/grants/${id}`);
}
export async function getUserPermissions(id: string): Promise<import("../types").EffectivePermissions> {
  return (await api.get<import("../types").EffectivePermissions>(`/users/${id}/permissions`)).data;
}
export async function getMyPermissions(): Promise<import("../types").EffectivePermissions> {
  return (await api.get<import("../types").EffectivePermissions>("/me/permissions")).data;
}
/** Create an API key optionally bound to a team and/or narrowed to scopes. */
export async function createScopedApiKey(body: {
  name: string;
  role: string;
  teamId?: string;
  scopes?: import("../types").GrantInput[];
}): Promise<{ apiKey: import("../types").ScopedAPIKey; secret: string }> {
  return (await api.post<{ apiKey: import("../types").ScopedAPIKey; secret: string }>("/apikeys", body)).data;
}

// ---------------------------------------------------------------------------
// Single sign-on
// ---------------------------------------------------------------------------

/** Public: enabled providers for the login page and whether password sign-in is off. */
export async function listPublicSsoProviders(): Promise<{ providers: import("../types").SsoPublicProvider[]; ssoOnly: boolean }> {
  return (await api.get<{ providers: import("../types").SsoPublicProvider[]; ssoOnly: boolean }>("/auth/sso/providers")).data;
}
/** URL that starts the browser redirect to the identity provider. */
export function ssoStartUrl(providerId: string, returnTo: string): string {
  return `/api/v1/auth/sso/${encodeURIComponent(providerId)}/start?returnTo=${encodeURIComponent(returnTo)}`;
}
/** Redeem the one-time HttpOnly ticket cookie set by the SSO callback. */
export async function ssoExchange(): Promise<LoginResponse & { returnTo: string }> {
  return (await api.post<LoginResponse & { returnTo: string }>("/auth/sso/exchange", undefined, {
    headers: { "X-Requested-With": "kowan" },
  })).data;
}
export async function listSsoProviders(): Promise<{ providers: import("../types").SsoProvider[]; externalUrl: string; settings: import("../types").SsoSettings }> {
  return (await api.get<{ providers: import("../types").SsoProvider[]; externalUrl: string; settings: import("../types").SsoSettings }>("/sso/providers")).data;
}
export async function createSsoProvider(body: Partial<import("../types").SsoProvider>): Promise<import("../types").SsoProvider> {
  return (await api.post<import("../types").SsoProvider>("/sso/providers", body)).data;
}
export async function updateSsoProvider(id: string, body: Partial<import("../types").SsoProvider>): Promise<import("../types").SsoProvider> {
  return (await api.put<import("../types").SsoProvider>(`/sso/providers/${id}`, body)).data;
}
export async function deleteSsoProvider(id: string): Promise<void> {
  await api.delete(`/sso/providers/${id}`);
}
export async function testSsoProvider(id: string): Promise<{ ok: boolean; detail: string; redirectUri: string }> {
  return (await api.post<{ ok: boolean; detail: string; redirectUri: string }>(`/sso/providers/${id}/test`)).data;
}
export async function putSsoSettings(body: Partial<import("../types").SsoSettings>): Promise<import("../types").SsoSettings> {
  return (await api.put<import("../types").SsoSettings>("/sso/settings", body)).data;
}

// ---------------------------------------------------------------------------
// Compose validation + GitOps
// ---------------------------------------------------------------------------

export async function validateCompose(body: {
  compose: string;
  hostId?: string;
  deploymentId?: string;
  env?: Record<string, string>;
}, signal?: AbortSignal): Promise<import("../types").ComposeValidation> {
  return (await api.post<import("../types").ComposeValidation>("/compose/validate", body, { signal })).data;
}

export async function listGitOpsSyncs(): Promise<import("../types").GitOpsSync[]> {
  return (await api.get<import("../types").GitOpsSync[]>("/gitops/syncs")).data;
}
export async function createGitOpsSync(body: import("../types").GitOpsSyncInput): Promise<import("../types").GitOpsSync> {
  return (await api.post<import("../types").GitOpsSync>("/gitops/syncs", body)).data;
}
export async function updateGitOpsSync(id: string, body: Partial<import("../types").GitOpsSyncInput>): Promise<import("../types").GitOpsSync> {
  return (await api.put<import("../types").GitOpsSync>(`/gitops/syncs/${id}`, body)).data;
}
export async function deleteGitOpsSync(id: string): Promise<void> {
  await api.delete(`/gitops/syncs/${id}`);
}
export async function refreshGitOpsSync(id: string): Promise<import("../types").GitOpsSync & { plan: import("../types").GitOpsPlan | null }> {
  return (await api.post<import("../types").GitOpsSync & { plan: import("../types").GitOpsPlan | null }>(`/gitops/syncs/${id}/refresh`)).data;
}
export async function getGitOpsPlan(id: string): Promise<{ sync: import("../types").GitOpsSync; plan: import("../types").GitOpsPlan | null }> {
  return (await api.get<{ sync: import("../types").GitOpsSync; plan: import("../types").GitOpsPlan | null }>(`/gitops/syncs/${id}/plan`)).data;
}
export async function applyGitOpsSync(id: string, commit: string): Promise<{ run: import("../types").GitOpsRun; sync: import("../types").GitOpsSync }> {
  return (await api.post<{ run: import("../types").GitOpsRun; sync: import("../types").GitOpsSync }>(`/gitops/syncs/${id}/apply`, { commit })).data;
}
export async function listGitOpsRuns(id: string): Promise<import("../types").GitOpsRun[]> {
  return (await api.get<import("../types").GitOpsRun[]>(`/gitops/syncs/${id}/runs`)).data;
}
export async function getGitOpsRun(id: string, runId: string): Promise<import("../types").GitOpsRun> {
  return (await api.get<import("../types").GitOpsRun>(`/gitops/syncs/${id}/runs/${runId}`)).data;
}
export async function listGitOpsManaged(id: string): Promise<import("../types").GitOpsManaged[]> {
  return (await api.get<import("../types").GitOpsManaged[]>(`/gitops/syncs/${id}/managed`)).data;
}
export async function rotateGitOpsWebhook(id: string): Promise<import("../types").GitOpsSync> {
  return (await api.post<import("../types").GitOpsSync>(`/gitops/syncs/${id}/webhook`)).data;
}
export async function listGitOpsResources(): Promise<import("../types").GitOpsResourceRef[]> {
  return (await api.get<import("../types").GitOpsResourceRef[]>("/gitops/resources")).data;
}
type GitOpsSelection = { kind: string; id: string }[] | undefined;
export async function exportGitOpsPreview(resources: GitOpsSelection): Promise<{ files: import("../types").GitOpsExportFile[]; warnings: string[] }> {
  return (await api.post<{ files: import("../types").GitOpsExportFile[]; warnings: string[] }>("/gitops/export", { resources, format: "json" })).data;
}
export async function exportGitOpsZip(resources: GitOpsSelection): Promise<Blob> {
  return (await api.post<Blob>("/gitops/export", { resources, format: "zip" }, { responseType: "blob" })).data;
}
export async function exportGitOpsPush(
  resources: GitOpsSelection,
  push: { syncId: string; branch: string; message?: string },
): Promise<{ branch: string; commit: string; changed: boolean; files: number; warnings: string[] }> {
  return (await api.post<{ branch: string; commit: string; changed: boolean; files: number; warnings: string[] }>("/gitops/export", { resources, push })).data;
}

// ---------------------------------------------------------------------------
// Uptime checks and the status page
// ---------------------------------------------------------------------------

export async function listUptimeChecks(): Promise<import("../types").UptimeCheck[]> {
  return (await api.get<import("../types").UptimeCheck[]>("/uptime/checks")).data;
}
export async function createUptimeCheck(body: Partial<import("../types").UptimeCheckInput>): Promise<import("../types").UptimeCheck> {
  return (await api.post<import("../types").UptimeCheck>("/uptime/checks", body)).data;
}
export async function updateUptimeCheck(id: string, body: Partial<import("../types").UptimeCheckInput>): Promise<import("../types").UptimeCheck> {
  return (await api.put<import("../types").UptimeCheck>(`/uptime/checks/${id}`, body)).data;
}
export async function deleteUptimeCheck(id: string): Promise<void> {
  await api.delete(`/uptime/checks/${id}`);
}
export async function runUptimeCheck(id: string): Promise<{ ok: boolean; latencyMs: number; code: number; error: string; check: import("../types").UptimeCheck }> {
  return (await api.post<{ ok: boolean; latencyMs: number; code: number; error: string; check: import("../types").UptimeCheck }>(`/uptime/checks/${id}/run`)).data;
}
export async function uptimeResults(id: string, hours = 24): Promise<import("../types").UptimeResult[]> {
  return (await api.get<import("../types").UptimeResult[]>(`/uptime/checks/${id}/results`, { params: { hours } })).data;
}
export async function listUptimeIncidents(checkId?: string): Promise<import("../types").UptimeIncident[]> {
  return (await api.get<import("../types").UptimeIncident[]>("/uptime/incidents", { params: checkId ? { check: checkId } : undefined })).data;
}
export async function getStatusPage(): Promise<import("../types").StatusPageSettings> {
  return (await api.get<import("../types").StatusPageSettings>("/uptime/status-page")).data;
}
export async function putStatusPage(body: import("../types").StatusPageSettings): Promise<import("../types").StatusPageSettings> {
  return (await api.put<import("../types").StatusPageSettings>("/uptime/status-page", body)).data;
}
/** Public, no session needed. */
export async function getPublicStatus(): Promise<import("../types").PublicStatus> {
  return (await api.get<import("../types").PublicStatus>("/public/status")).data;
}

// ---------------------------------------------------------------------------
// Retained logs
// ---------------------------------------------------------------------------
export async function listLogRetention(): Promise<import("../types").LogRetention[]> {
  return (await api.get<import("../types").LogRetention[]>("/log-archive/retention")).data;
}
export async function setLogRetention(workload: string, days: number): Promise<import("../types").LogRetention> {
  return (await api.put<import("../types").LogRetention>(`/log-archive/retention/${encodeURIComponent(workload)}`, { days })).data;
}
/** Stop keeping a workload's logs and delete what is stored. */
export async function deleteLogRetention(workload: string): Promise<{ removed: number }> {
  return (await api.delete<{ removed: number }>(`/log-archive/retention/${encodeURIComponent(workload)}`)).data;
}
export async function searchLogArchive(p: import("../types").LogSearchParams): Promise<import("../types").LogSearchResult> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) {
    if (v === undefined || v === "" || v === 0) continue;
    if (Array.isArray(v)) v.forEach((x) => params.append(k, x));
    else params.set(k, String(v));
  }
  return (await api.get<import("../types").LogSearchResult>(`/log-archive/search?${params}`)).data;
}

export async function getWorkloadResources(workload: string, hours: number): Promise<import("../types").WorkloadResources> {
  return (await api.get<import("../types").WorkloadResources>(`/resource-history/${encodeURIComponent(workload)}`, { params: { hours } })).data;
}

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------
/** Scan the Portainer instance of a saved `portainer` connection. */
export async function scanPortainer(connectionId: string): Promise<import("../types").ImportCandidate[]> {
  return (await api.post<import("../types").ImportCandidate[]>("/import/portainer/scan", { connectionId })).data;
}
export async function scanComposeFolder(files: Record<string, string>): Promise<import("../types").ImportCandidate[]> {
  return (await api.post<import("../types").ImportCandidate[]>("/import/compose/scan", { files })).data;
}
export async function applyImport(items: { name: string; host: string; strategy: string; compose?: string; git?: import("../types").ImportCandidate["git"]; variables?: import("../types").KeyValue[] }[]): Promise<import("../types").ImportResult[]> {
  return (await api.post<import("../types").ImportResult[]>("/import/apply", { items })).data;
}

// ---------------------------------------------------------------------------
// Disk cleanup
// ---------------------------------------------------------------------------
export async function getCleanupPlan(hostId: string): Promise<import("../types").CleanupPlan> {
  return (await api.get<import("../types").CleanupPlan>(`/hosts/${hostId}/cleanup`)).data;
}
export async function runCleanup(hostId: string, body: { containers: string[]; images: string[]; volumes: string[]; buildCache: boolean }): Promise<import("../types").CleanupResult> {
  return (await api.post<import("../types").CleanupResult>(`/hosts/${hostId}/cleanup`, body)).data;
}
export async function putCleanupSchedule(hostId: string, policy: import("../types").CleanupPolicy): Promise<import("../types").CleanupPolicy> {
  return (await api.put<import("../types").CleanupPolicy>(`/hosts/${hostId}/cleanup/schedule`, policy)).data;
}

// ---------------------------------------------------------------------------
// Digest
// ---------------------------------------------------------------------------
export async function getDigest(): Promise<{ settings: import("../types").DigestSettings; last: import("../types").DigestLast | null }> {
  return (await api.get<{ settings: import("../types").DigestSettings; last: import("../types").DigestLast | null }>("/digest")).data;
}
export async function putDigest(s: import("../types").DigestSettings): Promise<import("../types").DigestSettings> {
  return (await api.put<import("../types").DigestSettings>("/digest", s)).data;
}
export async function previewDigest(): Promise<import("../types").DigestPreview> {
  return (await api.get<import("../types").DigestPreview>("/digest/preview")).data;
}
export async function sendDigest(): Promise<import("../types").DigestLast> {
  return (await api.post<import("../types").DigestLast>("/digest/send")).data;
}

// ---------------------------------------------------------------------------
// Platform features on/off (kowan/api/features.py)
// ---------------------------------------------------------------------------

export async function getFeatures(): Promise<import("../lib/features").Features> {
  return (await api.get<import("../lib/features").Features>("/features")).data;
}

export async function updateFeatures(changes: Partial<import("../lib/features").Features>): Promise<import("../lib/features").Features> {
  return (await api.put<import("../lib/features").Features>("/features", changes)).data;
}
