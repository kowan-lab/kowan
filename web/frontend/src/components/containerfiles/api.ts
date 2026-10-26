import { api } from "../../api/client";

/** One entry of a container directory listing (see kowan/docker/containerfs.py). */
export interface ContainerFileEntry {
  name: string;
  path: string;
  size: number;
  mode: string;
  kind: "file" | "dir" | "symlink" | "device" | "pipe" | "socket";
  isDir: boolean;
  isLink: boolean;
  linkTarget: string;
  modTime: number;
}

export interface ContainerDirListing {
  /** The directory actually listed (symlinks resolved). */
  path: string;
  requested: string;
  entries: ContainerFileEntry[];
  truncated: boolean;
  /** "exec" (ls in a running container) or "archive" (tar headers). */
  method: "exec" | "archive";
  running: boolean;
}

export interface ContainerFileContent {
  path: string;
  size: number;
  tooLarge: boolean;
  limit: number;
  binary: boolean;
  content: string;
  mode?: string;
  modTime?: number;
}

const base = (hostId: string, id: string) =>
  `/hosts/${encodeURIComponent(hostId)}/containers/${encodeURIComponent(id)}/files`;

export async function listContainerDir(hostId: string, id: string, path: string): Promise<ContainerDirListing> {
  return (await api.get<ContainerDirListing>(base(hostId, id), { params: { path } })).data;
}

export async function readContainerFile(hostId: string, id: string, path: string): Promise<ContainerFileContent> {
  return (await api.get<ContainerFileContent>(`${base(hostId, id)}/content`, { params: { path } })).data;
}

export async function writeContainerFile(hostId: string, id: string, path: string, content: string): Promise<void> {
  // A Blob is sent verbatim (a string would be JSON-encoded by the client).
  await api.put(`${base(hostId, id)}/content`, new Blob([content], { type: "text/plain;charset=utf-8" }), {
    params: { path },
  });
}

export async function downloadContainerPath(hostId: string, id: string, path: string): Promise<Blob> {
  return (await api.get<Blob>(`${base(hostId, id)}/download`, { params: { path }, responseType: "blob" })).data;
}

export async function uploadContainerFile(hostId: string, id: string, dir: string, file: File): Promise<void> {
  const form = new FormData();
  form.append("file", file);
  await api.post(`${base(hostId, id)}/upload`, form, { params: { path: dir } });
}

export async function makeContainerDir(hostId: string, id: string, dir: string, name: string): Promise<void> {
  await api.post(`${base(hostId, id)}/mkdir`, { path: dir, name });
}

export async function renameContainerPath(hostId: string, id: string, path: string, name: string): Promise<void> {
  await api.post(`${base(hostId, id)}/rename`, { path, name });
}

export async function deleteContainerPath(hostId: string, id: string, path: string): Promise<void> {
  await api.delete(base(hostId, id), { params: { path } });
}
