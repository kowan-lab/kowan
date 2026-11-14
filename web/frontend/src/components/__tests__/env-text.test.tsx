import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { TooltipProvider } from "@/ui/kit";
import EnvEditor, { composeVariables, envRowsFromText, envRowsToText, SECRET_KEPT, type EnvRow } from "../EnvEditor";

describe(".env text", () => {
  const rows: EnvRow[] = [
    { key: "MODE", value: "prod" },
    { key: "DB_PASSWORD", value: "", secret: true, stored: "[[shop-env.DB_PASSWORD]]" },
    { key: "GREETING", value: "hello world" },
  ];

  it("round-trips, keeping saved secrets and kinds", () => {
    const text = envRowsToText(rows);
    expect(text).toBe(`MODE=prod\nDB_PASSWORD=${SECRET_KEPT}\nGREETING="hello world"`);
    expect(envRowsFromText(text, rows)).toEqual(rows);
  });

  it("adds, changes and removes from pasted text", () => {
    const out = envRowsFromText("# from .env\nexport MODE='dev'\n\nAPI_TOKEN=abc\nPORT=8080\nDB_PASSWORD=new-one", rows);
    expect(out).toEqual([
      { key: "MODE", value: "dev" },
      { key: "API_TOKEN", value: "abc", secret: true },
      { key: "PORT", value: "8080" },
      { key: "DB_PASSWORD", value: "new-one", secret: true },
    ]);
    expect(envRowsFromText("API_TOKEN=abc", [], false)).toEqual([{ key: "API_TOKEN", value: "abc" }]);
  });

  it("finds the variables a compose file uses", () => {
    const compose = "image: app:${TAG}\nenv: $HOST ${PORT:-80} ${NAME-x} ${REQ:?set it} $$NOT ${TAG}";
    expect(composeVariables(compose)).toEqual([
      { name: "TAG", hasDefault: false }, { name: "HOST", hasDefault: false }, { name: "PORT", hasDefault: true },
      { name: "NAME", hasDefault: true }, { name: "REQ", hasDefault: false },
    ]);
  });

  it("edits every row at once from the editor", async () => {
    const user = userEvent.setup();
    let last: EnvRow[] = [];
    function Host() {
      const [v, setV] = useState<EnvRow[]>([{ key: "A", value: "1" }]);
      last = v;
      return <EnvEditor value={v} onChange={setV} />;
    }
    render(<TooltipProvider><Host /></TooltipProvider>);
    await user.click(screen.getByRole("button", { name: /edit as \.env/i }));
    const d = await screen.findByRole("dialog");
    const area = within(d).getByLabelText(/one name=value per line/i);
    await user.clear(area);
    await user.type(area, "A=2{Enter}B=3");
    await user.click(within(d).getByRole("button", { name: /^apply$/i }));
    expect(last).toEqual([{ key: "A", value: "2" }, { key: "B", value: "3" }]);
  });
});
