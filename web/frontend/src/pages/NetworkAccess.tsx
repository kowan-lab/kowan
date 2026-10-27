import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Callout, Skeleton, Text, TextArea, TextInput } from "@/ui/kit";
import { getNetworkAccess, saveNetworkAccess, type NetworkAccess as NetworkAccessData } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";

/** One address or CIDR per line (commas work too); blank lines are ignored. */
function parseLines(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function EnvList({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="grid grid-cols-[minmax(0,200px)_minmax(0,1fr)] gap-6 py-3 shadow-[inset_0_-1px_0_var(--border)] last:shadow-none">
      <span className="font-sans text-[12px] tracking-normal text-muted-foreground ">{label}</span>
      <span className="min-w-0 font-mono text-[13px] break-words">
        {values.length ? values.join(", ") : <span className="text-muted-foreground">not set: no limit</span>}
      </span>
    </div>
  );
}

export default function NetworkAccess() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const query = useQuery({ queryKey: ["network-access"], queryFn: getNetworkAccess });
  const data = query.data;

  const [ui, setUi] = useState("");
  const [edge, setEdge] = useState("");
  const [password, setPassword] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    if (!data) return;
    setUi(data.uiAllow.join("\n"));
    setEdge(data.edgeAllow.join("\n"));
  }, [data]);

  const dirty = useMemo(
    () => !!data && (parseLines(ui).join(",") !== data.uiAllow.join(",") || parseLines(edge).join(",") !== data.edgeAllow.join(",")),
    [data, ui, edge],
  );
  // Local accounts confirm with their password (SSO accounts have none here).
  const needsPassword = !user?.authSource || user.authSource === "local";

  const save = useMutation({
    mutationFn: () => saveNetworkAccess({ uiAllow: parseLines(ui), edgeAllow: parseLines(edge), ...(needsPassword ? { password } : {}) }),
    onSuccess: (res: NetworkAccessData) => {
      qc.setQueryData(["network-access"], res);
      setPassword("");
      setWarnings(res.warnings ?? []);
      notify({ color: "success", title: "Network access saved", message: "The new lists apply to the next request." });
    },
    onError: (err) => notify({ color: "danger", title: "Not saved", message: errorMessage(err, "Could not save the lists") }),
  });

  return (
    <>
      <PageHeader
        title="Network access"
        description="Limit which addresses can reach this server. Lists here can only narrow the ones set in the server's environment, never widen them."
      />

      {query.isLoading || !data ? (
        <SectionCard title="Your connection">
          <Skeleton height={40} />
        </SectionCard>
      ) : (
        <>
          <SectionCard title="Your connection" description="Saving a Web UI list that leaves this address out is refused, so you cannot lock yourself out.">
            <EnvList label="Your address" values={[data.yourAddress || "unknown"]} />
            {data.peerAddress && data.peerAddress !== data.yourAddress && (
              <EnvList label="Via proxy" values={[data.peerAddress]} />
            )}
          </SectionCard>

          <SectionCard
            title="Allowlists"
            description="One address or CIDR per line, for example 192.168.1.0/24. Leave a list empty for no limit. Webhooks and /health stay reachable from anywhere."
          >
            <form
              className="flex max-w-[720px] flex-col gap-6"
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate();
              }}
            >
              <TextArea
                label="Web UI, API and AI assistants"
                rows={5}
                spellCheck={false}
                placeholder={"192.168.1.0/24\n10.8.0.0/16"}
                value={ui}
                onChange={(e) => setUi(e.target.value)}
              />
              <TextArea
                label="Edge agents"
                description="Addresses edge agents connect from. Tunnels already open stay up until they reconnect."
                rows={4}
                spellCheck={false}
                value={edge}
                onChange={(e) => setEdge(e.target.value)}
              />
              {needsPassword && (
                <TextInput
                  type="password"
                  label="Your password"
                  description="Required to change who may connect."
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  maxWidth={360}
                />
              )}
              {warnings.length > 0 && (
                <Callout color="warning" title="Some entries let nobody in">
                  {warnings.map((w) => (
                    <div key={w}>{w}</div>
                  ))}
                </Callout>
              )}
              <div>
                <Button type="submit" loading={save.isPending} disabled={!dirty || (needsPassword && !password)}>
                  Save lists
                </Button>
              </div>
            </form>
          </SectionCard>

          <SectionCard
            title="Set in the environment"
            description="Read-only here: they change only with the server's configuration, which takes access to the machine."
          >
            <EnvList label="KOWAN_UI_ALLOW_CIDRS" values={data.env.uiAllow} />
            <EnvList label="KOWAN_EDGE_ALLOW_CIDRS" values={data.env.edgeAllow} />
            <EnvList label="KOWAN_TRUSTED_PROXIES" values={data.env.trustedProxies} />
            <EnvList label="KOWAN_PROXY_HEADER" values={[data.env.proxyHeader]} />
            <Text size="sm" color="muted" className="pt-3">
              Trusted proxies decide which address the server believes, so they can only be set in the environment.
            </Text>
          </SectionCard>
        </>
      )}
    </>
  );
}
