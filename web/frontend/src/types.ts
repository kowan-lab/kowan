// Mirrors the Go backend models. All fields are camelCase to match the JSON API.

export type Role = "admin" | "user" | string;

export interface User {
  id: string;
  username: string;
  role: Role;
  /** Email matched against an external identity provider at login. */
  email: string;
  /** Account origin: "local" or a provider type ("oidc"/"github"/"google"/"ldap"). */
  authSource: string;
  createdAt: string;
}

export type HostMode = "agent" | "edge";

export interface Host {
  id: string;
  name: string;
  type: string;
  mode: HostMode | string;
  address: string;
  online: boolean;
  lastSeen: string;
  createdAt: string;
}

/**
 * Returned by the server when a host is created (or re-fetched via the
 * join-command endpoint). Carries the created host plus the docker command the
 * user runs on the target machine to start the agent.
 */
export interface EnrollResponse {
  host: Host;
  dockerRun: string;
}

/**
 * One node of a Docker Swarm cluster, as reported by a swarm manager host.
 * `leader` is only true for the leader manager; `availability` is the node's
 * scheduling state ("active"/"pause"/"drain") and `status` its reachability
 * ("ready"/"down"/"unknown"/...).
 */
export interface SwarmNode {
  id: string;
  hostname: string;
  role: "manager" | "worker" | string;
  leader: boolean;
  availability: string;
  status: string;
  address: string;
  engineVersion: string;
}

/**
 * Swarm topology for a host. `active` is true only when the host is a reachable
 * swarm manager (the only role that can enumerate nodes); otherwise `nodes` is
 * empty.
 */
export interface HostSwarm {
  active: boolean;
  nodes: SwarmNode[];
}

export interface ContainerPort {
  privatePort: number;
  publicPort: number;
  type: string;
  ip: string;
}

export interface Container {
  id: string;
  names: string[];
  image: string;
  state: string;
  status: string;
  created: number;
  ports: ContainerPort[];
  labels: Record<string, string>;
  hostId: string;
}

export interface Image {
  id: string;
  repoTags: string[];
  size: number;
  created: number;
}

/**
 * Options for building an image on a host, from either an inline Dockerfile or a
 * git repository. Exactly one source is used: `dockerfile` (inline contents) or
 * `gitUrl` (cloned, then built). `buildArgs` is a flat key/value map. When `push`
 * is set, the resolved image is pushed to the registry identified by `registryId`
 * (the API layer resolves the stored credentials before building).
 */
export interface BuildOptions {
  tag: string;
  gitUrl?: string;
  gitRef?: string;
  dockerfile?: string;
  contextSubdir?: string;
  buildArgs?: Record<string, string>;
  noCache: boolean;
  push: boolean;
  registryId?: string;
}

/** Result of an image build: the combined build (and push) log plus outcome. */
export interface BuildResult {
  success: boolean;
  imageId: string;
  output: string;
}

export interface Volume {
  name: string;
  driver: string;
  mountpoint: string;
  createdAt: string;
  labels?: Record<string, string>;
  options?: Record<string, string>;
}

export interface VolumeCreateOptions {
  name: string;
  driver: string;
  options?: Record<string, string>;
  labels?: Record<string, string>;
  /**
   * When set together with `provider`, the control plane resolves this
   * connection and injects its secret into the driver options server-side (SMB
   * user/pass, AWS keys, or sshfs password). The frontend never sends the
   * secret — only the connection id + the non-secret options.
   */
  connectionRef?: string;
  /** Guided provider whose secret gets injected: "cifs" | "s3fs" | "sshfs". */
  provider?: string;
}

export interface IPAMConfig {
  subnet: string;
  gateway: string;
  ipRange?: string;
}

export interface NetworkContainer {
  id: string;
  name: string;
  ipv4Address: string;
}

export interface Network {
  id: string;
  name: string;
  driver: string;
  scope: string;
  internal?: boolean;
  attachable?: boolean;
  labels?: Record<string, string>;
  ipam?: IPAMConfig[];
  containers?: NetworkContainer[];
}

export interface NetworkCreateOptions {
  name: string;
  driver: string;
  internal?: boolean;
  attachable?: boolean;
  labels?: Record<string, string>;
  options?: Record<string, string>;
  ipam?: IPAMConfig[];
}

export type RegistryProviderType = "generic" | "ghcr" | "ecr" | "gcp" | "acr";

export interface Registry {
  id: string;
  name: string;
  url: string;
  username: string;
  hasPassword: boolean;
  /** Auth provider type. Defaults to "generic" on the server. */
  type: RegistryProviderType | string;
  /** True when the registry uses plain HTTP or an untrusted/self-signed TLS cert. */
  insecure: boolean;
  /** True when the last validation reached the registry and credentials worked. */
  verified: boolean;
  /** Error from the last validation attempt, if any. */
  lastError?: string;
  /**
   * Provider-specific config. Non-sensitive values are returned in clear,
   * sensitive ones masked; `configKeys` lists the populated keys.
   */
  config?: Record<string, string>;
  configKeys?: string[];
  /**
   * When set, this registry delegates its auth to a shared connection (by id)
   * instead of storing inline username/password/config.
   */
  connectionRef?: string;
  createdAt: string;
}

/** One configurable field of a registry type, used to drive the UI. */
export interface RegistryFieldSpec {
  key: string;
  label: string;
  sensitive: boolean;
  required: boolean;
}

/** The config field schema for a single registry type. */
export interface RegistryType {
  type: RegistryProviderType | string;
  fields: RegistryFieldSpec[];
}

/** Result of testing a registry's connectivity/credentials. */
export interface RegistryTestResult {
  verified: boolean;
  insecure: boolean;
  error?: string;
}

/**
 * A reusable, typed credential ("connection") that other subsystems reference
 * by id. Stored under the unified /credentials endpoint with kind="connection".
 * Sensitive config values are masked on read; only field NAMES are listed in
 * `configKeys`.
 */
export interface Connection {
  id: string;
  type: string;
  name: string;
  meta: Record<string, unknown>;
  configKeys: string[];
  config?: Record<string, string>;
  createdAt: string;
  updatedAt: string;
}

/**
 * The capability + field schema for a single connection type, from
 * /credentials/types?kind=connection. `capabilities` are strings like
 * "dockerRegistryAuth", "secretFetch", "messageSend"; `fields` reuse the
 * generic PluginField shape (key/label/sensitive/required/placeholder).
 */
export interface ConnectionType {
  type: string;
  capabilities: string[];
  fields: PluginField[];
}

export type SecretType =
  | "generic"
  | "registry"
  | "git"
  | "env"
  | "token"
  | "vault";

/**
 * An encrypted secret. Only field NAMES are ever serialized over the API; the
 * values are write-only (sent on create/update) or returned exclusively by the
 * dedicated /secrets/{id}/reveal endpoint.
 */
export interface Secret {
  id: string;
  name: string;
  type: SecretType | string;
  description: string;
  fields: string[];
  /**
   * When set, this secret delegates its external backend to a shared connection
   * (by id) — e.g. Vault's address/token come from the connection and the secret
   * only carries its ref-specific selector fields (mount/path/key). Requires a
   * connection whose capabilities include `secretFetch`.
   */
  connectionRef?: string;
  createdAt: string;
  updatedAt: string;
}

/**
 * The outcome of resolving one [[...]] reference token, returned by the admin
 * "resolve references" tool. The value is always MASKED — the preview confirms a
 * token resolves (and roughly to what) without revealing the secret. For an
 * external/plugin-backed secret the preview performs a live backend fetch.
 */
export interface ReferencePreview {
  token: string;
  source: "" | "secret" | "plugin";
  name: string;
  field?: string;
  status: "resolved" | "unresolved" | "error";
  secretType?: string;
  external: boolean;
  masked?: string;
  length?: number;
  message?: string;
}

/** One entry of an image's build history (docker history). */
export interface ImageHistoryLayer {
  created: number;
  createdBy: string;
  size: number;
  comment?: string;
  tags?: string[];
}

/** The image inspect result: the raw inspect JSON plus build history. */
export interface ImageInspectResult {
  inspect: Record<string, unknown>;
  history: ImageHistoryLayer[];
}

/** One entry when browsing a volume's contents. */
export interface VolumeFile {
  name: string;
  path: string;
  size: number;
  mode: string;
  isDir: boolean;
  modTime: number;
}

/**
 * An admin-managed API key used to authenticate API requests (kowanctl, webhook
 * scripts, CI) without a username/password login. Keys are role-scoped and
 * stored hashed; the full secret is shown in full exactly ONCE at creation and
 * is never serialized afterwards — only the `prefix` is returned for display.
 */
export interface APIKey {
  id: string;
  name: string;
  prefix: string;
  role: Role;
  createdAt: string;
  lastUsed?: string;
}

export interface ContainerStats {
  cpuPercent: number;
  memUsage: number;
  memLimit: number;
  memPercent: number;
  netRx: number;
  netTx: number;
  blkRead: number;
  blkWrite: number;
  pids: number;
}

export interface SystemInfo {
  containers: number;
  containersRunning: number;
  images: number;
  serverVersion: string;
  os: string;
  arch: string;
  memTotal: number;
  ncpu: number;
}

/**
 * One per-host time-series sample recorded by the monitor each tick. `time` is
 * an RFC3339 UTC timestamp stamped server-side. CPU/memory are aggregated over
 * the host's running containers; `memTotal` is the host memory capacity.
 */
export interface MetricSample {
  hostId: string;
  time: string;
  running: number;
  total: number;
  images: number;
  cpuPercent: number;
  memUsed: number;
  memTotal: number;
}

export interface KeyValue {
  key: string;
  value: string;
}

export type PortProtocol = "tcp" | "udp";

export interface PortMapping {
  hostPort: number;
  containerPort: number;
  protocol: PortProtocol;
}

export interface VolumeMapping {
  source: string;
  target: string;
  readOnly: boolean;
}

export type RestartPolicy =
  | ""
  | "no"
  | "on-failure"
  | "always"
  | "unless-stopped";

export interface ContainerSpec {
  image: string;
  env: KeyValue[];
  ports: PortMapping[];
  volumes: VolumeMapping[];
  networks: string[];
  restartPolicy: RestartPolicy;
  command: string[];
  labels: Record<string, string>;
  // Optional advanced fields (backward compatible, omitempty on the server).
  entrypoint?: string[];
  workingDir?: string;
  user?: string;
  cpuLimit?: number;
  memoryLimitMB?: number;
  memoryReservationMB?: number;
  privileged?: boolean;
  gpus?: string;
  tty?: boolean;
  openStdin?: boolean;
  pullImage?: boolean;
  extraArgs?: string[];
}

export interface DiskUsage {
  imagesCount: number;
  imagesSize: number;
  imagesReclaimable: number;
  containersCount: number;
  containersSize: number;
  volumesCount: number;
  volumesSize: number;
  volumesReclaimable: number;
  buildCacheSize: number;
  layersSize: number;
}

export type DeploymentType = "container" | "compose";
export type DeploymentStrategy = "standalone" | "swarm";

export interface Deployment {
  id: string;
  name: string;
  type: DeploymentType;
  strategy: DeploymentStrategy;
  targets: string[];
  replicas: number;
  spec: ContainerSpec | null;
  compose: string;
  status: string;
  redeployToken?: string;
  protected: boolean;
  /** Desired-state switch: when true the reconcile loop keeps the deployment live. */
  enabled: boolean;
  /** Git source (compose only); null/absent = inline YAML. */
  git?: GitSource | null;
  /** Commit SHA of the last successful git deploy. */
  deployedCommit?: string;
  /** RFC3339 time of the last git fetch / remote check. */
  lastSyncAt?: string;
  preview?: PreviewInfo | null;
  /** The deployment this one promotes to (staging -> production); "" = none. */
  promoteTo?: string;
  createdAt: string;
  updatedAt: string;
}

export interface DeploymentTarget {
  hostId: string;
  hostName: string;
  state: string;
  containerId?: string;
  message?: string;
}

/**
 * One service of a deployment as observed on a host. For container deployments
 * this is the single managed container; for compose deployments it is one entry
 * per compose service (grouped by the com.docker.compose.service label).
 */
export interface DeploymentService {
  service: string;
  containerId: string;
  /** Docker container name (no leading slash) for display + linking. */
  name: string;
  state: string;
  image?: string;
  ports?: ContainerPort[];
}

export type AlertLevel = "info" | "warning" | "critical" | string;

export interface Alert {
  id: string;
  ruleId: string;
  hostId: string;
  level: AlertLevel;
  title: string;
  message: string;
  resource: string;
  channelId: string;
  resolved: boolean;
  createdAt: string;
}

export type AlertRuleType =
  | "container_down"
  | "cpu_high"
  | "mem_high"
  | "disk_high"
  | "container_unhealthy"
  | "container_restart_loop"
  | "container_exited"
  | "host_down";

export type AlertOperator = ">" | "<" | ">=" | "<=" | "==";

export interface AlertRule {
  id: string;
  name: string;
  type: AlertRuleType;
  operator: AlertOperator | string;
  threshold: number;
  severity: AlertLevel | "";
  forDuration: number;
  hostId: string;
  match: string;
  labelKey: string;
  labelValue: string;
  channelId: string;
  autoResolve: boolean;
  enabled: boolean;
  createdAt: string;
}

/** Metadata describing one alert rule type; drives the rule builder. */
export interface AlertRuleTypeSpec {
  type: AlertRuleType;
  label: string;
  description: string;
  category: "resource" | "lifecycle" | "host" | string;
  numeric: boolean;
  unit: string;
  containerScoped: boolean;
  defaultOperator: AlertOperator | string;
  defaultThreshold: number;
  defaultSeverity: AlertLevel;
}

/** One entry in the notification center (the top-bar bell). */
export interface Notification {
  id: string;
  kind: "alert" | "notice" | string;
  level: AlertLevel;
  title: string;
  body: string;
  hostId: string;
  source: string;
  refId: string;
  read: boolean;
  createdAt: string;
}

// ---------------------------------------------------------------------------
// Notification channels
// ---------------------------------------------------------------------------

export type NotificationChannelType =
  | "smtp"
  | "webhook"
  | "ntfy"
  | "telegram"
  | "slack"
  | "discord";

/**
 * A notification channel. The actual config map is never serialized except
 * masked: non-sensitive values are returned in clear, sensitive ones masked,
 * and `configKeys` lists the keys that have a stored value.
 */
export interface NotificationChannel {
  id: string;
  name: string;
  type: NotificationChannelType | string;
  enabled: boolean;
  minLevel: AlertLevel;
  configKeys: string[];
  config: Record<string, string>;
  /**
   * When set, this channel delegates its transport credentials to a shared
   * connection (by id) instead of storing them inline. Requires a connection
   * whose capabilities include `messageSend`.
   */
  connectionRef?: string;
  createdAt: string;
  updatedAt: string;
}

/** One configurable field of a channel type, used to drive the UI. */
export interface ChannelFieldSpec {
  key: string;
  label: string;
  sensitive: boolean;
  required: boolean;
  placeholder: string;
}

/** The config field schema for a single channel type. */
export interface ChannelType {
  type: NotificationChannelType | string;
  fields: ChannelFieldSpec[];
}

// ---------------------------------------------------------------------------
// Workflows (automation / procedures)
// ---------------------------------------------------------------------------

export type WorkflowStepType =
  | "script"
  | "http"
  | "deploy"
  | "container"
  | "run-container"
  | "prune"
  | "notify"
  | "backup";

/**
 * One step of a workflow. `config` is a flat string map whose meaningful keys
 * depend on `type`:
 *   - script:        { script }
 *   - deploy:        { deployment, action }       (deployment id or name; action in redeploy|deploy|start|stop|destroy, default redeploy)
 *   - container:     { host, container, action }  (action in start|stop|restart)
 *   - run-container: { host, image, command, env, outputVar, remove }
 *                                                 (env is KEY=VALUE per line; command is shell-split; remove truthy auto-removes)
 *   - prune:         { host, target }             (target in containers|images|images-all|volumes|networks|all)
 *   - notify:        { channel, title, body, level }
 *   - backup:        { path }                     (server-side dir; defaults to <data>/backups)
 */
export interface WorkflowStep {
  name: string;
  type: WorkflowStepType | string;
  config: Record<string, string>;
  continueOnError: boolean;
}

/**
 * One trigger attached to a workflow. Multiple triggers fire it independently
 * (OR). `type` selects the pluggable provider; `config` is the type-specific
 * field map. `state`/`lastFired`/`lastError` are read-only provider bookkeeping.
 */
export interface WorkflowTrigger {
  id: string;
  type: string;
  enabled: boolean;
  config: Record<string, string>;
  state?: string;
  lastFired?: string;
  lastError?: string;
}

/** Trigger kind: how the trigger fires. */
export type TriggerKind = "poll" | "cron" | "webhook";

/** Catalog entry for a pluggable trigger type, from /workflow-trigger-types. */
export interface TriggerSpec {
  type: string;
  kind: TriggerKind;
  label: string;
  description: string;
  fields: PluginField[];
}

export interface Workflow {
  id: string;
  name: string;
  description: string;
  steps: WorkflowStep[];
  triggers: WorkflowTrigger[];
  /** Env-style run variables (string->string) seeding the initial run env. */
  variables?: Record<string, string>;
  enabled: boolean;
  runToken?: string;
  protected: boolean;
  createdAt: string;
  updatedAt: string;
}

export type StepStatus = "success" | "failed" | "skipped" | string;

export interface StepResult {
  name: string;
  type: string;
  status: StepStatus;
  output: string;
  error?: string;
  durationMs: number;
}

export type WorkflowRunStatus = "running" | "success" | "failed" | string;

export interface WorkflowRun {
  id: string;
  workflowId: string;
  trigger: string;
  status: WorkflowRunStatus;
  steps: StepResult[];
  startedAt: string;
  finishedAt: string;
}

// ---------------------------------------------------------------------------
// Audit / activity log
// ---------------------------------------------------------------------------

/**
 * One audited event: a mutating API action (POST/PUT/PATCH/DELETE under
 * /api/v1) or an authentication event. `time` is an RFC3339 UTC timestamp
 * stamped server-side. `action` is the HTTP method for API actions, or
 * "login"/"login_failed" for authentication events.
 */
export interface AuditEntry {
  id: string;
  time: string;
  userId: string;
  username: string;
  action: string;
  path: string;
  status: number;
  ip: string;
  detail?: string;
}

// Auth
export interface LoginResponse {
  token: string;
  user: User;
}


// Container inspect is a free-form Docker inspect object.
export type ContainerInspect = Record<string, unknown>;

// Settings is a flat key/value map.
export type Settings = Record<string, string>;

export type ContainerActionType = "start" | "stop" | "restart" | "pause" | "unpause" | "kill";

// ---------------------------------------------------------------------------
// Volume plugins (Docker managed volume drivers such as s3fs/sshfs)
// ---------------------------------------------------------------------------

export interface PluginField {
  key: string;
  label: string;
  sensitive?: boolean;
  required?: boolean;
  placeholder?: string;
  // When set, this field is a fixed choice list: the frontend renders a Select
  // (dropdown) of these string values instead of a free-text input.
  options?: string[];
  // Supplied by the selected connection and injected server-side — the form must
  // not render an input for it (e.g. s3fs access keys, sshfs username/password).
  fromConnection?: boolean;
  // When set, this field is a credential PICKER, not a text input: the frontend
  // renders <ConnectionField> for it and stores the chosen connection id under
  // config[key]. `capability` selects the eligible connection types; `types`
  // optionally narrows to specific connection types; `optional` allows a "None"
  // choice (no auth required).
  connection?: { capability: string; types?: string[]; optional?: boolean };
}

// One entry of the built-in volume-driver catalog (GET /volume-plugins): a type
// the user can pick from a list, with the managed plugin to install, the Docker
// driver to use, and its option fields.
export interface VolumePluginType {
  type: string;
  label: string;
  description?: string;
  pluginRef: string;
  driver: string;
  options: PluginField[];
}

export interface PluginInstallOptions {
  remote: string;
  alias?: string;
  settings?: Record<string, string>;
}

// One curated volume plugin from the catalog (system === "volume" entries carry
// pluginRef + driver + option schema via the Plugin.fields above; these extra
// fields are surfaced on the dedicated volume-plugin endpoint when needed).
export interface SecretProviderType {
  type: string;
  fields: PluginField[];
}

// One Docker engine event, normalised by the server (GET /hosts/{id}/events and
// the /events/ws stream). `id` is a stable dedup key; `time` is RFC3339.
export type DockerEventType = "container" | "image" | "network" | "volume" | string;
export interface DockerEvent {
  id: string;
  time: string;
  type: DockerEventType;
  action: string;
  actorId: string;
  actorName: string;
  image: string;
  attributes: Record<string, string>;
}

// Image update detection (GET /hosts/{id}/updates): one row per container,
// comparing the digest it runs with the registry's digest for the same tag.
// `error` explains rows that can't be checked (pinned by digest, local-only
// image, registry error); those always report updateAvailable=false.
export interface ContainerUpdateStatus {
  containerId: string;
  containerName: string;
  image: string;
  localDigest: string;
  remoteDigest: string;
  updateAvailable: boolean;
  checkedAt: string;
  error?: string;
}

// POST /hosts/{id}/containers/{cid}/update: the recreated container's new id.
export interface ContainerUpdateResult {
  id: string;
  image: string;
}

// App-template catalog (GET /templates). A template is a container spec or a
// compose file plus `inputs` the UI asks for; inputs are ${NAME} placeholders
// the server substitutes in POST /templates/{id}/render.
export interface TemplateInput {
  name: string;
  label: string;
  description?: string;
  default?: string;
  required: boolean;
  /** Password field; with no default the UI pre-fills a random value. */
  secret: boolean;
}

export interface AppTemplate {
  id: string;
  name: string;
  description: string;
  category: string;
  icon?: string;
  type: "container" | "compose";
  /** Raw spec: numeric fields may still hold ${VAR} tokens. */
  spec?: Record<string, unknown>;
  compose?: string;
  inputs: TemplateInput[];
  website?: string;
  builtIn: boolean;
  createdAt?: string;
  updatedAt?: string;
}

// POST /templates/{id}/render: a body fragment ready for POST /deployments.
export interface RenderedTemplate {
  type: "container" | "compose";
  spec: ContainerSpec | null;
  compose: string;
}

/**
 * Git source of a compose deployment. `webhookSecret` is server-generated (the
 * HMAC key for GitHub/Gitea `X-Hub-Signature-256` and the GitLab
 * `X-Gitlab-Token`); it is ignored when sent by the client.
 */
export interface GitSource {
  repoUrl: string;
  /** Branch or tag; default "main". */
  ref: string;
  /** Compose file path inside the repo; default "docker-compose.yml". */
  path: string;
  /** Stored connection (gitFetch capability) for private repos. */
  credentialId: string;
  /** Redeploy on a verified push webhook. */
  autoDeploy: boolean;
  webhookSecret?: string;
  /** Preview environments for other branches and pull requests. */
  previews?: PreviewSettings | null;
}

export interface PreviewSettings {
  enabled: boolean;
  /** Comma-separated globs: pushes to matching branches get a preview. "" = none. */
  branches: string;
  pullRequests: boolean;
  /** Remove a preview nobody pushed to for this long; 0 = never. */
  ttlHours: number;
  max: number;
}

/** Set on a deployment that is a preview of another one. */
export interface PreviewInfo {
  parentId: string;
  ref: string;
  /** 0 for a branch preview. */
  pullRequest: number;
  title: string;
  url: string;
  createdBy: string;
  lastActivityAt: string;
}

/** Result of POST /deployments/{id}/sync ("Pull & redeploy"). */
export interface DeploymentSyncResult {
  deployment: Deployment;
  /** True when the remote commit differed and a redeploy was queued. */
  changed: boolean;
  /** The commit the configured ref points at on the remote. */
  commit: string;
}

// ---------------------------------------------------------------------------
// Platform self-update (server + agents). Contract: docs/updates-design.md.
// ---------------------------------------------------------------------------
export type PlatformUpdateStatus = "running" | "updated" | "rolled_back" | "failed" | "aborted" | "unknown";

export interface PlatformUpdatePhase {
  phase: string;
  ok: boolean;
  at: string;
  detail?: string;
}

/** One update or rollback attempt, as recorded in the `updates` history. */
export interface PlatformUpdate {
  id: string;
  component: "server" | "agent";
  hostId: string | null;
  mode: "update" | "rollback";
  status: PlatformUpdateStatus;
  fromImage: string;
  fromDigest: string;
  fromVersion: string;
  toImage: string;
  toDigest: string;
  toVersion: string;
  phases: PlatformUpdatePhase[];
  error: string;
  logs: string[];
  startedAt: string;
  finishedAt: string;
  startedBy: string;
}

/** Update status of the server or of one agent (superset of the old shape). */
export interface ComponentUpdateStatus {
  supported: boolean;
  reason?: string;
  error?: string;
  image: string;
  container: string;
  version?: string;
  running: string;
  latest: string;
  updateAvailable: boolean;
  canRollback?: boolean;
  previous?: { image: string; version: string; digest: string } | null;
  inProgress?: PlatformUpdate | null;
  last?: PlatformUpdate | null;
  /** Release versions (X.Y.Z tags of the release repository): the running one,
   *  the newest published, and whether it is newer. Known even without self-update. */
  currentVersion?: string;
  latestVersion?: string;
  newVersion?: boolean;
  releaseError?: string;
}

// ---------------------------------------------------------------------------
// Image vulnerability scans (Trivy) — kowan/api/scan.py
// ---------------------------------------------------------------------------
export interface ScanCounts {
  critical: number;
  high: number;
  medium: number;
  low: number;
  unknown: number;
}
export type ScanSeverity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "UNKNOWN";
export interface ScanFinding {
  id: string;
  pkg: string;
  installed: string;
  fixed: string;
  severity: ScanSeverity;
  title: string;
  url: string;
  target: string;
}
export interface ImageScan {
  imageId: string;
  imageRef: string;
  repoDigests: string[];
  hostId: string;
  status: "ok" | "error";
  error: string;
  scanner: string;
  scannerVersion: string;
  os: string;
  counts: ScanCounts;
  total: number;
  fixable: number;
  findings?: ScanFinding[];
  scannedAt: string;
  durationMs: number;
}
export interface ImageScanStatus {
  imageId: string;
  running: boolean;
  fresh: boolean;
  result: ImageScan | null;
  scanner?: { image: string; version: string };
}
export interface HostScans {
  hostId: string;
  images: { imageId: string; repoTags: string[]; running: boolean; result: ImageScan | null }[];
  /** containerId -> imageId (matched by tag) for row chips. */
  containers: Record<string, string>;
  totals: ScanCounts;
  scanned: number;
  imageCount: number;
}
export interface HostScanSummary {
  hostId: string;
  scanned: number;
  lastScanAt: string;
  counts: ScanCounts;
}

// ---------------------------------------------------------------------------
// Auto-update policies — kowan/api/autoupdate.py
// ---------------------------------------------------------------------------
export type UpdateMode = "off" | "notify" | "pull" | "apply";
export type ScanBlockPolicy = "never" | "critical" | "high" | "worse";
export type PolicySource = "label" | "policy" | "default";
export interface EffectiveUpdatePolicy {
  mode: UpdateMode;
  window: string;
  minAge: string;
  scanBlock: ScanBlockPolicy;
  sources: Partial<Record<"mode" | "window" | "minAge" | "scanBlock", PolicySource>>;
  errors: string[];
  notify: boolean;
}
export interface StoredUpdatePolicy {
  kind: "container" | "deployment";
  hostId: string;
  target: string;
  mode: UpdateMode;
  window: string;
  minAge: string;
  scanBlock: string;
  updatedAt: string;
  updatedBy: string;
}
export interface UpdateWindowInfo {
  valid: boolean;
  error?: string;
  always?: boolean;
  open?: boolean;
  describe?: string;
  nextOpen?: string;
}
export type AutoUpdateOutcome = "updated" | "rolled_back" | "blocked" | "failed" | "notified" | "pulled";
export interface AutoUpdateHistoryEntry {
  id: string;
  kind: "container" | "deployment";
  hostId: string;
  target: string;
  targetName: string;
  image: string;
  fromDigest: string;
  toDigest: string;
  mode: string;
  trigger: "auto" | "manual";
  outcome: AutoUpdateOutcome | string;
  message: string;
  scan: Partial<ScanCounts> | null;
  startedAt: string;
  finishedAt: string;
}
export interface AutoUpdateState {
  lastResult?: string;
  lastMessage?: string;
  lastCheckAt?: string;
  available?: boolean;
  pending?: boolean;
  image?: string;
  remoteDigest?: string;
  currentDigest?: string;
  trackedRef?: string;
  rollbackRef?: string;
}
export interface ObjectUpdatePolicy {
  kind: "container" | "deployment";
  hostId: string;
  target: string;
  name?: string;
  labels: Record<string, string>;
  stored: StoredUpdatePolicy | null;
  effective: EffectiveUpdatePolicy;
  window: UpdateWindowInfo;
  state: AutoUpdateState;
  history: AutoUpdateHistoryEntry[];
  managedByDeployment?: boolean;
  deploymentId?: string;
  swarm?: boolean;
}
export interface AutoUpdateSettings {
  enabled: boolean;
  intervalSeconds: number;
  defaultScanBlock: ScanBlockPolicy;
  verifyTimeoutSeconds: number;
  stableSeconds: number;
  lastTickAt?: string;
  minIntervalSeconds?: number;
}
export interface AutoUpdateOverviewItem {
  kind: "container" | "deployment";
  hostId: string;
  hostName: string;
  target: string;
  name: string;
  stored: StoredUpdatePolicy | null;
  effective: EffectiveUpdatePolicy;
  window: UpdateWindowInfo;
  lastCheckAt: string;
  lastResult: string;
  lastMessage: string;
  available: boolean;
  pending: boolean;
  image: string;
  nextCheckAt: string | null;
  last: AutoUpdateHistoryEntry | null;
}
export interface AutoUpdateOverview {
  settings: AutoUpdateSettings;
  lastTickAt: string;
  objects: AutoUpdateOverviewItem[];
}

// ---------------------------------------------------------------------------
// Teams, grants and effective permissions (docs/permissions.md)
// ---------------------------------------------------------------------------

export type PermissionLevel = "read" | "operate" | "manage";
export type GrantScope = "all_hosts" | "host" | "deployment" | "stack" | "labels";
export type GrantSubjectType = "team" | "user" | "apikey";

export interface Grant {
  id: string;
  subjectType?: GrantSubjectType;
  subjectId?: string;
  scope: GrantScope;
  hostId: string;
  /** Deployment id, compose project name or `key=value` label selector. */
  target: string;
  level: PermissionLevel;
  createdAt?: string;
  createdBy?: string;
}

export interface GrantInput {
  scope: GrantScope;
  hostId?: string;
  target?: string;
  level: PermissionLevel;
}

export interface TeamMember {
  userId: string;
  username: string;
  /** "manual" or "sso:<provider id>" when kept in sync from IdP claims. */
  source: string;
  addedAt: string;
}

export interface Team {
  id: string;
  name: string;
  description: string;
  createdAt: string;
  updatedAt: string;
  members: TeamMember[];
  grants?: Grant[];
}

export interface EffectiveGrant extends Grant {
  source: string;
  sourceName: string;
  hostName: string;
  targetName: string;
  effectiveLevel: PermissionLevel;
}

export interface EffectivePermissions {
  userId?: string;
  username: string;
  role: Role;
  ceiling?: string;
  /** admin: bypasses all checks; global: role only (no teams); scoped: grants only. */
  mode: "admin" | "global" | "scoped";
  scoped: boolean;
  teams: { teamId: string; teamName: string; source: string }[];
  grants: EffectiveGrant[];
  identities?: { providerId: string; provider: string; subject: string; email: string; lastLogin: string }[];
}

export interface ScopedAPIKey extends APIKey {
  teamId?: string;
  scopes?: Grant[];
}

// ---------------------------------------------------------------------------
// Single sign-on (docs/sso.md)
// ---------------------------------------------------------------------------

export type SsoProviderType = "oidc" | "github";

export interface SsoMapping {
  claim: string;
  value: string;
  role?: Role;
  team?: string;
}

export interface SsoProviderConfig {
  defaultRole?: Role;
  roleMappings?: SsoMapping[];
  teamMappings?: SsoMapping[];
  allowSignup?: boolean;
  linkByEmail?: boolean;
  trustEmail?: boolean;
  allowedDomains?: string[];
  useUserinfo?: boolean;
  extraAuthParams?: Record<string, string>;
  tokenAuthMethod?: string;
  baseUrl?: string;
  apiUrl?: string;
}

export interface SsoProvider {
  id: string;
  name: string;
  type: SsoProviderType;
  label: string;
  icon: string;
  issuer: string;
  clientId: string;
  /** "********" when a secret is stored; never the secret itself. */
  clientSecret: string;
  scopes: string;
  config: SsoProviderConfig;
  enabled: boolean;
  managed: string;
  redirectUri: string;
  createdAt: string;
  updatedAt: string;
}

export interface SsoSettings {
  ssoOnly: boolean;
  breakGlassUser: string;
}

export interface SsoPublicProvider {
  id: string;
  name: string;
  type: SsoProviderType;
  label: string;
  icon: string;
}

// ---------------------------------------------------------------------------
// Compose validation (POST /compose/validate) and GitOps (/gitops/*)
// ---------------------------------------------------------------------------

export type DiagnosticSeverity = "error" | "warning" | "info";

/** One patch operation on the YAML document (not on the text). */
export interface ComposePatchOp {
  op: "set" | "remove";
  path: (string | number)[];
  value?: unknown;
}

export interface ComposeDiagnostic {
  severity: DiagnosticSeverity;
  code: string;
  message: string;
  path: string;
  /** 1-based; 0 when unknown. */
  line: number;
  column: number;
  fix?: { description: string; patch: ComposePatchOp[] };
}

export interface ComposeValidation {
  diagnostics: ComposeDiagnostic[];
  engine: "docker" | "builtin" | "none";
  checks: { hostPorts?: boolean; hostPaths?: boolean; secrets?: boolean };
  summary: Record<DiagnosticSeverity, number>;
}

export type GitOpsStatus = "" | "in_sync" | "drift" | "error";
export type GitOpsAction = "create" | "update" | "delete" | "noop" | "orphan" | "forget";

export interface GitOpsSummary {
  create: number;
  update: number;
  delete: number;
  noop: number;
  orphan: number;
  adopt?: number;
}

export interface GitOpsSync {
  id: string;
  name: string;
  repoUrl: string;
  ref: string;
  path: string;
  credentialId: string;
  intervalSec: number;
  autoApply: boolean;
  prune: boolean;
  enabled: boolean;
  webhookUrl: string;
  /** Returned only on create and webhook rotation. */
  webhookSecret?: string;
  status: GitOpsStatus;
  lastCommit: string;
  lastCheckedAt: string;
  lastAppliedCommit: string;
  lastAppliedAt: string;
  lastError: string;
  summary: GitOpsSummary | null;
  planErrors: number;
  createdAt: string;
  updatedAt: string;
}

export type GitOpsSyncInput = Pick<
  GitOpsSync,
  "name" | "repoUrl" | "ref" | "path" | "credentialId" | "intervalSec" | "autoApply" | "prune" | "enabled"
>;

export interface GitOpsFieldChange {
  path: string;
  before: unknown;
  after: unknown;
}

export interface GitOpsDiffLine {
  op: " " | "+" | "-";
  text: string;
}

export interface GitOpsChange {
  kind: string;
  name: string;
  action: GitOpsAction;
  adopt: boolean;
  resourceId: string;
  file: string;
  fields: GitOpsFieldChange[];
  diff: GitOpsDiffLine[];
  blocked?: string;
}

export interface GitOpsPlanError {
  file: string;
  doc: number;
  kind?: string;
  name?: string;
  message: string;
}

export interface GitOpsPlan {
  commit: string;
  generatedAt: string;
  errors: GitOpsPlanError[];
  changes: GitOpsChange[];
  summary: GitOpsSummary;
}

export interface GitOpsRunResult {
  kind: string;
  name: string;
  action: GitOpsAction;
  status: "ok" | "failed" | "skipped" | "unchanged";
  error: string;
  resourceId?: string;
}

export interface GitOpsRun {
  id: string;
  syncId: string;
  trigger: string;
  action: "refresh" | "apply";
  status: string;
  commit: string;
  error: string;
  startedAt: string;
  finishedAt: string;
  summary: GitOpsSummary | null;
  plan?: GitOpsPlan | null;
  result?: GitOpsRunResult[] | null;
}

export interface GitOpsKindInfo {
  kind: string;
  plural: string;
  order: number;
  description: string;
}

export interface GitOpsManaged {
  kind: string;
  resourceId: string;
  name: string;
  hash: string;
  commit: string;
  updatedAt: string;
}

export interface GitOpsResourceRef {
  kind: string;
  id: string;
  name: string;
  managedBy: string;
}

export interface GitOpsExportFile {
  path: string;
  kind: string;
  name: string;
  content: string;
}

// ---------------------------------------------------------------------------
// Deployment history (GET /deployments/{id}/revisions)
// ---------------------------------------------------------------------------

/** One saved version of what a deployment runs. */
export interface DeploymentRevision {
  number: number;
  createdAt: string;
  /** Who saved it ("" for the system: auto-heal, git deploys). */
  author: string;
  /** create | edit | rollback | git | gitops | exposure | mcp | update */
  source: string;
  note: string;
  /** Set when a deploy of this revision succeeded. */
  deployedAt: string;
  /** The revision that runs now (the latest one deployed). */
  live: boolean;
  /** Top-level fields that differ from the previous revision. */
  changes: string[];
}

export interface DeploymentRevisionDiff {
  from: number;
  to: number;
  changes: string[];
  lines: GitOpsDiffLine[];
}

// ---------------------------------------------------------------------------
// Uptime checks (GET /uptime/checks) and the public status page
// ---------------------------------------------------------------------------

export type UptimeStatus = "up" | "down" | "unknown" | "paused";

export interface UptimeResult {
  at: string;
  ok: boolean;
  latencyMs: number;
  code: number;
  error: string;
}

export interface UptimeCheckInput {
  name: string;
  kind: "http" | "tcp";
  /** URL for HTTP, host:port for TCP. */
  target: string;
  method: "GET" | "HEAD";
  /** Accepted status codes, e.g. "200-399" or "200,301". */
  expectStatus: string;
  keyword: string;
  verifyTls: boolean;
  intervalSec: number;
  timeoutSec: number;
  /** Failures in a row before the check is down. */
  failThreshold: number;
  deploymentId: string;
  channelId: string;
  level: "info" | "warning" | "critical";
  enabled: boolean;
  /** Listed on the public status page. */
  public: boolean;
}

export interface UptimeCheck extends UptimeCheckInput {
  id: string;
  createdAt: string;
  updatedAt: string;
  status: UptimeStatus;
  statusSince: string;
  lastCheckedAt: string;
  lastLatencyMs: number;
  lastCode: number;
  lastError: string;
  certExpiresAt: string;
  uptime24h: number | null;
  uptime7d: number | null;
  uptime30d: number | null;
  latency24h: number | null;
  recent: UptimeResult[];
}

export interface UptimeIncident {
  id: string;
  checkId: string;
  startedAt: string;
  /** "" while the outage lasts. */
  endedAt: string;
  error?: string;
  name?: string;
}

export interface StatusPageSettings {
  enabled: boolean;
  title: string;
  description: string;
}

export interface PublicStatus {
  title: string;
  description: string;
  overall: "up" | "degraded" | "down" | "unknown";
  checks: {
    id: string;
    name: string;
    status: UptimeStatus;
    statusSince: string;
    uptime30d: number | null;
    latencyMs: number | null;
    days: { date: string; uptime: number; probes: number }[];
  }[];
  incidents: UptimeIncident[];
  generatedAt: string;
}

// Retained logs (/log-archive)

/** A workload that keeps its logs. `workload` is an app id (dep~… / stack~… / ctr~…). */
export interface LogRetention {
  workload: string;
  days: number;
  createdAt: string;
  createdBy: string;
  lines: number;
  /** Oldest stored line, "" when nothing is stored yet. */
  oldest: string;
}

export interface ArchivedLogLine {
  id: number;
  ts: string;
  line: string;
  hostId: string;
  hostName: string;
  containerId: string;
  container: string;
  workload: string;
  stack: string;
  deploymentId: string;
}

export interface LogSearchResult {
  items: ArchivedLogLine[];
  /** Pass as `before` for the next (older) page; null at the end. */
  next: number | null;
}

export interface LogSearchParams {
  q?: string;
  workload?: string[];
  host?: string;
  hours?: number;
  since?: string;
  until?: string;
  before?: number;
  limit?: number;
}

// Resource history per workload (/workloads/{id}/resources)

export interface WorkloadResourcePoint {
  time: string;
  containers: number;
  /** Average CPU % over the bucket (100 = one core). */
  cpu: number;
  cpuMax: number;
  /** Average memory in bytes over the bucket. */
  mem: number;
  memMax: number;
  /** 0 = no limit. */
  memLimit: number;
  netRx: number;
  netTx: number;
}

export interface MemoryTrend {
  points: number;
  spanHours: number;
  slopeBytesPerHour: number;
  r2: number;
  start: number;
  end: number;
  limit: number;
  hoursToLimit: number | null;
  restarted: boolean;
  hostId: string;
  growing: boolean;
}

export interface WorkloadResources {
  hours: number;
  points: WorkloadResourcePoint[];
  growth: MemoryTrend | null;
}

export interface PromotionPlan {
  target: { id: string; name: string };
  changes: { what: string; from: string; to: string }[];
  notes: string[];
  /** A git target gets pinned to the commit (auto-deploy off). */
  pinned: boolean;
  diff: GitOpsDiffLine[];
}

// Imports (/import)

export interface ImportCandidate {
  key: string;
  source: "portainer" | "folder";
  name: string;
  kind: "compose" | "swarm" | "kubernetes";
  /** Portainer environment, or the folder/file for a compose folder. */
  endpoint: string;
  active: boolean;
  envCount: number;
  /** Suggested Kowan host. */
  host: string;
  strategy: "standalone" | "swarm";
  /** Compose text ("" for git stacks); with `env`, a template whose ${VAR}s
   *  the import fills in. */
  compose: string;
  /** The stack's variables. On import the ones in `inline` are written into the
   *  compose, the others go to one encrypted secret (admin only). `inImage`:
   *  variables an image line uses. */
  env?: { secret: string; values: Record<string, string>; inImage: string[] };
  git: { repoUrl: string; ref: string; path: string } | null;
  importable: boolean;
  warnings: string[];
}

export interface ImportResult {
  name: string;
  ok: boolean;
  id?: string;
  error?: string;
}

// Disk cleanup (/hosts/{id}/cleanup)

export interface CleanupItem {
  id: string;
  name: string;
  size: number;
  detail: string;
  /** Picked by default. */
  selected: boolean;
  /** Why it is not picked, or what to keep in mind. */
  note: string;
}

export interface CleanupCategory {
  id: "containers" | "dangling" | "images" | "volumes" | "build";
  title: string;
  help: string;
  items: CleanupItem[];
  total: number;
  selectedTotal: number;
}

export interface CleanupPolicy {
  enabled: boolean;
  every: "daily" | "weekly";
  /** 0 = Monday … 6 = Sunday. */
  weekday: number;
  hour: number;
  containers: boolean;
  dangling: boolean;
  /** Remove unused images at least this old; 0 = never. */
  imagesOlderThanDays: number;
  anonymousVolumes: boolean;
  buildCache: boolean;
}

export interface CleanupRun {
  at: string;
  freed: number;
  removed: Record<string, number>;
  problems: number;
  error: string;
  seconds: number;
}

export interface CleanupPlan {
  categories: CleanupCategory[];
  total: number;
  selectedTotal: number;
  disk: { images: number; containers: number; volumes: number; buildCache: number };
  schedule: CleanupPolicy;
  lastRun: CleanupRun | null;
}

export interface CleanupResult {
  freed: number;
  removed: Record<string, number>;
  problems: { id: string; error: string }[];
}

// The periodic digest (/digest)

export type DigestSection = "updates" | "cves" | "backups" | "certificates" | "alerts";

export interface DigestSettings {
  enabled: boolean;
  every: "daily" | "weekly";
  /** 0 = Monday … 6 = Sunday. */
  weekday: number;
  hour: number;
  /** Channel ids; empty = every enabled channel. */
  channels: string[];
  sections: Record<DigestSection, boolean>;
  /** Send "all clear" too, instead of skipping an empty digest. */
  sendEmpty: boolean;
}

export interface DigestLast {
  at: string;
  sent: string[];
  failed: string[];
  skipped: boolean;
}

export interface DigestPreview {
  since: string;
  until: string;
  sections: Partial<Record<DigestSection, unknown[]>>;
  empty: boolean;
  title: string;
  text: string;
  level: string;
}
