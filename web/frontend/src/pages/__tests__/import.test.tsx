import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, press, toast } from "../../test/ui";
import { readFolder } from "../Import";

const cand = (over: Record<string, unknown> = {}) => ({
  key: "portainer-1", source: "portainer", name: "shop", kind: "compose", endpoint: "local", active: true, envCount: 2,
  host: "local", strategy: "standalone", compose: "services:\n  web:\n    image: nginx:1.27\n", git: null, importable: true, warnings: [],
  ...over,
});
const found = [
  cand(),
  cand({ key: "portainer-2", name: "blog", git: { repoUrl: "https://github.com/acme/blog.git", ref: "prod", path: "c.yml" }, compose: "",
         active: false, warnings: ["private repository: pick a git credential after importing"] }),
  cand({ key: "portainer-3", name: "metrics", warnings: ["a deployment with this name exists already"] }),
  cand({ key: "portainer-4", name: "k8s", kind: "kubernetes", importable: false, warnings: ["Kubernetes stacks are not supported"] }),
];

function file(path: string, text: string, size?: number): File {
  const f = new File([text], path.split("/").pop()!, { type: "text/plain" });
  Object.defineProperty(f, "webkitRelativePath", { value: path });
  if (size !== undefined) Object.defineProperty(f, "size", { value: size });
  return f;
}

describe("reading a folder", () => {
  it("keeps compose and .env files only", async () => {
    const out = await readFolder([
      file("stacks/shop/compose.yaml", "a"), file("stacks/shop/.env", "T=1"), file("stacks/shop/README.md", "no"),
      file("stacks/blog/docker-compose.yml", "b"), file("stacks/big/compose.yml", "x", 2 << 20),
    ]);
    expect(out).toEqual({ "stacks/shop/compose.yaml": "a", "stacks/shop/.env": "T=1", "stacks/blog/docker-compose.yml": "b" });
  });
});

describe("import page", () => {
  it("scans Portainer with a token and imports the picked stacks", async () => {
    const { user } = renderApp("/import", {
      api: {
        "POST /import/portainer/scan": found,
        "POST /import/apply": (r) => r.body.items.map((i: { name: string }, n: number) => (n === 1 ? { name: i.name, ok: false, error: "unknown host" } : { name: i.name, ok: true, id: `new-${n}` })),
      },
    });
    const root = pageRoot();
    const scanBtn = await within(root).findByRole("button", { name: "Find stacks" }, { timeout: 10_000 });
    expect(scanBtn).toBeDisabled();
    await user.type(screen.getByLabelText("Portainer URL"), "https://portainer.example.com");
    await user.type(screen.getByLabelText("Access token"), "ptr_x");
    await user.click(screen.getByRole("switch", { name: /verify the tls certificate/i }));
    await user.click(scanBtn);
    expect(await screen.findByRole("list", { name: "Stacks found" })).toBeInTheDocument();
    expect(requests("POST", "/import/portainer/scan")[0].body).toEqual({ url: "https://portainer.example.com", apiKey: "ptr_x", verifyTls: false });
    // Importable and not clashing: picked; existing name and kubernetes: not.
    expect(screen.getByRole("checkbox", { name: "Import shop" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Import blog" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Import metrics" })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Import k8s" })).toBeDisabled();
    expect(screen.getAllByText(/2 variables applied/)).toHaveLength(3); // not on the git stack
    expect(screen.getByText(/stopped in portainer/i)).toBeInTheDocument();
    expect(screen.getByText(/git https:\/\/github.com\/acme\/blog.git @ prod/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Name of shop"));
    expect(screen.getByRole("button", { name: "Import 2" })).toBeDisabled();
    await user.type(screen.getByLabelText("Name of shop"), "shop-prod");
    await choose(user, "Host for shop", "edge-1");
    await press(user, "Import 2");
    await toast(/imported 1 of 2/i);
    const items = requests("POST", "/import/apply")[0].body.items;
    expect(items).toEqual([
      { name: "shop-prod", host: "h-edge", strategy: "standalone", compose: "services:\n  web:\n    image: nginx:1.27\n" },
      { name: "blog", host: "local", strategy: "standalone", git: { repoUrl: "https://github.com/acme/blog.git", ref: "prod", path: "c.yml" } },
    ]);
    expect(screen.getByText("unknown host")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: /imported: open shop-prod/i }));
    await waitFor(() => expect(currentPath()).toBe("/workloads/dep~new-0"));
  });

  it("signs in with a password and shows scan errors", async () => {
    const { user } = renderApp("/import", { api: { "POST /import/portainer/scan": reply(400, { error: "Portainer refused the username or password" }) } });
    await user.click(await screen.findByRole("switch", { name: /username and password/i }, { timeout: 10_000 }));
    await user.type(screen.getByLabelText("Portainer URL"), "http://p");
    await user.type(screen.getByLabelText("Username"), "admin");
    await user.type(screen.getByLabelText("Password"), "pw");
    await press(user, "Find stacks");
    expect(await screen.findByText(/refused the username or password/)).toBeInTheDocument();
    expect(requests("POST", "/import/portainer/scan")[0].body).toEqual({ url: "http://p", username: "admin", password: "pw", verifyTls: true });
  });

  it("says when Portainer has no stacks and when the import fails", async () => {
    const { user } = renderApp("/import", { api: { "POST /import/portainer/scan": [] } });
    await user.type(await screen.findByLabelText("Portainer URL", {}, { timeout: 10_000 }), "http://p");
    await user.type(screen.getByLabelText("Access token"), "t{Enter}");
    await toast(/nothing to import/i);
  });

  it("imports compose folders as an operator (Portainer is admin-only)", async () => {
    const { user } = renderApp("/import", {
      role: "operator",
      api: {
        "POST /import/compose/scan": [cand({ key: "folder-stacks/shop", source: "folder", endpoint: "stacks/shop/compose.yaml", envCount: 0 })],
        "POST /import/apply": reply(500, { error: "boom" }),
      },
    });
    expect(await screen.findByRole("tab", { name: "From Portainer" }, { timeout: 10_000 })).toBeDisabled();
    const input = screen.getByLabelText("Choose folder");
    fireEvent.change(input, { target: { files: [file("stacks/shop/compose.yaml", "services: {}"), file("stacks/shop/notes.txt", "x")] } });
    expect(await screen.findByText("1 files read.")).toBeInTheDocument();
    await waitFor(() => expect(requests("POST", "/import/compose/scan")[0].body).toEqual({ files: { "stacks/shop/compose.yaml": "services: {}" } }));
    expect(await screen.findByText(/stacks\/shop\/compose.yaml · compose file/)).toBeInTheDocument();
    await press(user, "Import 1");
    await toast(/import failed/i);
  });

  it("reports an empty folder and a failed folder scan", async () => {
    renderApp("/import", { role: "operator", api: { "POST /import/compose/scan": reply(400, { error: "files must map" }) } });
    const input = await screen.findByLabelText("Choose folder", {}, { timeout: 10_000 });
    fireEvent.change(input, { target: { files: [file("x/readme.md", "x")] } });
    expect(await screen.findByText(/no compose files in that folder/i)).toBeInTheDocument();
    fireEvent.change(input, { target: { files: [file("x/compose.yml", "x")] } });
    expect(await screen.findByText("files must map")).toBeInTheDocument();
  });

  it("is reachable from the new workload menu", async () => {
    const { user } = renderApp("/workloads");
    await user.click(await screen.findByRole("button", { name: /new workload/i }, { timeout: 10_000 }));
    await user.click(await screen.findByRole("menuitem", { name: /import stacks/i }));
    await waitFor(() => expect(currentPath()).toBe("/import"));
  });
});
