// Test environment for jsdom: DOM matchers plus the browser APIs jsdom lacks
// and the UI kit (Radix, sonner, cmdk, CodeMirror, xterm) expects.
import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { calls, resetApi } from "./api";
import { settleClients } from "./clients";

// Failing queries report the message only (not a full DOM dump of the shell).
configure({
  getElementError: (message) => {
    const err = new Error(message ?? "element not found");
    err.name = "TestingLibraryElementError";
    return err;
  },
});

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
class IntersectionObserverStub {
  root = null;
  rootMargin = "";
  thresholds = [];
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

Object.assign(globalThis, {
  ResizeObserver: globalThis.ResizeObserver ?? ResizeObserverStub,
  IntersectionObserver: globalThis.IntersectionObserver ?? IntersectionObserverStub,
});

if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

// Radix uses pointer capture and scrollIntoView; jsdom implements neither.
const proto = Element.prototype as unknown as Record<string, unknown>;
proto.hasPointerCapture ??= () => false;
proto.setPointerCapture ??= () => {};
proto.releasePointerCapture ??= () => {};
proto.scrollIntoView ??= () => {};
HTMLElement.prototype.scrollTo ??= () => {};
window.scrollTo = () => {};

// CodeMirror measures text ranges.
document.createRange = () => {
  const range = new Range();
  range.getBoundingClientRect = () => new DOMRect();
  range.getClientRects = () => ({ item: () => null, length: 0, [Symbol.iterator]: [][Symbol.iterator] }) as unknown as DOMRectList;
  return range;
};

URL.createObjectURL ??= () => "blob:test";
URL.revokeObjectURL ??= () => {};

Object.defineProperty(navigator, "clipboard", {
  value: { writeText: vi.fn(async () => {}), readText: vi.fn(async () => "") },
  configurable: true,
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  resetApi();
});

afterEach(async () => {
  vi.useRealTimers();
  await settleClients();
  cleanup();
  // A refetch loop (a query flipping between loading and error) shows up as
  // hundreds of identical requests within one test: fail loudly.
  const counts = new Map<string, number>();
  for (const c of calls) counts.set(`${c.method} ${c.path}`, (counts.get(`${c.method} ${c.path}`) ?? 0) + 1);
  const storm = [...counts.entries()].find(([, n]) => n > 150);
  if (storm) throw new Error(`request storm: ${storm[0]} called ${storm[1]} times`);
});
