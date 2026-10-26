import { act, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ContainerConsole from "../ContainerConsole";
import { socket } from "../../test/api";
import { containers } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";

// xterm needs a real layout engine; a small fake records what the console
// writes and lets the test type into it.
const term = vi.hoisted(() => ({
  written: [] as (string | Uint8Array)[],
  onData: null as ((d: string) => void) | null,
  focused: 0,
  disposed: 0,
}));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    _core = { _renderService: { dimensions: {} } };
    loadAddon() {}
    open() {}
    focus() { term.focused += 1; }
    write(d: string | Uint8Array) { term.written.push(d); }
    writeln(d: string) { term.written.push(d); }
    onData(fn: (d: string) => void) { term.onData = fn; return { dispose() {} }; }
    dispose() { term.disposed += 1; }
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit = vi.fn(); } }));

beforeEach(() => {
  term.written.length = 0;
  term.onData = null;
  term.focused = 0;
  term.disposed = 0;
});

describe("container console", () => {
  it("wires the exec socket to the terminal both ways", async () => {
    const { unmount } = renderWithProviders(<ContainerConsole hostId="local" container={containers[0]} active running />);
    await act(async () => { await Promise.resolve(); });
    const ws = socket("/exec")!;
    expect(ws.url).toMatch(/\/hosts\/local\/containers\/c-web0+\/exec\?/);
    expect(term.focused).toBe(1);
    act(() => {
      ws.emit("$ ");
      // An ArrayBuffer from the page's realm (what a binary frame delivers).
      ws.emit(new window.ArrayBuffer(3));
    });
    expect(term.written[0]).toBe("$ ");
    expect(term.written[1]).toBeInstanceOf(Uint8Array);
    act(() => term.onData!("ls\r"));
    expect(new TextDecoder().decode(ws.sent[0] as Uint8Array)).toBe("ls\r");
    act(() => window.dispatchEvent(new Event("resize")));
    act(() => ws.onerror?.(new Event("error")));
    expect(term.written.at(-1)).toMatch(/connection error/);
    act(() => ws.close());
    expect(term.written.at(-1)).toMatch(/session closed/);
    unmount();
    expect(term.disposed).toBe(1);
  });

  it("closes an open socket on unmount without a closed notice", async () => {
    const { unmount } = renderWithProviders(<ContainerConsole hostId="local" container={containers[0]} active={false} running />);
    await act(async () => { await Promise.resolve(); });
    const ws = socket("/exec")!;
    unmount();
    expect(ws.readyState).toBe(3);
    expect(term.written.some((w) => typeof w === "string" && /session closed/.test(w))).toBe(false);
  });

  it("asks to start a stopped container", () => {
    renderWithProviders(<ContainerConsole hostId="local" container={containers[2]} active running={false} />);
    expect(screen.getByText(/start it to open an interactive console/i)).toBeInTheDocument();
    expect(socket("/exec")).toBeUndefined();
  });
});
