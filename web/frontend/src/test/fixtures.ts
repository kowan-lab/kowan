// Realistic API payloads shared by the component tests (shapes from src/types.ts).
import type { Container, Deployment, Host, Image, Network, User, Volume } from "../types";

export const NOW = "2026-10-26T10:00:00Z";

export const users: Record<"admin" | "operator" | "viewer", User> = {
  admin: { id: "u-admin", username: "root", role: "admin", email: "root@example.com", authSource: "local", createdAt: NOW },
  operator: { id: "u-op", username: "ops", role: "operator", email: "", authSource: "local", createdAt: NOW },
  viewer: { id: "u-view", username: "watcher", role: "viewer", email: "", authSource: "local", createdAt: NOW },
};

export const hosts: Host[] = [
  { id: "local", name: "local", type: "local", mode: "", address: "", online: true, lastSeen: NOW, createdAt: NOW },
  { id: "h-edge", name: "edge-1", type: "agent", mode: "edge", address: "", online: false, lastSeen: "", createdAt: NOW },
];

export const containers: Container[] = [
  {
    id: "c-web".padEnd(64, "0"), names: ["/shop-web-1"], image: "nginx:1.27", state: "running", status: "Up 3 hours",
    created: 1_761_000_000, ports: [{ ip: "0.0.0.0", privatePort: 80, publicPort: 8080, type: "tcp" }],
    labels: { "com.docker.compose.project": "shop", "com.docker.compose.service": "web", "kowan.deployment.id": "d-shop" },
    hostId: "local",
  },
  {
    id: "c-db".padEnd(64, "0"), names: ["/shop-db-1"], image: "postgres:16", state: "running", status: "Up 3 hours (healthy)",
    created: 1_761_000_000, ports: [], labels: { "com.docker.compose.project": "shop", "com.docker.compose.service": "db" },
    hostId: "local",
  },
  {
    id: "c-job".padEnd(64, "0"), names: ["/nightly-job"], image: "alpine:3.22", state: "exited", status: "Exited (1) 2 hours ago",
    created: 1_761_000_000, ports: [], labels: {}, hostId: "local",
  },
];

export const images: Image[] = [
  { id: "sha256:" + "a".repeat(64), repoTags: ["nginx:1.27"], size: 192_000_000, created: 1_760_000_000 },
  { id: "sha256:" + "b".repeat(64), repoTags: ["postgres:16"], size: 420_000_000, created: 1_760_000_000 },
  { id: "sha256:" + "c".repeat(64), repoTags: [], size: 5_000_000, created: 1_750_000_000 },
];

export const volumes: Volume[] = [
  { name: "shop_db", driver: "local", mountpoint: "/var/lib/docker/volumes/shop_db/_data", createdAt: NOW,
    labels: { "com.docker.compose.project": "shop" } },
  { name: "orphan-data", driver: "local", mountpoint: "/var/lib/docker/volumes/orphan-data/_data", createdAt: NOW },
];

export const networks: Network[] = [
  { id: "n-bridge", name: "bridge", driver: "bridge", scope: "local", ipam: [{ subnet: "172.17.0.0/16", gateway: "172.17.0.1" }], containers: [] },
  { id: "n-shop", name: "shop_default", driver: "bridge", scope: "local", labels: { "com.docker.compose.project": "shop" },
    containers: [{ id: containers[0].id, name: "shop-web-1", ipv4Address: "172.18.0.2/16" }] },
];

export const deployments: Deployment[] = [
  {
    id: "d-shop", name: "shop", type: "compose", strategy: "standalone", targets: ["local"], replicas: 0, spec: null,
    compose: "services:\n  web:\n    image: nginx:1.27\n  db:\n    image: postgres:16\n", status: "deployed",
    protected: false, enabled: true, createdAt: NOW, updatedAt: NOW,
  },
  {
    id: "d-api", name: "api", type: "container", strategy: "standalone", targets: ["local"], replicas: 0,
    spec: { image: "ghcr.io/acme/api:2.1", env: [{ key: "MODE", value: "prod" }], ports: [{ hostPort: 9000, containerPort: 9000, protocol: "tcp" }] } as Deployment["spec"],
    compose: "", status: "stopped", protected: true, enabled: true, createdAt: NOW, updatedAt: NOW,
  },
];

export const systemInfo = {
  containers: 3, containersRunning: 2, images: 3, serverVersion: "29.0.0", os: "Debian GNU/Linux 13", arch: "x86_64",
  memTotal: 16 * 2 ** 30, ncpu: 8,
};

export const diskUsage = {
  imagesCount: 3, imagesSize: 617_000_000, imagesReclaimable: 5_000_000, containersCount: 3, containersSize: 1_000_000,
  volumesCount: 2, volumesSize: 50_000_000, volumesReclaimable: 10_000_000, buildCacheSize: 0, layersSize: 0,
};

export const containerInspect = {
  Id: containers[0].id,
  Name: "/shop-web-1",
  Created: NOW,
  RestartCount: 0,
  Image: images[0].id,
  Config: { Image: "nginx:1.27", Env: ["NGINX_PORT=80", "PATH=/usr/bin"], Labels: containers[0].labels, Cmd: ["nginx", "-g", "daemon off;"] },
  State: { Status: "running", Running: true, Paused: false, Restarting: false, ExitCode: 0, StartedAt: NOW, Health: { Status: "healthy" } },
  HostConfig: { RestartPolicy: { Name: "unless-stopped" }, PortBindings: { "80/tcp": [{ HostIp: "0.0.0.0", HostPort: "8080" }] }, Binds: [] },
  NetworkSettings: { Networks: { shop_default: { IPAddress: "172.18.0.2", Aliases: ["web"] } }, Ports: { "80/tcp": [{ HostIp: "0.0.0.0", HostPort: "8080" }] } },
  Mounts: [{ Type: "volume", Name: "shop_db", Source: "/var/lib/docker/volumes/shop_db/_data", Destination: "/data", RW: true }],
};

export const containerSpec = {
  image: "nginx:1.27", env: [{ key: "NGINX_PORT", value: "80" }], ports: [{ hostPort: 8080, containerPort: 80, protocol: "tcp" }],
  volumes: [], networks: ["shop_default"], restartPolicy: "unless-stopped", command: [], labels: {}, entrypoint: [],
};

export const triggerTypes = [
  { type: "cron", kind: "cron", label: "Schedule", description: "Run on a cron schedule", fields: [{ key: "cron", label: "Cron", required: true, placeholder: "0 3 * * *" }] },
  { type: "webhook", kind: "webhook", label: "Webhook", description: "Run on an HTTP request", fields: [{ key: "method", label: "Method", required: false }] },
  { type: "git-commit", kind: "poll", label: "Git commit", description: "Run on a new commit", fields: [{ key: "repo", label: "Repository", required: true }, { key: "branch", label: "Branch" }] },
];

export const workflow = {
  id: "wf-1", name: "nightly backup", description: "Back up and notify",
  steps: [
    { name: "backup", type: "backup", config: {}, continueOnError: false },
    { name: "ping", type: "http", config: { method: "POST", url: "https://hooks.example/done" }, continueOnError: true },
  ],
  triggers: [{ id: "t1", type: "cron", enabled: true, config: { cron: "0 3 * * *" } }],
  variables: { ENV: "prod" },
  enabled: true, protected: false, createdAt: NOW, updatedAt: NOW,
};

export const workflowRuns = [
  { id: "run-2", workflowId: "wf-1", trigger: "manual", status: "failed", startedAt: NOW, finishedAt: NOW,
    steps: [{ name: "backup", type: "backup", status: "success", output: "wrote backup /data/b.tgz", durationMs: 120 },
            { name: "ping", type: "http", status: "failed", error: "http step: unexpected status 500", output: "", durationMs: 30 }] },
  { id: "run-1", workflowId: "wf-1", trigger: "cron", status: "success", startedAt: NOW, finishedAt: NOW, steps: [] },
];

const cred = (id: string, kind: string, type: string, name: string, meta: Record<string, unknown>, configKeys: string[] = [], config?: Record<string, string>) =>
  ({ id, kind, type, name, meta, configKeys, config, createdAt: NOW, updatedAt: NOW });

export const credentials = {
  secret: [
    cred("s-db", "secret", "generic", "db-password", { description: "Postgres password" }, ["value"]),
    cred("s-vault", "secret", "vault", "vault-api", { description: "", connectionRef: "c-vault" }, ["path", "key"], { path: "app", key: "api" }),
  ],
  registry: [
    cred("r-ghcr", "registry", "ghcr", "ghcr", { url: "ghcr.io", username: "acme", hasPassword: true, verified: true, connectionRef: "c-ghcr" }),
    cred("r-priv", "registry", "generic", "private", { url: "registry.example.com", username: "bob", hasPassword: true, verified: false, lastError: "unauthorized" }),
  ],
  channel: [
    cred("ch-ops", "channel", "webhook", "ops-hook", { enabled: true, minLevel: "warning", connectionRef: "c-hook" }, ["url"], { url: "https://hooks.example/ops" }),
    cred("ch-mail", "channel", "smtp", "oncall-mail", { enabled: false, minLevel: "critical" }, ["host", "from", "to", "password"], { host: "smtp.example.com", from: "k@example.com", to: "o@example.com", password: "********" }),
  ],
  connection: [
    cred("c-gh", "connection", "github", "github", { note: "" }, ["token"], { token: "********" }),
    cred("c-vault", "connection", "vault", "vault-prod", {}, ["address", "token"], { address: "https://vault.example", token: "********" }),
    cred("c-ghcr", "connection", "ghcr", "ghcr-acme", { username: "acme" }, ["username", "token"], { username: "acme", token: "********" }),
    cred("c-hook", "connection", "webhook", "ops-webhook", {}, ["url"], { url: "https://hooks.example/ops" }),
    cred("c-smb", "connection", "basic-auth", "smb-user", {}, ["username", "password"], { username: "bob", password: "********" }),
    cred("c-aws", "connection", "aws", "aws-prod", { region: "eu-west-1" }, ["accessKeyId", "secretAccessKey"], { accessKeyId: "AKIA…", secretAccessKey: "********" }),
  ],
};

export const teams = [
  {
    id: "t-web", name: "web", description: "Web squad", createdAt: NOW, updatedAt: NOW,
    members: [{ userId: "u-op", username: "ops", source: "manual", addedAt: NOW }],
    grants: [{ id: "g-1", subjectType: "team", subjectId: "t-web", scope: "deployment", hostId: "local", target: "d-shop", level: "operate", createdAt: NOW }],
  },
  { id: "t-empty", name: "data", description: "", createdAt: NOW, updatedAt: NOW, members: [], grants: [] },
];

export const apiKeys = [
  { id: "k-ci", name: "ci-deploy", prefix: "kw_ab12", role: "operator", createdAt: NOW, lastUsed: NOW },
  { id: "k-team", name: "web-bot", prefix: "kw_cd34", role: "viewer", createdAt: NOW, teamId: "t-web" },
];
