// copyText: the Clipboard API when the page has it, else the textarea fallback
// (http://<address> is not a secure context), else a toast.
import { afterEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { copyText } from "../clipboard";

const realClipboard = navigator.clipboard;
const setClipboard = (value: unknown) => Object.defineProperty(navigator, "clipboard", { value, configurable: true });

afterEach(() => {
  setClipboard(realClipboard);
  vi.restoreAllMocks();
});

describe("copyText", () => {
  it("uses the Clipboard API when there is one", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard({ writeText });
    expect(await copyText("docker run x")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("docker run x");
  });

  it("falls back to a selected textarea inside the open dialog", async () => {
    setClipboard(undefined);
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const button = document.createElement("button");
    dialog.append(button);
    document.body.append(dialog);
    button.focus();
    let copied = "";
    let parent: Element | null = null;
    document.execCommand = vi.fn(() => {
      const area = document.activeElement as HTMLTextAreaElement;
      copied = area.value.slice(area.selectionStart, area.selectionEnd);
      parent = area.parentElement;
      return true;
    });
    expect(await copyText("docker run y")).toBe(true);
    expect(copied).toBe("docker run y");
    expect(parent).toBe(dialog);
    expect(dialog.querySelector("textarea")).toBeNull();
    expect(document.activeElement).toBe(button);
    dialog.remove();
  });

  it("falls back when the Clipboard API refuses, and says when nothing works", async () => {
    setClipboard({ writeText: vi.fn(async () => { throw new Error("denied"); }) });
    document.execCommand = vi.fn(() => true);
    expect(await copyText("a")).toBe(true);
    const error = vi.spyOn(toast, "error");
    setClipboard(undefined);
    document.execCommand = vi.fn(() => false);
    expect(await copyText("b")).toBe(false);
    document.execCommand = vi.fn(() => { throw new Error("unsupported"); });
    expect(await copyText("c")).toBe(false);
    expect(error).toHaveBeenCalledTimes(2);
    expect(error.mock.calls[0][0]).toBe("Could not copy");
  });
});
