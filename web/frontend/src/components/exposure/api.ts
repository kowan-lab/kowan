import { api } from "../../api/client";

export interface ExposureRisk {
  kind: "database" | "admin" | "docker";
  label: string;
}

interface Owner {
  containerId: string;
  containerName: string;
  image: string;
  state: string;
  stack: string;
  service: string;
  deploymentId: string;
  deploymentName: string;
  deploymentType: string;
}

export interface ExposedPort extends Owner {
  ip: string;
  publicPort: number;
  privatePort: number;
  protocol: string;
  scope: "public" | "local" | "interface";
  risk: ExposureRisk | null;
  warn: boolean;
}

export interface ExposedDomain extends Owner {
  domain: string;
  proxy: "traefik";
  router: string;
  protocol: string;
  entrypoints: string[];
  tls: boolean;
  certResolver: string;
  rule: string;
  enabled: boolean;
  port: number;
  proxyRunning: boolean;
  managedByKowan: boolean;
}

export interface ProxyInfo {
  kind: "traefik";
  containerId: string;
  name: string;
  image: string;
  state: string;
  networks: string[];
  ports: { ip: string; publicPort: number; privatePort: number; protocol: string }[];
  entrypoints: Record<string, string>;
  secureEntrypoint: string;
  httpEntrypoint: string;
  certResolvers: string[];
  resolvers: Record<string, { storage: string; email: string; challenge: string; staging: boolean }>;
  dockerNetwork: string;
  exposedByDefault: boolean;
  apiEnabled: boolean;
  ingressNetworks?: string[];
}

export interface HostExposure {
  hostId: string;
  hostName: string;
  error?: string;
  ports: ExposedPort[];
  domains: ExposedDomain[];
  proxies: ProxyInfo[];
  summary: { publicPorts?: number; localPorts?: number; interfacePorts?: number; riskyPublic?: number; domains?: number };
}

export interface CertRow {
  domain: string;
  proxy: string;
  router: string;
  containerName: string;
  tls: boolean;
  certResolver: string;
  routerStatus: string;
  routerErrors: string[];
  certificate: { issuer: string; notAfter: string; daysLeft: number; status: string; staging: boolean; resolver: string; source: string } | null;
}

export interface CertStatus {
  hostId: string;
  domains: CertRow[];
  notes: string[];
}

export interface ExposeTarget {
  kind: "container" | "deployment";
  id: string;
  service?: string;
}

export interface ExposeRequest {
  target: ExposeTarget;
  domain: string;
  port?: number;
  proxyId?: string;
  entrypoint?: string;
  tls?: boolean;
  certResolver?: string;
}

export interface ExposeResult {
  status: string;
  kind?: "container" | "deployment";
  containerId?: string;
  deploymentId?: string;
  domain: string;
  proxy?: string;
  proxyName?: string;
  network?: string;
  warnings?: string[];
}

export async function getHostExposure(hostId: string): Promise<HostExposure> {
  return (await api.get<HostExposure>(`/hosts/${encodeURIComponent(hostId)}/exposure`)).data;
}

export async function getAllExposure(): Promise<HostExposure[]> {
  return (await api.get<{ hosts: HostExposure[] }>("/exposure")).data.hosts;
}

export async function getCertificates(hostId: string): Promise<CertStatus> {
  return (await api.get<CertStatus>(`/hosts/${encodeURIComponent(hostId)}/exposure/certificates`)).data;
}

export async function exposeTarget(hostId: string, req: ExposeRequest): Promise<ExposeResult> {
  return (await api.post<ExposeResult>(`/hosts/${encodeURIComponent(hostId)}/exposure/expose`, req)).data;
}

export async function unexposeTarget(hostId: string, target: ExposeTarget): Promise<ExposeResult> {
  return (await api.post<ExposeResult>(`/hosts/${encodeURIComponent(hostId)}/exposure/unexpose`, { target })).data;
}

export interface DeployProxyRequest {
  kind: "traefik";
  email: string;
  name?: string;
  httpPort?: number;
  httpsPort?: number;
  staging?: boolean;
}

export async function deployProxy(hostId: string, req: DeployProxyRequest): Promise<{ deploymentId: string; name: string }> {
  return (await api.post<{ deploymentId: string; name: string }>(`/hosts/${encodeURIComponent(hostId)}/exposure/proxy`, req)).data;
}
