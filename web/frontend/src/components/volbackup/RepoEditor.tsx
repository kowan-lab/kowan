import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Box, Button, Callout, Popup, Select, TextArea, TextInput } from "@/ui/kit";
import { listHosts } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import { createRepo, updateRepo, type BackupRepo, type RepoInput, type RepoType } from "./api";

const TYPES: { value: RepoType; label: string }[] = [
  { value: "local", label: "Local path on a host" },
  { value: "s3", label: "S3-compatible (AWS, MinIO, R2, B2…)" },
  { value: "sftp", label: "SFTP" },
  { value: "rest", label: "restic REST server" },
];

/** Create / edit a restic repository. Secrets are write-only: blank = keep. */
export default function RepoEditor({ open, onClose, onSaved, repo }: {
  open: boolean; onClose: () => void; onSaved: () => void; repo?: BackupRepo | null;
}) {
  const [f, setF] = useState<RepoInput>({ name: "", type: "local", hostId: "local", config: {}, secrets: {} });
  useEffect(() => {
    if (!open) return;
    setF(repo
      ? { name: repo.name, type: repo.type, hostId: repo.hostId || "local", config: { ...repo.config }, secrets: {} }
      : { name: "", type: "local", hostId: "local", config: {}, secrets: {} });
  }, [open, repo]);
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: open });
  const cfg = (k: string) => f.config[k] ?? "";
  const setCfg = (k: string, v: string) => setF((x) => ({ ...x, config: { ...x.config, [k]: v } }));
  const setSec = (k: string, v: string) => setF((x) => ({ ...x, secrets: { ...x.secrets, [k]: v } }));
  const isSet = (k: string) => Boolean(repo?.secrets?.[k]);
  const secretHint = (k: string) => (isSet(k) ? "Set. Leave blank to keep it." : undefined);

  const save = useMutation({
    mutationFn: () => {
      const secrets = Object.fromEntries(Object.entries(f.secrets).filter(([, v]) => v));
      const config = Object.fromEntries(Object.entries(f.config).filter(([, v]) => v));
      const body = { ...f, config, secrets, hostId: f.type === "local" ? f.hostId : "" };
      return repo ? updateRepo(repo.id, body) : createRepo(body);
    },
    onSuccess: () => { notify({ color: "green", title: "Repository saved", message: f.name }); onSaved(); onClose(); },
    onError: (e) => notify({ color: "red", title: "Save failed", message: errorMessage(e, "Could not save the repository") }),
  });

  return (
    <Popup open={open} onOpenChange={(o) => { if (!o) onClose(); }} size="md">
      <Popup.Content>
        <Popup.Header><Popup.Title>{repo ? `Edit ${repo.name}` : "New repository"}</Popup.Title><Popup.Close /></Popup.Header>
        <Popup.Body>
          <Box direction="row" wrap gap={3}>
            <TextInput label="Name" required value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} maxWidth={260} data-autofocus />
            <Select label="Type" width={300} value={f.type} disabled={!!repo} options={TYPES}
              onChange={(v) => setF({ ...f, type: v as RepoType, config: {} })} />
          </Box>
          {f.type === "local" && (
            <Box direction="row" wrap gap={3}>
              <Select label="Host" width={200} value={f.hostId ?? "local"} onChange={(v) => setF({ ...f, hostId: v })}
                options={(hosts.data ?? []).map((h) => ({ value: h.id, label: h.name }))} />
              <TextInput label="Path on the host" required placeholder="/srv/backups/restic" value={cfg("path")} onChange={(e) => setCfg("path", e.target.value)}
                description="Absolute path; created if missing. Only jobs on this host can use it." />
            </Box>
          )}
          {f.type === "s3" && (
            <>
              <Box direction="row" wrap gap={3}>
                <TextInput label="Endpoint" placeholder="https://s3.eu-west-1.amazonaws.com or http://minio:9000" value={cfg("endpoint")} onChange={(e) => setCfg("endpoint", e.target.value)}
                  description="Empty = AWS (s3.amazonaws.com). http:// for plain MinIO." />
                <TextInput label="Region" placeholder="eu-west-1" value={cfg("region")} onChange={(e) => setCfg("region", e.target.value)} maxWidth={160} />
              </Box>
              <Box direction="row" wrap gap={3}>
                <TextInput label="Bucket" required value={cfg("bucket")} onChange={(e) => setCfg("bucket", e.target.value)} maxWidth={240} />
                <TextInput label="Prefix" placeholder="kowan/" value={cfg("prefix")} onChange={(e) => setCfg("prefix", e.target.value)} maxWidth={240} />
              </Box>
              <Box direction="row" wrap gap={3}>
                <TextInput label="Access key ID" autoComplete="off" value={f.secrets.accessKeyId ?? ""} onChange={(e) => setSec("accessKeyId", e.target.value)} description={secretHint("accessKeyId")} />
                <TextInput label="Secret access key" type="password" autoComplete="new-password" value={f.secrets.secretAccessKey ?? ""} onChange={(e) => setSec("secretAccessKey", e.target.value)} description={secretHint("secretAccessKey")} />
              </Box>
            </>
          )}
          {f.type === "sftp" && (
            <>
              <Box direction="row" wrap gap={3}>
                <TextInput label="Host" required value={cfg("host")} onChange={(e) => setCfg("host", e.target.value)} maxWidth={240} />
                <TextInput label="Port" placeholder="22" value={cfg("port")} onChange={(e) => setCfg("port", e.target.value)} maxWidth={100} />
                <TextInput label="User" required value={cfg("user")} onChange={(e) => setCfg("user", e.target.value)} maxWidth={160} />
              </Box>
              <TextInput label="Path" required placeholder="/backups/restic" value={cfg("path")} onChange={(e) => setCfg("path", e.target.value)} />
              <TextArea label="SSH private key" rows={4} value={f.secrets.sshKey ?? ""} onChange={(e) => setSec("sshKey", e.target.value)}
                description={secretHint("sshKey") ?? "OpenSSH private key without passphrase. The server's host key is accepted on first use."} />
            </>
          )}
          {f.type === "rest" && (
            <Box direction="row" wrap gap={3}>
              <TextInput label="URL" required placeholder="https://rest.example.com:8000/repo" value={cfg("url")} onChange={(e) => setCfg("url", e.target.value)} />
              <TextInput label="Username" value={cfg("username")} onChange={(e) => setCfg("username", e.target.value)} maxWidth={180} />
              <TextInput label="Password" type="password" autoComplete="new-password" value={f.secrets.restPassword ?? ""} onChange={(e) => setSec("restPassword", e.target.value)} description={secretHint("restPassword")} maxWidth={220} />
            </Box>
          )}
          <Box direction="row" wrap gap={3}>
            <TextInput label="Repository password" type="password" autoComplete="new-password" required={!repo} value={f.secrets.password ?? ""}
              onChange={(e) => setSec("password", e.target.value)}
              description={secretHint("password") ?? "Encrypts the repository. Without it backups cannot be restored: keep a copy elsewhere."} />
            <TextInput label="Docker network" placeholder="optional" value={cfg("network")} onChange={(e) => setCfg("network", e.target.value)} maxWidth={200}
              description="Attach the restic container to it (e.g. to reach a MinIO container)." />
          </Box>
          {!repo && <Callout color="info">Credentials are stored encrypted with Kowan's secret key and never shown again. The repository is initialised on first use.</Callout>}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="outline" color="neutral" onClick={onClose}>Cancel</Button>
          <Button loading={save.isPending} disabled={!f.name.trim()} onClick={() => save.mutate()}>{repo ? "Save" : "Create repository"}</Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}
