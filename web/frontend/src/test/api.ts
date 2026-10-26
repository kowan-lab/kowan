// A fake Kowan backend for component tests: `fetch` and `WebSocket` are
// replaced by in-memory versions. Routes are keyed "METHOD /path" relative to
// /api/v1 (":name" matches one segment, "*" the rest); a handler is either the
// JSON body to return or a function of the request. Unmatched GETs answer an
// empty list and unmatched writes an empty object, so a page renders even when
// a test only cares about one of its calls; every request is recorded in
// `calls` for assertions.
import { vi } from "vitest";

export interface MockRequest {
  method: string;
  path: string;
  query: Record<string, string>;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- each test knows the shape it posts
  body: any;
  headers: Record<string, string>;
  params: Record<string, string>;
}

export interface MockResponse {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Return the raw text (or Blob) instead of JSON. */
  raw?: string | Blob;
}

// A function handler gets the request (typed by context); anything else is the
// response body as-is.
export type Handler =
  | ((req: MockRequest) => unknown)
  | object
  | Blob
  | string
  | number
  | boolean
  | null;

const routes = new Map<string, Handler>();
export const calls: MockRequest[] = [];

/** A response the fake backend should send as-is (status, headers, body). */
export function reply(status: number, body?: unknown, headers?: Record<string, string>): MockResponse & { __reply: true } {
  return { __reply: true, status, body, headers };
}

function isReply(v: unknown): v is MockResponse & { __reply: true } {
  return typeof v === "object" && v !== null && (v as { __reply?: boolean }).__reply === true;
}

export function mockApi(map: Record<string, Handler>): void {
  for (const [k, v] of Object.entries(map)) routes.set(k, v);
}

export function resetApi(): void {
  routes.clear();
  calls.length = 0;
  sockets.length = 0;
  vi.stubGlobal("fetch", fakeFetch);
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("EventSource", FakeEventSource);
}

function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split("/").filter(Boolean);
  const s = path.split("/").filter(Boolean);
  const params: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    if (p[i] === "*") {
      params["*"] = s.slice(i).map(decodeURIComponent).join("/");
      return params;
    }
    if (i >= s.length) return null;
    if (p[i].startsWith(":")) params[p[i].slice(1)] = decodeURIComponent(s[i]);
    else if (p[i] !== decodeURIComponent(s[i])) return null;
  }
  return p.length === s.length ? params : null;
}

function find(method: string, path: string): { handler: Handler; params: Record<string, string> } | null {
  // Exact patterns win over parametrised ones; among equals the most recently
  // registered wins, so a test's routes override the defaults.
  const candidates = [...routes.entries()].reverse().filter(([k]) => k.startsWith(method + " "));
  candidates.sort(([a], [b]) => (a.includes(":") || a.includes("*") ? 1 : 0) - (b.includes(":") || b.includes("*") ? 1 : 0));
  for (const [key, handler] of candidates) {
    const params = match(key.slice(method.length + 1), path);
    if (params) return { handler, params };
  }
  return null;
}

async function readRequestBody(init?: RequestInit): Promise<unknown> {
  const b = init?.body;
  if (b == null) return undefined;
  if (typeof b === "string") {
    try {
      return JSON.parse(b);
    } catch {
      return b;
    }
  }
  return b; // FormData / Blob: handed to the handler untouched
}

async function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, "http://kowan.test");
  const method = (init?.method ?? "GET").toUpperCase();
  const path = url.pathname.replace(/^\/api\/v1/, "") || "/";
  const query: Record<string, string> = {};
  url.searchParams.forEach((v, k) => (query[k] = v));
  const headers: Record<string, string> = {};
  new Headers(init?.headers).forEach((v, k) => (headers[k] = v));
  const req: MockRequest = { method, path, query, body: await readRequestBody(init), headers, params: {} };
  calls.push(req);

  const hit = find(method, path);
  let out: unknown = hit ? hit.handler : method === "GET" ? [] : {};
  if (hit) req.params = hit.params;
  if (typeof out === "function") out = await (out as (r: MockRequest) => unknown)(req);

  let status = 200;
  let respHeaders: Record<string, string> = { "content-type": "application/json" };
  let body: BodyInit | null;
  if (isReply(out)) {
    status = out.status ?? 200;
    respHeaders = { ...respHeaders, ...(out.headers ?? {}) };
    out = out.body;
  }
  if (out instanceof Blob) {
    // jsdom's Blob is not the one undici's Response accepts: hand over bytes.
    body = typeof out.arrayBuffer === "function" ? await out.arrayBuffer() : await out.text();
    respHeaders["content-type"] = out.type || "application/octet-stream";
  } else if (typeof out === "string" && respHeaders["content-type"] !== "application/json") {
    body = out;
  } else {
    body = out === undefined ? "" : JSON.stringify(out);
  }
  return new Response(status === 204 ? null : body, { status, headers: respHeaders });
}

/** Requests made to ``method path`` (path relative to /api/v1). */
export function requests(method: string, pathPattern: string): MockRequest[] {
  return calls.filter((c) => c.method === method && match(pathPattern, c.path) !== null);
}

// ---------------------------------------------------------------------------
// WebSocket / EventSource
// ---------------------------------------------------------------------------

export const sockets: FakeWebSocket[] = [];

export class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = FakeWebSocket.CONNECTING;
  binaryType = "blob";
  sent: unknown[] = [];
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  private listeners: Record<string, ((e: Event) => void)[]> = {};

  constructor(public url: string) {
    sockets.push(this);
    queueMicrotask(() => this.open());
  }

  addEventListener(type: string, fn: (e: Event) => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  removeEventListener(type: string, fn: (e: Event) => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn);
  }

  private fire(type: string, e: Event) {
    const prop = (this as unknown as Record<string, unknown>)["on" + type];
    if (typeof prop === "function") (prop as (e: Event) => void)(e);
    for (const fn of this.listeners[type] ?? []) fn(e);
  }

  open() {
    if (this.readyState !== FakeWebSocket.CONNECTING) return;
    this.readyState = FakeWebSocket.OPEN;
    this.fire("open", new Event("open"));
  }

  /** Deliver a server message (string or binary). */
  emit(data: string | ArrayBuffer | Blob) {
    this.fire("message", new MessageEvent("message", { data }));
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  close(code = 1000, reason = "") {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.fire("close", new CloseEvent("close", { code, reason }));
  }
}

class FakeEventSource extends EventTarget {
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  readyState = 1;
  constructor(public url: string) {
    super();
  }
  close() {
    this.readyState = 2;
  }
}

/** The most recent socket whose URL contains ``part``. */
export function socket(part: string): FakeWebSocket | undefined {
  return [...sockets].reverse().find((s) => s.url.includes(part));
}
