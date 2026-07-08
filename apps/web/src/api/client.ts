/** Typed HTTP client — injects JWT + X-Workspace-Id on every request.
 *  All module endpoints (endpoints.ts) go through `http`. */
import { useAuthStore } from "@/stores/auth";

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api/v1";

export class ApiError extends Error {
  constructor(
    public status: number,
    public body: unknown,
    message?: string,
  ) {
    super(message ?? `API ${status}`);
    this.name = "ApiError";
  }
}

export type Query = Record<string, string | number | boolean | undefined | null>;

function buildQuery(query?: Query): string {
  if (!query) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

interface RequestOptions {
  body?: unknown;
  query?: Query;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

/** Single-flight refresh — concurrent 401s share one POST /auth/refresh.
 *  Resolves true when the store now holds a fresh token pair. */
let refreshInFlight: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  const { refreshToken } = useAuthStore.getState();
  if (!refreshToken) return false;
  refreshInFlight ??= (async () => {
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return false;
      const out = (await res.json()) as { access_token?: string; refresh_token?: string };
      if (!out.access_token || !out.refresh_token) return false;
      useAuthStore.getState().setTokens(out.access_token, out.refresh_token);
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function http<T>(
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const doFetch = () => {
    const { token, workspaceId } = useAuthStore.getState();
    const headers: Record<string, string> = { ...opts.headers };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    if (workspaceId) headers["X-Workspace-Id"] = workspaceId;

    let body: BodyInit | undefined;
    if (opts.body instanceof FormData) {
      body = opts.body;
    } else if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    return fetch(`${API_BASE}${path}${buildQuery(opts.query)}`, {
      method,
      headers,
      body,
      signal: opts.signal,
    });
  };

  let res = await doFetch();

  // Expired access token → refresh once (never for /auth/* itself) and retry.
  if (res.status === 401 && useAuthStore.getState().token && !path.startsWith("/auth/")) {
    if (await tryRefresh()) res = await doFetch();
  }

  if (res.status === 401 && useAuthStore.getState().token) {
    useAuthStore.getState().logout();
    if (!location.pathname.startsWith("/login")) {
      location.assign("/login");
    }
    throw new ApiError(401, null, "unauthorized");
  }

  if (!res.ok) {
    let errBody: unknown = null;
    try {
      errBody = await res.json();
    } catch {
      /* non-json error body */
    }
    const detail =
      errBody && typeof errBody === "object" && "detail" in errBody
        ? String((errBody as { detail: unknown }).detail)
        : undefined;
    throw new ApiError(res.status, errBody, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** WebSocket URL for the realtime gateway. The gateway serves /ws/agent at
 * the ROOT (a separate service in prod, reverse-proxied by nginx; a vite
 * proxy forwards /ws in dev). VITE_WS_BASE overrides for split hosts. */
export function wsUrl(params: Record<string, string | number>, path = "/ws/agent"): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const override = import.meta.env.VITE_WS_BASE as string | undefined;
  const base = override
    ? override.replace(/^http/, "ws").replace(/\/$/, "")
    : `${proto}//${location.host}`;
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) usp.set(k, String(v));
  return `${base}${path}?${usp.toString()}`;
}
