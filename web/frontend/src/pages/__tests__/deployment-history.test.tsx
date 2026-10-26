import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reply, requests } from "../../test/api";
import { deployments } from "../../test/fixtures";
import { pageRoot, renderApp } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

const NOW = "2026-10-26T10:00:00Z";
const revisions = [
  { number: 3, createdAt: NOW, author: "", source: "git", note: "commit c0ffee", deployedAt: NOW, live: true, changes: ["commit"] },
  { number: 2, createdAt: NOW, author: "ops", source: "edit", note: "", deployedAt: "", live: false, changes: ["compose", "targets"] },
  { number: 1, createdAt: NOW, author: "root", source: "create", note: "", deployedAt: NOW, live: false, changes: [] },
];
const diff = (to: number, from: number) => ({
  from, to, changes: ["compose"],
  lines: [{ op: " ", text: "services:" }, { op: "-", text: "  image: nginx:1.26" }, { op: "+", text: "  image: nginx:1.27" }],
});
const api = {
  "GET /deployments/:id/revisions": revisions,
  "GET /deployments/:id/revisions/:n/diff": (r: { params: Record<string, string>; query: Record<string, string> }) =>
    Number(r.params.n) === 1 && !r.query.against ? { from: 0, to: 1, changes: [], lines: [{ op: "+", text: "name: shop" }] } : diff(Number(r.params.n), Number(r.query.against ?? Number(r.params.n) - 1)),
  "POST /deployments/:id/revisions/:n/rollback": deployments[0],
};

async function history(extra: Record<string, unknown> = {}, role?: "viewer") {
  const r = renderApp("/workloads/dep~d-shop?tab=history", { api: { ...api, ...extra } as never, role });
  await within(pageRoot()).findByRole("list", { name: "Revisions" }, { timeout: 10_000 });
  return r;
}

describe("deployment history", () => {
  it("lists revisions with their source, author and what changed", async () => {
    await history();
    const list = screen.getByRole("list", { name: "Revisions" });
    expect(within(list).getByText("#3")).toBeInTheDocument();
    expect(within(list).getByText("New commit")).toBeInTheDocument();
    expect(within(list).getByText("Live")).toBeInTheDocument();
    expect(within(list).getByText(/ops/)).toBeInTheDocument();
    expect(within(list).getByText("host")).toBeInTheDocument();
    expect(within(list).getByText(/commit c0ffee/)).toBeInTheDocument();
    // The newest is selected and the live one cannot be rolled back to.
    expect(await screen.findByText("Revision #3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /roll back to #3/i })).toBeNull();
  });

  it("diffs a revision against the previous one or any other", async () => {
    const { user } = await history();
    await user.click(screen.getByText("#2").closest("button")!);
    expect(await screen.findByText("Changes from #1")).toBeInTheDocument();
    expect(await screen.findByText("image: nginx:1.27")).toBeInTheDocument();
    await choose(user, "Compare with", /#3/);
    await waitFor(() => expect(requests("GET", "/deployments/d-shop/revisions/2/diff").at(-1)!.query).toEqual({ against: "3" }));
    await user.click(screen.getByText("#1").closest("button")!);
    expect(await screen.findByText(/the first revision/i)).toBeInTheDocument();
  });

  it("rolls back with or without deploying", async () => {
    const { user } = await history();
    await user.click(screen.getByText("#2").closest("button")!);
    await press(user, /roll back to #2/i);
    let d = await dialog(/roll back to #2/i);
    await user.click(within(d).getByRole("checkbox", { name: /deploy it now/i }));
    await press(user, /^roll back$/i, d);
    await toast(/rolled back to #2/i);
    expect(requests("POST", "/deployments/d-shop/revisions/2/rollback")[0].body).toEqual({ deploy: false });
    await user.click(screen.getByText("#1").closest("button")!);
    await press(user, /roll back to #1/i);
    d = await dialog(/roll back to #1/i);
    await press(user, /^roll back$/i, d);
    await waitFor(() => expect(requests("POST", "/deployments/d-shop/revisions/1/rollback")[0]?.body).toEqual({ deploy: true }));
  });

  it("warns that a git revision gets pinned and reports failures", async () => {
    const gitRevs = [{ ...revisions[1], number: 5, live: true }, { ...revisions[0], number: 4, live: false }];
    const { user } = await history({
      "GET /deployments/:id/revisions": gitRevs,
      "POST /deployments/:id/revisions/:n/rollback": reply(409, { error: "a deploy is in progress" }),
    });
    await user.click(screen.getByText("#4").closest("button")!);
    await press(user, /roll back to #4/i);
    const d = await dialog(/roll back to #4/i);
    expect(within(d).getByText(/pinned to the commit it ran/i)).toBeInTheDocument();
    await press(user, /^roll back$/i, d);
    await toast(/a deploy is in progress/i);
  });

  it("is read-only for viewers and handles empty, error and no-diff states", async () => {
    await history({}, "viewer");
    await screen.findByText("Revision #3");
    expect(screen.queryByRole("button", { name: /roll back/i })).toBeNull();
  });

  it("shows an empty history", async () => {
    renderApp("/workloads/dep~d-shop?tab=history", { api: { "GET /deployments/:id/revisions": [] } });
    expect(await screen.findByText(/no history yet/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("shows a load error and an identical diff", async () => {
    renderApp("/workloads/dep~d-shop?tab=history", { api: { "GET /deployments/:id/revisions": reply(500, { error: "db locked" }) } });
    expect(await screen.findByText(/db locked/i, {}, { timeout: 10_000 })).toBeInTheDocument();
  });

  it("says when two revisions are the same", async () => {
    await history({ "GET /deployments/:id/revisions/:n/diff": { from: 2, to: 3, changes: [], lines: [{ op: " ", text: "a" }] } });
    expect(await screen.findByText(/no differences/i)).toBeInTheDocument();
  });

  it("shows a diff error", async () => {
    await history({ "GET /deployments/:id/revisions/:n/diff": reply(404, { error: "revision 3 not found" }) });
    expect(await screen.findByText(/revision 3 not found/i)).toBeInTheDocument();
  });
});
