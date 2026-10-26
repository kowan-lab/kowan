import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { Button, Callout, Code, CopyButton, Table, Tabs, type Column } from "@/ui/kit";
import { getToken } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";

/**
 * Setup page for Kowan's built-in MCP server (endpoint POST /mcp, see
 * docs/mcp.md). Shows the endpoint, copy-paste setup for common clients, and
 * the tools the current user's role can call, read live from the server.
 */
interface McpTool {
  name: string;
  title?: string;
  description?: string;
  annotations?: { readOnlyHint?: boolean; destructiveHint?: boolean; title?: string };
}

async function rpc(method: string, params: object, session?: string) {
  const res = await fetch("/mcp", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${getToken() ?? ""}`,
      "MCP-Protocol-Version": "2025-06-18",
      ...(session ? { "Mcp-Session-Id": session } : {}),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: Math.floor(Math.random() * 1e9), method, params }),
  });
  if (!res.ok) throw new Error(`MCP ${method}: HTTP ${res.status}`);
  const text = await res.text();
  // A server may answer as SSE: take the last data line.
  const json = text.trim().startsWith("{") ? JSON.parse(text) : JSON.parse(text.split("\n").filter((l) => l.startsWith("data:")).pop()!.slice(5));
  if (json.error) throw new Error(json.error.message);
  return { result: json.result, session: res.headers.get("Mcp-Session-Id") ?? session };
}

async function listTools(): Promise<McpTool[]> {
  const init = await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "kowan-console", version: "1" } });
  const list = await rpc("tools/list", {}, init.session ?? undefined);
  if (init.session) {
    void fetch("/mcp", { method: "DELETE", keepalive: true, headers: { "Mcp-Session-Id": init.session, Authorization: `Bearer ${getToken() ?? ""}` } });
  }
  return list.result.tools as McpTool[];
}

function access(t: McpTool): { label: string; tone: string } {
  if (t.annotations?.destructiveHint) return { label: "destructive", tone: "text-[var(--tone-red-text)]" };
  if (t.annotations?.readOnlyHint) return { label: "read", tone: "text-muted-foreground" };
  return { label: "action", tone: "text-[var(--tone-amber-text)]" };
}

export default function AiAssistants() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const isAdmin = user?.role === "admin";
  const url = `${window.location.origin}/mcp`;
  const tools = useQuery({ queryKey: ["mcp-tools", user?.role], queryFn: listTools, staleTime: 60_000, retry: false });

  const claudeCode = `claude mcp add --transport http kowan ${url} \\\n  --header "Authorization: Bearer <KOWAN_API_KEY>"`;
  const desktop = JSON.stringify(
    { mcpServers: { kowan: { command: "kowanctl", args: ["mcp"], env: { KOWAN_URL: window.location.origin, KOWAN_API_KEY: "<KOWAN_API_KEY>" } } } },
    null,
    2,
  );
  const cursor = JSON.stringify({ mcpServers: { kowan: { url, headers: { Authorization: "Bearer <KOWAN_API_KEY>" } } } }, null, 2);

  const columns: Column<McpTool>[] = [
    { key: "name", header: "Tool", sortable: true, accessor: (t) => t.name, cell: (t) => <span className="font-medium">{t.name}</span> },
    { key: "access", header: "Access", sortable: true, accessor: (t) => access(t).label, cell: (t) => <span className={access(t).tone}>{access(t).label}</span> },
    { key: "description", header: "What it does", grow: true, truncate: true, accessor: (t) => t.description ?? t.title ?? "", cell: (t) => <span className="text-muted-foreground" title={t.description}>{t.description ?? t.title ?? ""}</span> },
  ];

  return (
    <>
      <PageHeader
        title="AI assistants"
        description="Kowan includes an MCP server, so Claude and other AI assistants can inspect and operate your hosts with the permissions of an API key."
        actions={isAdmin && <Button variant="outline" iconLeft="key-outline-rounded" onClick={() => navigate("/api-keys")}>Create API key</Button>}
      />

      <SectionCard title="Endpoint" description="Streamable HTTP. Authenticate with an API key; its role decides which tools the assistant sees.">
        <div className="flex max-w-[640px] items-center gap-3 border bg-sunken/60 px-5 py-3">
          <code className="min-w-0 flex-1 truncate font-mono text-[13px]">{url}</code>
          <CopyButton value={url} label="Copy endpoint URL" />
        </div>
        <Callout color="info">
          Use a dedicated key per assistant. A viewer key can only read; an operator key can start, stop and deploy; an admin key can also remove and prune, and always needs an explicit confirmation from the assistant. Every action is written to the audit log.
        </Callout>
      </SectionCard>

      <SectionCard title="Connect a client">
        <Tabs defaultValue="claude-code" variant="segmented">
          <Tabs.List>
            <Tabs.Trigger value="claude-code">Claude Code</Tabs.Trigger>
            <Tabs.Trigger value="desktop">Claude Desktop</Tabs.Trigger>
            <Tabs.Trigger value="cursor">Cursor and others</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Panel value="claude-code">
            <div className="flex max-w-[760px] flex-col gap-3">
              <p className="m-0 text-[13px] text-muted-foreground">Run in a terminal, replacing the key:</p>
              <div className="relative"><Code block>{claudeCode}</Code><span className="absolute top-2 right-2"><CopyButton value={claudeCode} /></span></div>
            </div>
          </Tabs.Panel>
          <Tabs.Panel value="desktop">
            <div className="flex max-w-[760px] flex-col gap-3">
              <p className="m-0 text-[13px] text-muted-foreground">Claude Desktop starts local servers over stdio. Install the Kowan CLI, then add this to claude_desktop_config.json; <code className="font-mono">kowanctl mcp</code> bridges to this server.</p>
              <div className="relative"><Code block>{desktop}</Code><span className="absolute top-2 right-2"><CopyButton value={desktop} /></span></div>
            </div>
          </Tabs.Panel>
          <Tabs.Panel value="cursor">
            <div className="flex max-w-[760px] flex-col gap-3">
              <p className="m-0 text-[13px] text-muted-foreground">Any client that supports remote MCP servers over HTTP (Cursor, Windsurf, VS Code) takes the URL and an Authorization header:</p>
              <div className="relative"><Code block>{cursor}</Code><span className="absolute top-2 right-2"><CopyButton value={cursor} /></span></div>
            </div>
          </Tabs.Panel>
        </Tabs>
      </SectionCard>

      <SectionCard title="Tools available to you" description={`What an assistant with ${/^[aeiou]/i.test(user?.role ?? "") ? "an" : "a"} ${user?.role ?? "user"} key can call.`} noBodyPadding>
        {tools.isError ? (
          <div className="px-[var(--page-px)] py-6">
            <Callout color="warning" title="Could not list the tools">{(tools.error as Error).message}. The MCP endpoint may be disabled or not available in this version.</Callout>
          </div>
        ) : (
          <Table<McpTool> columns={columns} data={tools.data} rowKey={(t) => t.name} loading={tools.isLoading} globalSearch searchPlaceholder="Search tools…" emptyText="No tools available for this role." />
        )}
      </SectionCard>
    </>
  );
}
