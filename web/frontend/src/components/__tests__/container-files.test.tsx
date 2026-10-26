import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ContainerFiles from "../containerfiles/ContainerFiles";
import { mockApi, reply, requests } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { dialog, press, toast } from "../../test/ui";

const F = "/hosts/:h/containers/:id/files";
const entry = (name: string, dir: string, over: Record<string, unknown> = {}) => ({
  name, path: `${dir === "/" ? "" : dir}/${name}`, size: 1234, mode: "-rw-r--r--", kind: "file", isDir: false, isLink: false, linkTarget: "", modTime: 1_761_000_000, ...over,
});
const listings: Record<string, unknown[]> = {
  "/": [entry("etc", "/", { kind: "dir", isDir: true }), entry("app", "/", { kind: "symlink", isLink: true, linkTarget: "/srv/app" }), entry("notes.txt", "/")],
  "/etc": [entry("nginx.conf", "/etc"), entry("compose.yaml", "/etc"), entry("big.bin", "/etc"), entry("blob", "/etc")],
};
const list = (running = true) => (r: { query: Record<string, string> }) => {
  const path = r.query.path === "/app" ? "/srv/app" : r.query.path;
  return { path, requested: r.query.path, entries: listings[path] ?? [], truncated: path === "/etc", method: running ? "exec" : "archive", running };
};
const content = (r: { query: Record<string, string> }) => {
  if (r.query.path.endsWith("big.bin")) return { path: r.query.path, size: 9e7, tooLarge: true, limit: 1e6, binary: false, content: "" };
  if (r.query.path.endsWith("blob")) return { path: r.query.path, size: 10, tooLarge: false, limit: 1e6, binary: true, content: "" };
  return { path: r.query.path, size: 12, tooLarge: false, limit: 1e6, binary: false, content: "worker_processes 1;\n" };
};

function render(running = true) {
  const r = renderWithProviders(<ContainerFiles hostId="local" containerId="c1" />);
  mockApi({ [`GET ${F}`]: list(running), [`GET ${F}/content`]: content });
  return r;
}

describe("container files", () => {
  it("browses folders, links and the breadcrumb", async () => {
    const { user } = render();
    await user.click(await screen.findByText("etc", {}, { timeout: 5000 }));
    expect(await screen.findByText("nginx.conf")).toBeInTheDocument();
    expect(screen.getByText(/this folder is large/i)).toBeInTheDocument();
    await press(user, "Up one level");
    await user.click(await screen.findByText("app"));
    // The server resolved the link: the breadcrumb follows the real path.
    await waitFor(() => expect(screen.getByLabelText("Go to path")).toHaveValue("/srv/app"));
    expect(screen.getByText(/this folder is empty/i)).toBeInTheDocument();
    await press(user, "Root directory");
    await user.clear(screen.getByLabelText("Go to path"));
    await user.type(screen.getByLabelText("Go to path"), "/etc{Enter}");
    expect(await screen.findByText("compose.yaml")).toBeInTheDocument();
  });

  it("navigates with the keyboard", async () => {
    const { user } = render();
    const listEl = await screen.findByLabelText("Files in /", {}, { timeout: 5000 });
    await screen.findByText("etc");
    listEl.focus();
    fireEvent.keyDown(listEl, { key: "ArrowDown" });
    fireEvent.keyDown(listEl, { key: "ArrowUp" });
    fireEvent.keyDown(listEl, { key: "Enter" });
    expect(await screen.findByText("nginx.conf")).toBeInTheDocument();
    const etc = screen.getByLabelText("Files in /etc");
    fireEvent.keyDown(etc, { key: "F2" });
    expect(await dialog(/rename/i)).toBeInTheDocument();
    await press(user, /^cancel$/i, await dialog(/rename/i));
    fireEvent.keyDown(etc, { key: "Delete" });
    await press(user, /^cancel$/i, await dialog(/delete/i));
    fireEvent.keyDown(etc, { key: "/" });
    expect(screen.getByLabelText("Go to path")).toHaveFocus();
    fireEvent.keyDown(etc, { key: "Backspace" });
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
  });

  it("previews and saves a text file", async () => {
    const { user } = render();
    await user.click(await screen.findByText("notes.txt", {}, { timeout: 5000 }));
    const area = await screen.findByLabelText("Contents of notes.txt");
    expect(area).toHaveValue("worker_processes 1;\n");
    await user.type(area, "x");
    expect(screen.getByText("modified")).toBeInTheDocument();
    await press(user, /^revert$/i);
    expect(screen.queryByText("modified")).toBeNull();
    await user.type(area, "y");
    fireEvent.keyDown(area, { key: "s", ctrlKey: true });
    await toast(/^saved$/i);
    expect(requests("PUT", "/hosts/local/containers/c1/files/content")[0].query).toEqual({ path: "/notes.txt" });
    await press(user, "Close preview");
    expect(screen.queryByLabelText("Contents of notes.txt")).toBeNull();
  });

  it("explains files it cannot edit and reports read and save errors", async () => {
    const { user } = render();
    await user.click(await screen.findByText("etc", {}, { timeout: 5000 }));
    await user.click(await screen.findByText("big.bin"));
    expect(await screen.findByText(/above the .* editor limit/i)).toBeInTheDocument();
    await user.click(screen.getByText("blob"));
    expect(await screen.findByText(/looks like a binary file/i)).toBeInTheDocument();
    mockApi({ [`GET ${F}/content`]: reply(403, { error: "permission denied" }) });
    await user.click(screen.getByText("nginx.conf"));
    expect(await screen.findByText(/permission denied/i)).toBeInTheDocument();
  });

  it("creates folders, renames, deletes, uploads and downloads", async () => {
    const { user } = render();
    await screen.findByText("notes.txt", {}, { timeout: 5000 });
    await press(user, /new folder/i);
    let d = await dialog(/new folder/i);
    await user.type(within(d).getByLabelText(/name \(in \/\)/i), "data");
    await press(user, /^create$/i, d);
    await toast(/folder created/i);
    expect(requests("POST", "/hosts/local/containers/c1/files/mkdir")[0].body).toEqual({ path: "/", name: "data" });

    await press(user, "Rename notes.txt");
    d = await dialog(/rename/i);
    const name = within(d).getByLabelText(/new name/i);
    await user.clear(name);
    await user.type(name, "readme.txt");
    await press(user, /^rename$/i, d);
    await toast(/renamed/i);
    expect(requests("POST", "/hosts/local/containers/c1/files/rename")[0].body).toEqual({ path: "/notes.txt", name: "readme.txt" });

    await press(user, "Delete etc");
    d = await dialog(/delete/i);
    expect(within(d).getByText(/and everything inside it/i)).toBeInTheDocument();
    await press(user, /^delete$/i, d);
    await toast(/deleted/i);

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["hello"], "hello.txt"));
    await toast(/hello.txt → \//);

    mockApi({ [`GET ${F}/download`]: reply(200, new Blob(["tar"]), { "content-type": "application/x-tar" }) });
    await press(user, "Download folder");
    await press(user, "Download notes.txt");
    await waitFor(() => expect(requests("GET", "/hosts/local/containers/c1/files/download")).toHaveLength(2));
  });

  it("reports action failures", async () => {
    const { user } = render();
    mockApi({
      [`POST ${F}/mkdir`]: reply(409, { error: "exists" }),
      [`POST ${F}/upload`]: reply(413, { error: "too big" }),
      [`GET ${F}/download`]: reply(500, { error: "tar failed" }),
    });
    await screen.findByText("notes.txt", {}, { timeout: 5000 });
    await press(user, /new folder/i);
    const d = await dialog(/new folder/i);
    await user.type(within(d).getByLabelText(/name/i), "etc");
    await press(user, /^create$/i, d);
    await toast(/exists/i);
    await press(user, /^cancel$/i, d);
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["x"], "x.bin"));
    await toast(/too big/i);
    await press(user, "Download notes.txt");
    await toast(/tar failed/i);
  });

  it("is read-only on a stopped container and lists failures", async () => {
    const { user } = render(false);
    await screen.findByText("notes.txt", {}, { timeout: 5000 });
    expect(screen.getByRole("button", { name: "Rename notes.txt" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Delete notes.txt" })).toBeDisabled();
    mockApi({ [`GET ${F}`]: reply(404, { error: "no such directory" }) });
    await user.clear(screen.getByLabelText("Go to path"));
    await user.type(screen.getByLabelText("Go to path"), "/nope{Enter}");
    expect(await screen.findByText(/no such directory/i)).toBeInTheDocument();
  });
});
