import { screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import { ComposeBuilder } from "../ComposeBuilder";
import ImageInput from "../ImageInput";
import { mockApi, requests } from "../../test/api";
import { renderWithProviders } from "../../test/render";

let latest = "";
function Harness({ initial, validate }: { initial: string; validate?: boolean }) {
  const [value, setValue] = useState(initial);
  latest = value;
  return (
    <ComposeBuilder
      value={value}
      onChange={(v) => {
        latest = v;
        setValue(v);
      }}
      validation={validate ? { hostId: "local" } : undefined}
    />
  );
}
const doc = () => parse(latest) as { version?: string; services: Record<string, Record<string, unknown>> };

const RICH = `version: "3.9"
services:
  web:
    image: nginx:1.27
    ports: ["8080:80", 443, "5353:53/udp"]
    environment: [MODE=prod, "EMPTY"]
    volumes: ["data:/data:ro", "/cache", "./conf:/etc/nginx:rw"]
    restart: unless-stopped
    command: ["nginx", "-g", "daemon off;", 'say "hi"']
    depends_on: [db]
  db:
    image: postgres:16
    environment: { POSTGRES_PASSWORD: secret }
    command: postgres -c max_connections=200
    depends_on: [cache]
`;

describe("compose builder", () => {
  it("parses a rich compose into the easy editor and round-trips edits", async () => {
    const { user } = renderWithProviders(<Harness initial={RICH} />);
    const names = await screen.findAllByLabelText(/service name/i);
    expect(names.map((n) => (n as HTMLInputElement).value)).toEqual(["web", "db"]);
    expect(screen.getByDisplayValue("nginx:1.27")).toBeInTheDocument();
    expect(screen.getByDisplayValue("5353")).toBeInTheDocument();
    expect(screen.getByDisplayValue("MODE")).toBeInTheDocument();
    expect(screen.getByDisplayValue("./conf")).toBeInTheDocument();

    // Rename and change the command: the YAML is rebuilt, version kept.
    await user.clear(names[1]);
    await user.type(names[1], "postgres");
    const cmds = screen.getAllByLabelText(/^command/i);
    await user.clear(cmds[1]);
    await user.type(cmds[1], `postgres -c "shared_buffers=1GB" -c a\\ b ''`);
    const d = doc();
    expect(d.version).toBe("3.9");
    expect(Object.keys(d.services)).toEqual(["web", "postgres"]);
    expect(d.services.postgres.command).toEqual(["postgres", "-c", "shared_buffers=1GB", "-c", "a b", ""]);
    expect(d.services.web.ports).toEqual(["8080:80", "443", "5353:53/udp"]);
    expect(d.services.web.volumes).toEqual(["data:/data:ro", "/cache", "./conf:/etc/nginx"]);
    // An edit elsewhere must not re-split a quoted argument.
    expect(d.services.web.command).toEqual(["nginx", "-g", "daemon off;", 'say "hi"']);
    expect(d.services.web.depends_on).toEqual(["db"]);
    expect(d.services.web.environment).toEqual({ MODE: "prod", EMPTY: "" });
  });

  it("adds and removes services, ports, env and volumes", async () => {
    const { user } = renderWithProviders(<Harness initial="" />);
    expect(await screen.findByText(/no services yet/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /add service/i }));
    await user.click(screen.getByRole("button", { name: /add service/i }));
    expect(Object.keys(doc().services)).toEqual(["service", "service2"]);
    await user.type(screen.getAllByLabelText(/^image/i)[0], "redis:7");
    // Each card has three "Add" buttons: ports, environment, volumes.
    const adds = () => screen.getAllByRole("button", { name: /^add$/i });
    await user.click(adds()[0]);
    await user.type(screen.getAllByPlaceholderText("container")[0], "6379");
    await user.type(screen.getAllByPlaceholderText("host")[0], "6379");
    await user.click(adds()[1]);
    await user.type(screen.getAllByPlaceholderText("KEY")[0], "APPENDONLY");
    await user.type(screen.getAllByPlaceholderText("value")[0], "yes");
    await user.click(adds()[2]);
    await user.type(screen.getAllByPlaceholderText("/container/path")[0], "/data");
    await user.type(screen.getAllByPlaceholderText(/source \/ named volume/i)[0], "redis-data");
    await user.type(screen.getAllByPlaceholderText("service name")[0], "service2{Enter}");
    let d = doc();
    expect(d.services.service).toMatchObject({
      image: "redis:7", ports: ["6379:6379"], environment: { APPENDONLY: "yes" }, volumes: ["redis-data:/data"], depends_on: ["service2"],
    });
    await user.click(screen.getAllByRole("button", { name: "Remove port" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Remove env" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Remove volume" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Remove service" })[1]);
    d = doc();
    expect(Object.keys(d.services)).toEqual(["service"]);
    expect(d.services.service.ports).toBeUndefined();
    // Clearing the name keeps the service under a placeholder key.
    await user.clear(screen.getByLabelText(/service name/i));
    expect(screen.getAllByLabelText(/service name/i)).toHaveLength(1);
    expect(Object.keys(doc().services)).toHaveLength(1);
  });

  it("switches to YAML for compose it cannot represent", async () => {
    renderWithProviders(<Harness initial={"services:\n  web:\n    image: nginx\nnetworks: { edge: {} }\n"} />);
    await waitFor(() => expect(screen.getByRole("tab", { name: /yaml/i })).toHaveAttribute("aria-selected", "true"));
    expect(screen.queryByLabelText(/service name/i)).toBeNull();
  });

  it("treats broken YAML and odd shapes as not representable", async () => {
    const bad = [
      "services: [a, b",
      "- a\n- b\n",
      "services: 3\n",
      "services:\n  web: 1\n",
      "services:\n  web:\n    healthcheck: {}\n",
      "services:\n  web:\n    image: [x]\n",
      "services:\n  web:\n    ports: 80\n",
      "services:\n  web:\n    ports: [{ target: 53, published: 5353 }]\n",
      "services:\n  web:\n    ports: [\"127.0.0.1:80:80\"]\n",
      "services:\n  web:\n    depends_on: { db: { condition: service_healthy } }\n",
      "services:\n  web:\n    volumes: [\"a:b:c:d\"]\n",
      "services:\n  web:\n    volumes: [\"a:b:z\"]\n",
      "services:\n  web:\n    volumes: x\n",
      "services:\n  web:\n    volumes: [{ a: 1 }]\n",
      "services:\n  web:\n    restart: [x]\n",
      "services:\n  web:\n    command: { a: 1 }\n",
      "services:\n  web:\n    depends_on: 3\n",
      "services:\n  web:\n    environment: 3\n",
    ];
    for (const text of bad) {
      const r = renderWithProviders(<Harness initial={text} />);
      await waitFor(() => expect(screen.getByRole("tab", { name: /yaml/i })).toHaveAttribute("aria-selected", "true"));
      r.unmount();
    }
  });

  it("shows validation diagnostics and applies a fix", async () => {
    const { user } = renderWithProviders(<Harness initial={"services:\n  web:\n    image: nginx\n"} validate />);
    mockApi({
      "POST /compose/validate": {
        engine: "lint", checks: [], summary: { errors: 0, warnings: 1, infos: 0 },
        diagnostics: [{ severity: "warning", code: "restart", message: "No restart policy", line: 2, column: 3, path: ["services", "web"],
          fix: { title: "Add restart: unless-stopped", patch: [{ op: "set", path: ["services", "web", "restart"], value: "unless-stopped" }] } }],
      },
    });
    expect(await screen.findByText(/no restart policy/i, {}, { timeout: 5000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /add restart|apply fix|^fix/i }));
    await waitFor(() => expect(doc().services.web.restart).toBe("unless-stopped"));
    expect(requests("POST", "/compose/validate").length).toBeGreaterThan(0);
  });
});

describe("image input", () => {
  it("suggests local images and browses a registry", async () => {
    let value = "";
    function H() {
      const [v, setV] = useState("");
      return (
        <ImageInput
          value={v}
          onChange={(x) => {
            value = x;
            setV(x);
          }}
        />
      );
    }
    mockApi({
      "GET /credentials/:id/catalog": { repositories: ["acme/api", "acme/web"] },
      "GET /credentials/:id/tags": { tags: ["1.0", "2.0"] },
    });
    const { user } = renderWithProviders(<H />);
    await waitFor(() => expect(document.querySelectorAll("datalist option").length).toBeGreaterThan(0));
    await user.click(screen.getByRole("button", { name: /browse a registry/i }));
    await user.click(await screen.findByText("Select a registry"));
    await user.click(await screen.findByRole("option", { name: "private" }));
    await user.type(await screen.findByPlaceholderText(/filter repositories/i), "web");
    expect(screen.queryByText("acme/api")).toBeNull();
    await user.click(screen.getByText("acme/web"));
    await user.click(await screen.findByText("2.0"));
    await waitFor(() => expect(value).toBe("registry.example.com/acme/web:2.0"));
  });
});
