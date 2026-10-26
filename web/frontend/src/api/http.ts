/**
 * Minimal fetch-based HTTP client with the slice of the axios surface this app
 * used: api.get/post/put/patch/delete returning { data, status, headers },
 * `params`, `headers`, `responseType` ("json" | "text" | "blob"), and errors
 * shaped like axios (`err.response.status`, `err.response.data.error`) so every
 * existing error handler keeps working. Replaces axios (~42 KB).
 */

export interface RequestConfig {
  params?: Record<string, string | number | boolean | null | undefined>;
  headers?: Record<string, string>;
  responseType?: "json" | "text" | "blob";
  signal?: AbortSignal;
  /** Accepted for call-site compatibility; responses are not transformed. */
  transformResponse?: unknown;
}

export interface HttpResponse<T> {
  data: T;
  status: number;
  headers: Record<string, string>;
}

export class HttpError<T = unknown> extends Error {
  response?: HttpResponse<T>;
  constructor(message: string, response?: HttpResponse<T>) {
    super(message);
    this.name = "HttpError";
    this.response = response;
  }
}

interface ClientOptions {
  baseURL: string;
  /** Extra headers per request, e.g. the bearer token. */
  authHeader?: () => string | null;
  /** Called on 401 before the error is thrown. */
  onUnauthorized?: () => void;
}

function buildURL(base: string, url: string, params?: RequestConfig["params"]): string {
  const full = /^https?:\/\//.test(url) ? url : `${base}${url}`;
  if (!params) return full;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    qs.append(k, String(v));
  }
  const s = qs.toString();
  return s ? `${full}${full.includes("?") ? "&" : "?"}${s}` : full;
}

async function readBody(res: Response, responseType: RequestConfig["responseType"]): Promise<unknown> {
  if (responseType === "blob") return res.blob();
  const text = await res.text();
  if (responseType === "text") return text;
  if (!text) return "";
  const type = res.headers.get("content-type") ?? "";
  if (type.includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

export function createHttp({ baseURL, authHeader, onUnauthorized }: ClientOptions) {
  async function request<T>(method: string, url: string, body?: unknown, config: RequestConfig = {}): Promise<HttpResponse<T>> {
    const headers: Record<string, string> = { Accept: "application/json", ...config.headers };
    const auth = authHeader?.();
    if (auth) headers.Authorization = auth;

    let payload: BodyInit | undefined;
    if (body instanceof FormData || body instanceof Blob || body instanceof URLSearchParams) {
      payload = body;
      // Let the browser set the multipart boundary.
      for (const k of Object.keys(headers)) if (k.toLowerCase() === "content-type") delete headers[k];
    } else if (body !== undefined) {
      payload = JSON.stringify(body);
      headers["Content-Type"] ??= "application/json";
    }

    let res: Response;
    try {
      res = await fetch(buildURL(baseURL, url, config.params), { method, headers, body: payload, signal: config.signal, credentials: "same-origin" });
    } catch (err) {
      if ((err as Error)?.name === "AbortError") throw err;
      throw new HttpError("Network Error");
    }

    const resHeaders: Record<string, string> = {};
    res.headers.forEach((v, k) => (resHeaders[k] = v));
    const ok = res.status >= 200 && res.status < 300;
    const data = (await readBody(res, ok ? config.responseType : "json")) as T;
    const response: HttpResponse<T> = { data, status: res.status, headers: resHeaders };

    if (!ok) {
      if (res.status === 401) onUnauthorized?.();
      throw new HttpError(`Request failed with status code ${res.status}`, response);
    }
    return response;
  }

  return {
    get: <T = unknown>(url: string, config?: RequestConfig) => request<T>("GET", url, undefined, config),
    delete: <T = unknown>(url: string, config?: RequestConfig) => request<T>("DELETE", url, undefined, config),
    post: <T = unknown>(url: string, body?: unknown, config?: RequestConfig) => request<T>("POST", url, body, config),
    put: <T = unknown>(url: string, body?: unknown, config?: RequestConfig) => request<T>("PUT", url, body, config),
    patch: <T = unknown>(url: string, body?: unknown, config?: RequestConfig) => request<T>("PATCH", url, body, config),
  };
}
