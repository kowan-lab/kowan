import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { press, toast } from "../../test/ui";

const f = (name: string, dir: string, over: Record<string, unknown> = {}) => ({
  name, path: `${dir === "/" ? "" : dir}/${name}`, size: 120, mode: "-rw-r--r--", isDir: false, modTime: 1_761_000_000, ...over,
});
const tree: Record<string, unknown[]> = {
  "/": [f("conf", "/", { isDir: true, mode: "drwxr-xr-x" }), f("app.log", "/"), f("dump.sql.gz", "/", { size: 9_000_000 }), f("data.bin", "/")],
  "/conf": [f("site.conf", "/conf")],
};
const api = {
  "GET /hosts/:h/volumes/:name/browse": (r: { query: Record<string, string> }) => tree[r.query.path] ?? [],
  "GET /hosts/:h/volumes/:name/file": (r: { query: Record<string, string> }) =>
    reply(200, r.query.path.endsWith(".bin") ? "\u0000\u0001ELF" : `contents of ${r.query.path}`, { "content-type": "text/plain" }),
  "GET /hosts/:h/volumes/:name/download": reply(200, new Blob(["x"]), { "content-type": "application/octet-stream" }),
  "GET /hosts/:h/volumes/:name/archive": reply(200, new Blob(["PK"]), { "content-type": "application/zip" }),
};
const URL = "/volume-explorer?host=local&name=shop_db";

async function ready() {
  return within(pageRoot()).findByText("app.log", {}, { timeout: 10_000 });
}

describe("volume explorer", () => {
  it("browses folders and previews text files", async () => {
    const { user } = renderApp(URL, { api });
    await ready();
    await user.click(screen.getByText("app.log"));
    expect(await screen.findByText("contents of /app.log")).toBeInTheDocument();
    await user.click(screen.getByText("data.bin"));
    expect(await screen.findByText(/looks like a binary file/i)).toBeInTheDocument();
    await user.click(screen.getByText("dump.sql.gz"));
    expect(await screen.findByText(/too large to preview/i)).toBeInTheDocument();
    await press(user, /^close$/i, pageRoot());
    await user.click(screen.getByText("conf"));
    expect(await screen.findByText("site.conf")).toBeInTheDocument();
    await user.click(screen.getByText(".."));
    expect(await screen.findByText("app.log")).toBeInTheDocument();
  });

  it("downloads files and folders and uploads into the current folder", async () => {
    const { user } = renderApp(URL, { api: { ...api, "POST /hosts/:h/volumes/:name/upload": {} } });
    await ready();
    await press(user, "Download app.log");
    await press(user, "Download conf as zip");
    await press(user, "Download folder as zip");
    await waitFor(() => expect(requests("GET", "/hosts/local/volumes/shop_db/archive")).toHaveLength(2));
    expect(requests("GET", "/hosts/local/volumes/shop_db/download")[0].query).toEqual({ path: "/app.log" });
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["hi"], "hello.txt"));
    await toast(/"hello.txt" was written to \//);
    expect(requests("POST", "/hosts/local/volumes/shop_db/upload")[0].query).toEqual({ path: "/" });
  });

  it("reports download, upload, browse and preview failures", async () => {
    const { user } = renderApp(URL, {
      api: {
        ...api,
        "GET /hosts/:h/volumes/:name/download": reply(500, { error: "helper failed" }),
        "POST /hosts/:h/volumes/:name/upload": reply(413, { error: "too large" }),
      },
    });
    await ready();
    await press(user, "Download app.log");
    await toast(/helper failed/i);
    await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, new File(["x"], "x"));
    await toast(/too large/i);
    mockApi({ "GET /hosts/:h/volumes/:name/file": reply(404, { error: "gone" }) });
    await user.click(screen.getByText("app.log"));
    expect(await screen.findAllByText(/gone/i)).not.toHaveLength(0);
    mockApi({ "GET /hosts/:h/volumes/:name/browse": reply(500, { error: "cannot list" }) });
    await press(user, "Refresh");
    expect(await screen.findAllByText(/cannot list/i)).not.toHaveLength(0);
  });

  it("asks for a host and volume, then selects them", async () => {
    localStorage.setItem("kowan_host", "all");
    const { user } = renderApp("/volume-explorer", { api });
    expect(await screen.findByText(/pick a host and volume/i, {}, { timeout: 10_000 })).toBeInTheDocument();
    await user.click(await screen.findByText("Volume"));
    await user.click(await screen.findByRole("option", { name: "orphan-data" }));
    await waitFor(() => expect(currentPath()).toBe("/volume-explorer?host=local&name=orphan-data"));
    expect(await screen.findByText("app.log")).toBeInTheDocument();
  });
});
