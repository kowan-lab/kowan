import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  Button,
  Callout,
  Code,
  CopyButton,
  InputToggle,
  Popup,
  Select,
  Skeleton,
  Text,
  TextInput,
  toast,
} from "@/ui/kit";
import { deployRegistry, listHosts, type DeployRegistryResult } from "../../api/client";
import { errorMessage } from "../../utils";
import { DeployProxy } from "../exposure/ExposeDialog";
import { getHostExposure } from "../exposure/api";

/**
 * Deploy an image registry on one of the hosts, published by the host's
 * Traefik on a domain with a Let's Encrypt certificate, and add it to the
 * registry list. With no Traefik on the host it offers to deploy one first.
 */
export function DeployRegistryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const hosts = useQuery({ queryKey: ["hosts"], queryFn: listHosts, enabled: open });
  const [hostId, setHostId] = useState("");
  const [name, setName] = useState("registry");
  const [domain, setDomain] = useState("");
  const [auth, setAuth] = useState(true);
  const [username, setUsername] = useState("kowan");
  const [password, setPassword] = useState("");
  const [result, setResult] = useState<DeployRegistryResult | null>(null);

  const hostList = useMemo(() => hosts.data ?? [], [hosts.data]);
  const host = hostList.find((h) => h.id === hostId);
  useEffect(() => {
    if (!hostId && hostList.length) setHostId((hostList.find((h) => h.online) ?? hostList[0]).id);
  }, [hostList, hostId]);

  const exposure = useQuery({
    queryKey: ["exposure", hostId],
    queryFn: () => getHostExposure(hostId),
    enabled: open && Boolean(hostId),
  });
  const proxy = (exposure.data?.proxies ?? []).find((p) => p.state === "running");

  const deploy = useMutation({
    mutationFn: () =>
      deployRegistry(hostId, {
        name: name.trim(),
        domain: domain.trim(),
        auth,
        ...(auth ? { username: username.trim(), password } : {}),
      }),
    onSuccess: (r) => {
      setResult(r);
      void qc.invalidateQueries({ queryKey: ["registries"] });
      void qc.invalidateQueries({ queryKey: ["deployments"] });
    },
    onError: (err) => toast.error("Could not deploy the registry", { description: errorMessage(err, "Deploy failed.") }),
  });

  const ready =
    Boolean(hostId) && Boolean(name.trim()) && Boolean(domain.trim()) && Boolean(proxy) &&
    (!auth || (Boolean(username.trim()) && (password === "" || password.length >= 8)));

  return (
    <Popup open={open} onOpenChange={(o) => !o && onClose()} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>{result ? `Registry ${result.name}` : "Deploy a registry"}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        {result ? (
          <>
            <Popup.Body>
              <DeployedRegistry result={result} hostName={host?.name ?? hostId} />
            </Popup.Body>
            <Popup.Footer>
              <Button onClick={onClose}>Done</Button>
            </Popup.Footer>
          </>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (ready) deploy.mutate();
            }}
          >
            <Popup.Body>
              {hosts.isLoading ? (
                <Skeleton height={200} />
              ) : (
                <Box direction="column" gap={4}>
                  <Box direction="row" gap={3} wrap>
                    <Select label="Host" width={220} value={hostId} onChange={setHostId}
                      options={hostList.map((h) => ({ value: h.id, label: h.online ? h.name : `${h.name} (offline)` }))} />
                    <TextInput label="Name" width={200} value={name} required
                      description="Also its name in the registry list."
                      onChange={(e) => setName(e.currentTarget.value.toLowerCase())} />
                  </Box>

                  <TextInput label="Domain" placeholder="registry.example.com" value={domain} required
                    description="Point its DNS at this host. Let's Encrypt issues the certificate, so every machine trusts it."
                    onChange={(e) => setDomain(e.currentTarget.value.trim())} />
                  {exposure.isLoading ? (
                    <Skeleton height={40} />
                  ) : exposure.isError ? (
                    <Callout color="danger">{errorMessage(exposure.error, "Could not read this host.")}</Callout>
                  ) : !proxy ? (
                    <DeployProxy hostId={hostId} view={exposure.data} />
                  ) : (
                    <Text size="xs" color="muted">
                      Published through <Code>{proxy.name}</Code>; ports 80 and 443 must be open to the internet.
                    </Text>
                  )}

                  <InputToggle label="Require a login" checked={auth} onChange={(e) => setAuth(e.currentTarget.checked)} />
                  {auth ? (
                    <Box direction="row" gap={3} wrap>
                      <TextInput label="Username" width={180} value={username} onChange={(e) => setUsername(e.currentTarget.value)} />
                      <TextInput label="Password" type="password" width={260} value={password} autoComplete="new-password"
                        placeholder="Leave empty to generate one"
                        errorText={password && password.length < 8 ? "At least 8 characters" : undefined}
                        onChange={(e) => setPassword(e.currentTarget.value)} />
                    </Box>
                  ) : (
                    <Callout color="warning">Without a login, anyone who can reach the registry can push and pull images.</Callout>
                  )}
                </Box>
              )}
            </Popup.Body>
            <Popup.Footer>
              <Button variant="clear" color="neutral" onClick={onClose}>Cancel</Button>
              <Button type="submit" loading={deploy.isPending} disabled={!ready}>Deploy</Button>
            </Popup.Footer>
          </form>
        )}
      </Popup.Content>
    </Popup>
  );
}

function DeployedRegistry({ result, hostName }: { result: DeployRegistryResult; hostName: string }) {
  const example = `${result.address}/my-app:1.0`;
  const login = `docker login ${result.address} -u ${result.username}`;
  return (
    <Box direction="column" gap={4}>
      <Callout color="success" title={`Deploying on ${hostName}`}>
        It is already in the registry list and turns Verified once it answers; Let&apos;s Encrypt can take a minute.
      </Callout>
      <Box direction="column" gap={1}>
        <Text size="xs" color="muted" transform="uppercase">Image references</Text>
        <Box direction="row" gap={2} align="center"><Code>{example}</Code><CopyButton value={example} /></Box>
      </Box>
      {result.auth && (
        <Box direction="column" gap={1}>
          <Text size="xs" color="muted" transform="uppercase">Login</Text>
          <Box direction="row" gap={2} align="center"><Code>{login}</Code><CopyButton value={login} /></Box>
          {result.password && (
            <>
              <Box direction="row" gap={2} align="center">
                <Code>{result.password}</Code>
                <CopyButton value={result.password} label="Copy password" />
              </Box>
              <Text size="xs" color="muted">Shown only now. Kowan keeps it with the registry for its own pulls.</Text>
            </>
          )}
        </Box>
      )}
    </Box>
  );
}
