import { describe, expect, it } from "vitest";
import { rank, scoreEntry, type PaletteEntry, type PaletteKind } from "./paletteSearch";

const e = (key: string, kind: PaletteKind, title: string, terms: string[] = [], boost = 0): PaletteEntry => ({ key, kind, title, terms, boost });
const order = (entries: PaletteEntry[], q: string) => rank(entries, q).map((r) => r.entry.key);

describe("palette ranking", () => {
  it("puts the whole name first, then its start, a word start, anywhere", () => {
    const entries = [e("mid", "container", "myshopx"), e("word", "container", "old-shop"), e("start", "container", "shop-web-1"), e("exact", "container", "shop")];
    expect(order(entries, "shop")).toEqual(["exact", "start", "word", "mid"]);
  });

  it("prefers a workload to a page or container that matches as well", () => {
    const entries = [e("page", "page", "Logs"), e("ctr", "container", "logs"), e("wl", "workload", "logs")];
    expect(order(entries, "logs")).toEqual(["wl", "ctr", "page"]);
  });

  it("matches secondary terms below the name", () => {
    const entries = [e("by-image", "workload", "shop", ["postgres:16"]), e("by-name", "workload", "postgres-backup")];
    expect(order(entries, "postgres")).toEqual(["by-name", "by-image"]);
  });

  it("needs every word to match and accepts letters in order", () => {
    expect(scoreEntry(e("a", "workload", "shop", ["compose stack"]), "shop compose")).toBeGreaterThan(0);
    expect(scoreEntry(e("a", "workload", "shop"), "shop missing")).toBe(0);
    expect(scoreEntry(e("a", "container", "nginx"), "ngx")).toBeGreaterThan(0);
  });

  it("lifts what needs attention among equal matches", () => {
    const entries = [e("ok", "workload", "api-1"), e("broken", "workload", "api-2", [], 2)];
    expect(order(entries, "api")).toEqual(["broken", "ok"]);
  });
});
