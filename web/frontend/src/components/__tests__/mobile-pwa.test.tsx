import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import OfflineNotice from "../shell/OfflineNotice";
import { Table } from "@/ui/kit";
import { ThemeProvider, useTheme } from "../../ui/theme";
import { renderApp, pageRoot } from "../../test/render";

describe("offline notice", () => {
  it("shows while the browser is offline", () => {
    render(<OfflineNotice />);
    expect(screen.queryByRole("status")).toBeNull();
    act(() => {
      Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
      window.dispatchEvent(new Event("offline"));
    });
    expect(screen.getByRole("status")).toHaveTextContent(/you are offline/i);
    act(() => {
      Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
      window.dispatchEvent(new Event("online"));
    });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("is part of the app shell", async () => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    try {
      renderApp("/");
      await screen.findByText(/you are offline/i, {}, { timeout: 10_000 });
      expect(pageRoot()).toBeInTheDocument();
    } finally {
      Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    }
  });
});

describe("theme colour", () => {
  it("follows the page ground colour", () => {
    const meta = document.createElement("meta");
    meta.name = "theme-color";
    meta.content = "#000000";
    meta.media = "(prefers-color-scheme: dark)";
    document.head.appendChild(meta);
    document.documentElement.style.setProperty("--background", "#abcdef");
    function Flip() {
      const { toggle } = useTheme();
      return <button onClick={toggle}>flip</button>;
    }
    render(<ThemeProvider><Flip /></ThemeProvider>);
    act(() => screen.getByRole("button", { name: "flip" }).click());
    expect(meta.content).toBe("#abcdef");
    expect(meta.hasAttribute("media")).toBe(false);
    meta.remove();
    document.documentElement.style.removeProperty("--background");
  });
});

describe("tables on narrow screens", () => {
  it("label every cell for the stacked card layout", () => {
    render(
      <Table
        columns={[
          { key: "name", header: "Name" },
          { key: "size", header: "Size" },
          { key: "actions", header: "", cell: () => <button>Remove</button> },
          { key: "x", header: <b>Rich</b> },
        ]}
        data={[{ name: "web", size: "3 MB", x: 1 }]}
      />,
    );
    const cells = screen.getAllByRole("cell");
    expect(cells.map((c) => c.getAttribute("data-label"))).toEqual(["Name", "Size", "", "x"]);
    expect(cells[0].closest(".kit-table")).not.toBeNull();
  });
});
