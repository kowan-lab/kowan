// Environment rows: a saved secret's reference stays out of the input and goes
// back untouched; new secrets need a value; names checked for compose.
import { describe, expect, it } from "vitest";
import { envRowsFromApi, envRowsProblem, envRowsToApi } from "../EnvEditor";

describe("environment rows", () => {
  it("keep a saved secret's reference out of the form and send it back", () => {
    const rows = envRowsFromApi([{ key: "TAG", value: "1" }, { key: "PW", value: "[[shop-env.PW]]", secret: true }]);
    expect(rows).toEqual([{ key: "TAG", value: "1" }, { key: "PW", value: "", secret: true, stored: "[[shop-env.PW]]" }]);
    expect(envRowsToApi(rows)).toEqual([{ key: "TAG", value: "1" }, { key: "PW", value: "[[shop-env.PW]]", secret: true }]);
    rows[1].value = "new";
    rows.push({ key: "  ", value: "dropped" }, { key: " K ", value: "v" });
    expect(envRowsToApi(rows)).toEqual([{ key: "TAG", value: "1" }, { key: "PW", value: "new", secret: true }, { key: "K", value: "v" }]);
    expect(envRowsFromApi(undefined)).toEqual([]);
  });

  it("report the first problem", () => {
    expect(envRowsProblem([{ key: "A", value: "" }, { key: "", value: "x" }])).toBeNull();
    expect(envRowsProblem([{ key: "A", value: "1" }, { key: "A", value: "2" }])).toBe("A is set twice");
    expect(envRowsProblem([{ key: "S", value: "", secret: true }])).toBe("Enter a value for the secret S");
    expect(envRowsProblem([{ key: "S", value: "", secret: true, stored: "[[x.S]]" }])).toBeNull();
    expect(envRowsProblem([{ key: "1A", value: "x" }])).toBeNull();
    expect(envRowsProblem([{ key: "1A", value: "x" }], { strictNames: true })).toMatch(/^1A: use letters/);
  });
});
