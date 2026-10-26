import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, reply, requests } from "../../test/api";
import { users } from "../../test/fixtures";
import { currentPath, heading, renderApp } from "../../test/render";

describe("sign in", () => {
  it("signs in and lands on the dashboard", async () => {
    const { user } = renderApp("/login", { role: null });
    mockApi({ "POST /auth/login": { token: "tok", user: users.admin } });
    await user.type(await screen.findByLabelText(/username/i), "root");
    await user.type(screen.getByLabelText(/password/i), "supersecret");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(currentPath()).toBe("/"));
    expect(requests("POST", "/auth/login")[0].body).toEqual({ username: "root", password: "supersecret" });
  });

  it("shows the server's error on bad credentials", async () => {
    const { user } = renderApp("/login", { role: null });
    mockApi({ "POST /auth/login": reply(401, { error: "invalid credentials" }) });
    await user.type(await screen.findByLabelText(/username/i), "root");
    await user.type(screen.getByLabelText(/password/i), "wrong");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/invalid/i)).toBeInTheDocument();
  });

  it("redirects to /login when signed out", async () => {
    renderApp("/volumes", { role: null });
    await waitFor(() => expect(currentPath()).toBe("/login"));
  });

  it("renders the dashboard for a signed-in admin", async () => {
    renderApp("/");
    expect(await heading("local")).toBeInTheDocument();
  });

  it("ends the session when the server rejects the token", async () => {
    renderApp("/volumes", { api: { "GET /me": reply(401, { error: "token expired" }) } });
    await waitFor(() => expect(currentPath()).toBe("/login"));
    expect(localStorage.getItem("kowan_token")).toBeNull();
    expect(localStorage.getItem("kowan_user")).toBeNull();
  });

  it("keeps the session with the last identity when the server cannot be reached", async () => {
    localStorage.setItem("kowan_user", JSON.stringify({ id: "u-admin", username: "root", role: "admin" }));
    renderApp("/volumes", { api: { "GET /me": reply(502, { error: "bad gateway" }) } });
    expect(await heading(/volumes/i)).toBeInTheDocument();
    expect(currentPath()).toBe("/volumes");
    expect(localStorage.getItem("kowan_token")).toBe("test-token");
  });

  it("remembers who signed in", async () => {
    const { user } = renderApp("/login", { role: null });
    mockApi({ "POST /auth/login": { token: "tok", user: users.operator } });
    await user.type(await screen.findByLabelText(/username/i), "ops");
    await user.type(screen.getByLabelText(/password/i), "pw");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(JSON.parse(localStorage.getItem("kowan_user")!)).toEqual({ id: "u-op", username: "ops", role: "operator" }));
  });
});
