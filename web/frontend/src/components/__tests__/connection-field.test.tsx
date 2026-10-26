import { screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import ConnectionField from "../ConnectionField";
import { mockApi, reply, requests } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { choose, dialog, press, toast } from "../../test/ui";

let picked = "";
function H(props: { capability: "messageSend" | "gitFetch"; types?: string[]; optional?: boolean; isEdit?: boolean }) {
  const [v, setV] = useState("");
  picked = v;
  return (
    <ConnectionField
      {...props}
      value={v}
      onChange={(x) => {
        picked = x;
        setV(x);
      }}
    />
  );
}

describe("connection field", () => {
  it("creates the first credential in place and selects it", async () => {
    const { user } = renderWithProviders(<H capability="messageSend" />);
    mockApi({
      "GET /credentials": [],
      "POST /credentials": (r) => ({ id: "c-new", kind: "connection", type: r.body.type, name: r.body.name, meta: {}, configKeys: [], createdAt: "", updatedAt: "" }),
    });
    expect(await screen.findByText(/no .* credentials yet/i, {}, { timeout: 5000 })).toBeInTheDocument();
    await press(user, /new credential/i);
    const d = await dialog(/new credential/i);
    await user.type(within(d).getByLabelText(/^name/i), "team-slack");
    await choose(user, /^type/i, /^slack$/i, d);
    await press(user, /^create$/i, d);
    expect(requests("POST", "/credentials")).toHaveLength(0); // the URL is required
    await user.type(within(d).getByLabelText(/webhook url/i), "https://hooks.slack.com/services/x");
    await press(user, /^create$/i, d);
    await toast(/credential created/i);
    expect(requests("POST", "/credentials")[0].body).toMatchObject({ kind: "connection", type: "slack", name: "team-slack", config: { url: "https://hooks.slack.com/services/x" } });
    await waitFor(() => expect(picked).toBe("c-new"));
  });

  it("restricts the type to the one a consumer needs", async () => {
    const { user } = renderWithProviders(<H capability="messageSend" types={["basic-auth"]} />);
    mockApi({ "GET /credentials": [], "POST /credentials": reply(400, { error: "username taken" }) });
    await screen.findByText(/no .* credentials yet/i, {}, { timeout: 5000 });
    await press(user, /new credential/i);
    const d = await dialog(/new credential/i);
    expect(within(d).getByRole("combobox", { name: /^type/i })).toBeDisabled();
    await user.type(within(d).getByLabelText(/^name/i), "smb");
    await user.type(within(d).getByLabelText(/^username/i), "bob");
    await user.type(within(d).getByLabelText(/^password/i), "pw");
    await press(user, /^create$/i, d);
    await toast(/username taken/i);
  });

  it("an optional picker offers None and does not preselect", async () => {
    const { user } = renderWithProviders(<H capability="gitFetch" optional />);
    const select = await screen.findByRole("combobox", {}, { timeout: 5000 });
    expect(picked).toBe("");
    await user.click(select);
    expect(await screen.findByRole("option", { name: /none \(no credential\)/i })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "github" }));
    expect(picked).toBe("c-gh");
  });

  it("a required picker preselects the only eligible credential", async () => {
    renderWithProviders(<H capability="gitFetch" />);
    await waitFor(() => expect(picked).toBe("c-gh"), { timeout: 5000 });
  });
});
