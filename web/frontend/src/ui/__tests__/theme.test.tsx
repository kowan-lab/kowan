// Light, dark and auto: auto follows the OS and switches when the OS does.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemePanel } from "../../components/shell/ThemePicker";
import { ThemeProvider } from "../theme";

function fakeSystem(dark: boolean) {
  const listeners = new Set<() => void>();
  const mq = {
    get matches() {
      return dark;
    },
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  };
  vi.spyOn(window, "matchMedia").mockImplementation(() => mq as unknown as MediaQueryList);
  return {
    set(next: boolean) {
      dark = next;
      act(() => listeners.forEach((fn) => fn()));
    },
  };
}

const shown = () => document.documentElement.getAttribute("data-theme");
const radio = (name: string) => screen.getByRole("radio", { name });

afterEach(() => vi.restoreAllMocks());

describe("theme mode", () => {
  it("starts on auto and follows the system", () => {
    const system = fakeSystem(true);
    render(<ThemeProvider><ThemePanel /></ThemeProvider>);
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    expect(shown()).toBe("dark");
    system.set(false);
    expect(shown()).toBe("light");
    expect(localStorage.getItem("kowan-theme")).toBe("auto");
  });

  it("a fixed mode ignores the system until auto is chosen again", async () => {
    const system = fakeSystem(false);
    const user = userEvent.setup();
    render(<ThemeProvider><ThemePanel /></ThemeProvider>);
    await user.click(radio("Dark"));
    expect(shown()).toBe("dark");
    expect(localStorage.getItem("kowan-theme")).toBe("dark");
    system.set(false);
    expect(shown()).toBe("dark");
    await user.click(radio("Auto"));
    expect(shown()).toBe("light");
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
  });

  it("keeps a saved choice", () => {
    fakeSystem(true);
    localStorage.setItem("kowan-theme", "light");
    render(<ThemeProvider><ThemePanel /></ThemeProvider>);
    expect(radio("Light")).toHaveAttribute("aria-checked", "true");
    expect(shown()).toBe("light");
  });
});
