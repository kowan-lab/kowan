import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { requests, reply } from "../../test/api";
import { currentPath, pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const ready = () => within(pageRoot()).findByText("postgres:16", {}, { timeout: 10_000 });

describe("images page", () => {
  it("pulls an image", async () => {
    const { user } = renderApp("/images", { api: { "POST /hosts/:h/images/pull": reply(200, "{}\n", { "content-type": "application/x-ndjson" }) } });
    await ready();
    await press(user, /pull image/i, pageRoot());
    const d = await dialog(/pull image/i);
    await user.type(within(d).getByLabelText(/image reference/i), "redis:7");
    await press(user, /^pull$/i, d);
    await toast(/image pulled/i);
    expect(requests("POST", "/hosts/local/images/pull")[0].body).toMatchObject({ ref: "redis:7" });
  });

  it("builds from an inline Dockerfile and shows the output", async () => {
    const { user } = renderApp("/images", { api: {
      "POST /hosts/:h/build": { success: true, imageId: "sha256:" + "d".repeat(64), output: "Step 1/1 : FROM alpine" },
    } });
    await ready();
    await press(user, /build image/i, pageRoot());
    const d = await dialog(/build image/i);
    await user.type(within(d).getByLabelText(/^tag/i), "app:1");
    await user.clear(within(d).getByLabelText(/^dockerfile/i)); // prefilled with an example
    await user.type(within(d).getByLabelText(/^dockerfile/i), "FROM alpine");
    await press(user, /^build$/i, d);
    expect(await within(d).findByText(/step 1\/1/i)).toBeInTheDocument();
    expect(requests("POST", "/hosts/local/build")[0].body).toMatchObject({ tag: "app:1", dockerfile: "FROM alpine" });
  });

  it("reports a failed build from git", async () => {
    const { user } = renderApp("/images", { api: { "POST /hosts/:h/build": { success: false, output: "git clone failed" } } });
    await ready();
    await press(user, /build image/i, pageRoot());
    const d = await dialog(/build image/i);
    await user.type(within(d).getByLabelText(/^tag/i), "app:2");
    await user.click(within(d).getByRole("tab", { name: /git repository/i }));
    await user.type(within(d).getByLabelText(/git url/i), "https://github.com/acme/app.git");
    await press(user, /^build$/i, d);
    expect(await within(d).findByText(/git clone failed/i)).toBeInTheDocument();
    expect(requests("POST", "/hosts/local/build")[0].body).toMatchObject({ gitUrl: "https://github.com/acme/app.git" });
  });

  it("removes with force and prunes everything unused", async () => {
    const { user } = renderApp("/images", { api: { "POST /hosts/:h/prune/:kind": { spaceReclaimed: 2 * 1024 * 1024 } } });
    await ready();
    await user.click(within(pageRoot()).getAllByRole("button", { name: /remove image/i })[0]);
    let d = await dialog(/remove image/i);
    await user.click(within(d).getByLabelText(/force remove/i));
    await press(user, /^remove$/i, d);
    await toast(/image removed/i);
    expect(requests("DELETE", "/hosts/local/images/*")[0].query.force).toBe("true");
    await press(user, /prune unused/i, pageRoot());
    d = await dialog(/prune images/i);
    await user.click(within(d).getByLabelText(/all unused/i));
    await press(user, /^prune$/i, d);
    await toast(/reclaimed 2\.0 mb/i);
    expect(requests("POST", "/hosts/local/prune/images")[0].query.all).toBe("true");
  });

  it("opens an image", async () => {
    const { user } = renderApp("/images", { api: {
      "GET /hosts/:h/images/:id/inspect": { inspect: { Id: "sha256:" + "a".repeat(64), RepoTags: ["nginx:1.27"], Config: { Env: [] } }, history: [] },
    } });
    await ready();
    await user.click(within(pageRoot()).getByText("nginx:1.27"));
    await waitFor(() => expect(currentPath()).toMatch(/^\/images\//));
    expect(await screen.findAllByText(/nginx:1\.27/)).not.toHaveLength(0);
  });

  it("shows a load error", async () => {
    renderApp("/images", { api: { "GET /hosts/:h/images": reply(502, { error: "daemon gone" }) } });
    expect(await within(pageRoot()).findByText(/failed to load images/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });
});

describe("images page: validation and failures", () => {
  it("validates the build form, sends build args and pushes", async () => {
    const { user } = renderApp("/images", { api: { "POST /hosts/:h/build": { success: true, imageId: "sha256:e", output: "pushed" } } });
    await ready();
    await press(user, /build image/i, pageRoot());
    const d = await dialog(/build image/i);
    await user.type(within(d).getByLabelText(/^tag/i), "app:3");
    await user.clear(within(d).getByLabelText(/^dockerfile/i));
    await press(user, /^build$/i, d);
    await toast(/dockerfile is required/i);
    await user.click(within(d).getByRole("tab", { name: /git repository/i }));
    await user.type(within(d).getByLabelText(/git url/i), "https://github.com/acme/app.git");
    await user.type(within(d).getByLabelText(/git ref/i), "v2");
    await user.type(within(d).getByLabelText(/context subdirectory/i), "api");
    await user.click(within(d).getByRole("button", { name: /^add/i }));
    await user.type(within(d).getAllByPlaceholderText(/key|name|arg/i)[0], "VERSION");
    await user.type(within(d).getAllByPlaceholderText(/value/i)[0], "2.0");
    await user.click(within(d).getByRole("switch", { name: /no cache/i }));
    await user.click(within(d).getByRole("switch", { name: /push to registry/i }));
    await press(user, /^build$/i, d);
    await toast(/registry is required/i);
    await choose(user, /^registry/i, /private/i, d);
    await press(user, /^build$/i, d);
    expect(await within(d).findByText("pushed")).toBeInTheDocument();
    expect(requests("POST", "/hosts/local/build")[0].body).toMatchObject({
      tag: "app:3", gitUrl: "https://github.com/acme/app.git", gitRef: "v2", contextSubdir: "api",
      buildArgs: { VERSION: "2.0" }, noCache: true, push: true, registryId: "r-priv",
    });
  });

  it("reports build, pull, remove and prune failures", async () => {
    const { user } = renderApp("/images", {
      api: {
        "POST /hosts/:h/build": reply(500, { error: "builder crashed" }),
        "POST /hosts/:h/images/pull": reply(404, { error: "manifest unknown" }),
        "DELETE /hosts/:h/images/*": reply(409, { error: "image is being used" }),
        "POST /hosts/:h/prune/:kind": reply(500, { error: "prune locked" }),
      },
    });
    await ready();
    await press(user, /pull image/i, pageRoot());
    let d = await dialog(/pull image/i);
    await press(user, /^pull$/i, d);
    expect(await within(d).findByText(/image reference is required/i)).toBeInTheDocument();
    await user.type(within(d).getByLabelText(/image reference/i), "nope:0");
    await choose(user, /^registry/i, /ghcr/i, d);
    await press(user, /^pull$/i, d);
    await toast(/manifest unknown/i);
    expect(requests("POST", "/hosts/local/images/pull")[0].body).toMatchObject({ ref: "nope:0", registryId: "r-ghcr" });
    await press(user, /^cancel$/i, d);
    await press(user, /build image/i, pageRoot());
    d = await dialog(/build image/i);
    await user.type(within(d).getByLabelText(/^tag/i), "x:1");
    await press(user, /^build$/i, d);
    await toast(/builder crashed/i);
    await press(user, /^cancel$/i, d);
    await user.click(within(pageRoot()).getAllByRole("button", { name: /remove image/i })[0]);
    await press(user, /^remove$/i, await dialog(/remove image/i));
    await toast(/image is being used/i);
    await press(user, /^cancel$/i, await dialog(/remove image/i));
    await press(user, /prune unused/i, pageRoot());
    await press(user, /^prune$/i, await dialog(/prune images/i));
    await toast(/prune locked/i);
  });
});
