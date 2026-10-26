import { api } from "../../api/client";

export type NetNodeKind = "container" | "network" | "port" | "domain";

export interface NetNode {
  id: string;
  kind: NetNodeKind;
  label: string;
  sub: string;
  state: string;
  warn?: boolean;
  hostId: string;
  stack: string;
  ref: { containerId?: string; deploymentId?: string; networkId?: string; networkName?: string };
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface NetEdge {
  id: string;
  from: string;
  to: string;
  kind: "port" | "domain" | "network" | "depends";
}

export interface NetGroup {
  id: string;
  kind: "host" | "stack";
  label: string;
  stack: string;
  hostId: string;
  deploymentId: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface NetMap {
  nodes: NetNode[];
  edges: NetEdge[];
  groups: NetGroup[];
  width: number;
  height: number;
  stacks: string[];
  networks: string[];
  hosts: { id: string; name: string; error: string }[];
  counts: { containers: number; networks: number; ingress: number };
}

export async function getNetMap(hostId: string | null, filters: { stack?: string; network?: string }): Promise<NetMap> {
  const url = hostId ? `/hosts/${encodeURIComponent(hostId)}/netmap` : "/netmap";
  return (await api.get<NetMap>(url, { params: { stack: filters.stack || undefined, network: filters.network || undefined } })).data;
}
