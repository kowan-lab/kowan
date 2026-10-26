import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router";
import {
  Badge,
  Box,
  Button,
  Callout,
  Checkbox,
  Code,
  Popup,
  Select,
  Skeleton,
  Text,
  TextInput,
  toast,
} from "@/ui/kit";
import { useAuth } from "../../auth/AuthContext";
import { errorMessage } from "../../utils";
import {
  deployProxy,
  exposeTarget,
  getHostExposure,
  unexposeTarget,
  type ExposeResult,
  type ExposeTarget,
  type HostExposure,
} from "./api";

export interface ExposeDialogProps {
  open: boolean;
  onClose: () => void;
  hostId: string;
  target: ExposeTarget;
  /** Label for the dialog title (container / deployment name). */
  name: string;
  /** Pre-filled container port, when known. */
  defaultPort?: number;
  /** Compose services the user can pick from (compose deployments). */
  services?: string[];
}

/**
 * Publish a container or deployment on a domain through the host's reverse
 * Traefik proxy, or remove that exposure. When no proxy
 * runs on the host it offers to deploy Traefik with Let's Encrypt (HTTP-01).
 */
export function ExposeDialog({ open, onClose, hostId, target, name, defaultPort, services }: ExposeDialogProps) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const view = useQuery({ queryKey: ["exposure", hostId], queryFn: () => getHostExposure(hostId), enabled: open && Boolean(hostId) });
  const running = useMemo(() => (view.data?.proxies ?? []).filter((p) => p.state === "running"), [view.data]);
  const current = useMemo(
    () =>
      (view.data?.domains ?? []).filter((d) =>
        target.kind === "container" ? d.containerId === target.id : d.deploymentId === target.id,
      ),
    [view.data, target],
  );
  const managed = current.find((d) => d.managedByKowan);

  const [domain, setDomain] = useState("");
  const [port, setPort] = useState(defaultPort ? String(defaultPort) : "");
  const [service, setService] = useState(target.service ?? services?.[0] ?? "");
  const [proxyId, setProxyId] = useState("");
  const [entrypoint, setEntrypoint] = useState("");
  const [tls, setTls] = useState(true);
  const [resolver, setResolver] = useState("");
  useEffect(() => {
    if (defaultPort && !port) setPort(String(defaultPort));
  }, [defaultPort, port]);

  const proxy = running.find((p) => p.containerId === proxyId) ?? running[0];
  useEffect(() => {
    if (!proxy) return;
    setEntrypoint(proxy.secureEntrypoint || proxy.httpEntrypoint || "");
    setTls(Boolean(proxy.secureEntrypoint));
    setResolver(proxy.certResolvers[0] ?? "");
  }, [proxy?.containerId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Recreating changes the container id: follow it (or its deployment) so the
  // detail page the dialog was opened from does not end up on a 404.
  const follow = (r: ExposeResult) => {
    if (target.kind !== "container" || !location.pathname.startsWith("/containers/")) return;
    if (r.kind === "container" && r.containerId) nav(`/containers/${r.containerId}${location.search}`, { replace: true });
    else if (r.kind === "deployment" && r.deploymentId) nav(`/workloads/dep~${r.deploymentId}`);
  };

  const done = () => {
    void qc.invalidateQueries({ queryKey: ["exposure"] });
    void qc.invalidateQueries({ queryKey: ["containers"] });
    void qc.invalidateQueries({ queryKey: ["container-inspect"] });
    void qc.invalidateQueries({ queryKey: ["deployment"] });
  };

  const expose = useMutation({
    mutationFn: () =>
      exposeTarget(hostId, {
        target: { ...target, service: target.kind === "deployment" && services?.length ? service : target.service },
        domain: domain.trim(),
        port: port ? Number(port) : undefined,
        proxyId: proxy?.containerId,
        entrypoint: proxy?.kind === "traefik" ? entrypoint : undefined,
        tls,
        certResolver: proxy?.kind === "traefik" && tls ? resolver : undefined,
      }),
    onSuccess: (r) => {
      toast.success(r.status === "redeploying" ? "Exposing: redeploy started" : "Exposed", {
        description: `${r.domain} via ${r.proxyName ?? r.proxy}`,
      });
      for (const w of r.warnings ?? []) toast.warning(w);
      done();
      onClose();
      follow(r);
    },
    onError: (err) => toast.error("Expose failed", { description: errorMessage(err, "Could not expose.") }),
  });

  const unexpose = useMutation({
    mutationFn: () => unexposeTarget(hostId, { ...target, service: managed?.service || target.service }),
    onSuccess: (r) => {
      toast.success(r.status === "redeploying" ? "Unexposing: redeploy started" : "Unexposed", { description: r.domain });
      done();
      onClose();
      follow(r);
    },
    onError: (err) => toast.error("Unexpose failed", { description: errorMessage(err, "Could not unexpose.") }),
  });

  const viaDeployment = target.kind === "deployment" || current.some((d) => d.deploymentId);
  const recreateNote = viaDeployment
    ? "Its deployment is updated and redeployed."
    : "The container is recreated with the new labels (same config, rolled back if anything fails).";
  useEffect(() => {
    if (managed && !domain) setDomain(managed.domain);
  }, [managed?.domain]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Popup open={open} onOpenChange={(o) => !o && onClose()} size="md">
      <Popup.Content>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            expose.mutate();
          }}
        >
          <Popup.Header>
            <Popup.Title>Expose {name}</Popup.Title>
            <Popup.Close />
          </Popup.Header>
          <Popup.Body>
            {view.isLoading ? (
              <Skeleton height={160} />
            ) : view.isError ? (
              <Callout color="danger">{errorMessage(view.error, "Could not read this host.")}</Callout>
            ) : running.length === 0 ? (
              <DeployProxy hostId={hostId} view={view.data} />
            ) : (
              <>
                {current.length > 0 && (
                  <Box direction="column" gap={1}>
                    <Text size="xs" color="muted" transform="uppercase">Currently routed</Text>
                    {current.map((d) => (
                      <Box key={`${d.router}-${d.domain}`} direction="row" gap={2} align="center" wrap>
                        <Code>{d.domain}</Code>
                        <Badge size="sm" variant="outline" color="gray">{d.proxy}</Badge>
                        {d.tls ? <Badge size="sm" variant="outline" color="green">tls</Badge> : <Badge size="sm" variant="outline" color="warning">http</Badge>}
                        {!d.managedByKowan && <Text size="xs" color="muted">labels set by hand</Text>}
                      </Box>
                    ))}
                  </Box>
                )}
                <TextInput data-autofocus label="Domain" placeholder="app.example.com" value={domain} required
                  description="Point its DNS A/AAAA record at this host first, so Let's Encrypt can validate it."
                  onChange={(e) => setDomain(e.currentTarget.value)} />
                <Box direction="row" gap={3} wrap>
                  <TextInput label="Container port" placeholder="80" inputMode="numeric" width={140} value={port}
                    onChange={(e) => setPort(e.currentTarget.value.replace(/[^0-9]/g, ""))} />
                  {services && services.length > 1 && (
                    <Select label="Service" options={services.map((s) => ({ value: s, label: s }))} value={service}
                      onChange={(v) => setService(v)} width={200} />
                  )}
                  {running.length > 1 && (
                    <Select label="Proxy" width={220} value={proxy?.containerId}
                      options={running.map((p) => ({ value: p.containerId, label: `${p.name} (${p.kind})` }))}
                      onChange={(v) => setProxyId(v)} />
                  )}
                </Box>
                {proxy?.kind === "traefik" && (
                  <Box direction="row" gap={3} wrap align="end">
                    <Select label="Entrypoint" width={180} value={entrypoint}
                      options={Object.entries(proxy.entrypoints).map(([k, v]) => ({ value: k, label: `${k} (${v})` }))}
                      onChange={(v) => setEntrypoint(v)} />
                    <Select label="Certificate resolver" width={200} value={resolver || "__none"}
                      disabled={!tls}
                      options={[{ value: "__none", label: "none" }, ...proxy.certResolvers.map((r) => ({ value: r, label: r }))]}
                      onChange={(v) => setResolver(v === "__none" ? "" : v)} />
                    <Checkbox label="HTTPS (TLS)" checked={tls} onChange={(e) => setTls(e.currentTarget.checked)} />
                  </Box>
                )}
                <Text size="xs" color="muted">
                  Through <Code>{proxy?.name}</Code> ({proxy?.kind}). {recreateNote}
                </Text>
                {proxy?.kind === "traefik" && tls && !resolver && (
                  <Callout color="warning">No certificate resolver: Traefik serves its default self-signed certificate.</Callout>
                )}
              </>
            )}
          </Popup.Body>
          {running.length > 0 && (
            <Popup.Footer>
              {managed && (
                <Button variant="outline" color="danger" loading={unexpose.isPending} onClick={() => unexpose.mutate()}>
                  Unexpose {managed.domain}
                </Button>
              )}
              <Button variant="clear" color="neutral" onClick={onClose}>Cancel</Button>
              <Button type="submit" loading={expose.isPending} disabled={!domain.trim()}>
                {managed ? "Update" : "Expose"}
              </Button>
            </Popup.Footer>
          )}
        </form>
      </Popup.Content>
    </Popup>
  );
}

/** No proxy on the host: offer to deploy Traefik with Let's Encrypt. */
export function DeployProxy({ hostId, view }: { hostId: string; view?: HostExposure }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [httpPort, setHttpPort] = useState("80");
  const [httpsPort, setHttpsPort] = useState("443");
  const [staging, setStaging] = useState(false);
  const busy = new Set((view?.ports ?? []).filter((p) => p.scope !== "local").map((p) => p.publicPort));
  const m = useMutation({
    mutationFn: () => deployProxy(hostId, { kind: "traefik", email: email.trim(), httpPort: Number(httpPort), httpsPort: Number(httpsPort), staging }),
    onSuccess: (r) => {
      toast.success("Deploying Traefik", { description: `Deployment ${r.name} started` });
      void qc.invalidateQueries({ queryKey: ["exposure"] });
      void qc.invalidateQueries({ queryKey: ["deployments"] });
    },
    onError: (err) => toast.error("Deploy failed", { description: errorMessage(err, "Could not deploy the proxy.") }),
  });
  const conflict = busy.has(Number(httpPort)) || busy.has(Number(httpsPort));
  return (
    <Box direction="column" gap={3}>
      <Callout color="info" title="No reverse proxy on this host">
        Deploy Traefik with automatic HTTPS from Let&apos;s Encrypt (HTTP-01 challenge). Ports 80 and 443 must be reachable from the internet.
      </Callout>
      <TextInput label="Email for Let's Encrypt" type="email" required value={email} onChange={(e) => setEmail(e.currentTarget.value)} />
      <Box direction="row" gap={3} wrap>
        <TextInput label="HTTP port" width={120} value={httpPort} onChange={(e) => setHttpPort(e.currentTarget.value.replace(/[^0-9]/g, ""))} />
        <TextInput label="HTTPS port" width={120} value={httpsPort} onChange={(e) => setHttpsPort(e.currentTarget.value.replace(/[^0-9]/g, ""))} />
      </Box>
      <Checkbox label="Use the Let's Encrypt staging CA (for testing; untrusted certificates)" checked={staging}
        onChange={(e) => setStaging(e.currentTarget.checked)} />
      {conflict && <Callout color="warning">One of these ports is already published on this host.</Callout>}
      <Box direction="row" justify="end">
        <Button loading={m.isPending} disabled={!email.includes("@") || m.isSuccess} onClick={() => m.mutate()}>
          {m.isSuccess ? "Deploying…" : "Deploy Traefik"}
        </Button>
      </Box>
    </Box>
  );
}

/** Button + dialog, for detail pages. Hidden for viewers. */
export default function ExposeButton(props: Omit<ExposeDialogProps, "open" | "onClose">) {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  if (!(user?.role === "admin" || user?.role === "operator")) return null;
  return (
    <>
      <Button variant="outline" color="neutral" iconLeft="http-rounded" onClick={() => setOpen(true)}>
        Expose
      </Button>
      {open && <ExposeDialog {...props} open={open} onClose={() => setOpen(false)} />}
    </>
  );
}
