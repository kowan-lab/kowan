// What's new: the bundled changelog, read into releases and shown newest first
// with the running version marked and the other changes folded.
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RELEASES, inlineParts, parseChangelog } from "../../lib/changelog";
import { pageRoot, renderApp } from "../../test/render";

const TEXT = `# Kowan — Changelog

What changes for you first.

## 0.2.0 — 2026-11-02

- **Faster.** Lists load \`twice\` as fast,
  see [the notes](https://example.com/notes).
- **Bold inside** a sentence stays text.

### Other changes

- **A fix.** Explained.

## 0.1.0 — 2026-10-26

First public release.

**What it contains**
1. Deployments

## 2026-10-25 — Before the first release

Notes.
`;

describe("changelog reader", () => {
  it("splits user changes from other changes, and titles from descriptions", () => {
    const [b, a, notes] = parseChangelog(TEXT);
    expect([b.version, b.date]).toEqual(["0.2.0", "2026-11-02"]);
    expect(b.blocks).toEqual([{ kind: "ul", items: [
      { title: "Faster", text: "Lists load `twice` as fast, see [the notes](https://example.com/notes)." },
      { title: "", text: "**Bold inside** a sentence stays text." },
    ] }]);
    expect(b.other).toEqual([{ kind: "ul", items: [{ title: "A fix", text: "Explained." }] }]);
    expect(a.blocks).toEqual([
      { kind: "p", text: "First public release." },
      { kind: "h", text: "What it contains" },
      { kind: "ol", items: [{ title: "", text: "Deployments" }] },
    ]);
    expect(a.other).toEqual([]);
    expect([notes.version, notes.date, notes.title]).toEqual(["", "2026-10-25", "Before the first release"]);
  });

  it("splits inline code, bold and links", () => {
    expect(inlineParts("a `b` **c** [d](https://e) f")).toEqual([
      { kind: "text", text: "a " }, { kind: "code", text: "b" }, { kind: "text", text: " " },
      { kind: "bold", text: "c" }, { kind: "text", text: " " }, { kind: "link", text: "d", href: "https://e" },
      { kind: "text", text: " f" },
    ]);
  });

  it("bundles the repository's releases, newest first, without the notes before 0.1.0", () => {
    expect(RELEASES[0].version).toMatch(/^\d+\.\d+\.\d+/);
    expect(RELEASES.at(-1)!.version).toBe("0.1.0");
    expect(RELEASES.every((r) => r.version)).toBe(true);
  });
});

describe("what's new page", () => {
  it("shows the releases with the running one marked and other changes folded", async () => {
    const { user } = renderApp("/changelog");
    const list = await within(pageRoot()).findByRole("list", { name: "Releases" }, { timeout: 10_000 });
    const first = within(list).getAllByRole("listitem")[0];
    expect(within(first).getByRole("heading", { level: 2 })).toHaveTextContent(RELEASES[0].version);
    expect(within(first).getByText("Running")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`this server runs ${RELEASES[0].version.replace(/\./g, "\\.")}`, "i"))).toBeInTheDocument();
    const r0 = RELEASES[0];
    const title = r0.blocks.flatMap((b) => (b.kind === "ul" ? b.items : []))[0].title;
    expect(within(first).getByText(title)).toBeInTheDocument();
    if (r0.other.length) {
      const fold = within(first).getByText(/^Other changes · \d+$/);
      expect(fold.closest("details")).not.toHaveAttribute("open");
      await user.click(fold);
      expect(fold.closest("details")).toHaveAttribute("open");
    }
    expect(screen.queryByText(/before 0\.1\.0/i)).toBeNull();
  });
});
