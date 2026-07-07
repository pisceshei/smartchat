/**
 * Widget REST client. All calls carry the visitor token (Bearer) once the
 * session is established; sends are idempotent via client_msg_id.
 *
 * Endpoints (see README "Backend contract"):
 *   GET  /api/v1/widget/bootstrap?key=
 *   POST /api/v1/widget/session          {widget_key, visitor_token?, login_info?, page?, lang?}
 *   POST /api/v1/widget/identify         {login_info}
 *   GET  /api/v1/widget/messages?before=&limit=
 *   POST /api/v1/widget/messages         {client_msg_id, content}
 *   POST /api/v1/widget/uploads          multipart {file}
 *   POST /api/v1/widget/lead             {fields, page?}
 *   POST /api/v1/widget/track            {event, props, page}
 *   GET  /api/v1/widget/events?cursor=&wait=   (long-poll fallback)
 */
import type { WidgetBootstrap } from "../shared/config";
import type { MessageContent, WireMessage } from "../shared/content";
import type { LoginInfo, PageInfo } from "../shared/protocol";

export interface SessionResponse {
  visitor_token: string;
  contact_id?: string | null;
  conversation_id?: string | null;
  /** current per-visitor event sequence — resume point for realtime */
  seq?: number | null;
}

export interface SendResponse {
  message: WireMessage;
  seq?: number | null;
}

export interface UploadResponse {
  file_id: string;
  url?: string | null;
  mime?: string | null;
  size?: number | null;
  name?: string | null;
}

export interface WidgetEvent {
  type: string;
  payload: Record<string, unknown>;
}

export interface EventsResponse {
  events: { seq: number; event: WidgetEvent }[];
  cursor: number;
}

export function newClientMsgId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return (
    "c" +
    Date.now().toString(36) +
    Math.random().toString(36).slice(2, 10) +
    Math.random().toString(36).slice(2, 10)
  );
}

export class WidgetApi {
  token: string | null = null;

  constructor(
    private apiBase: string,
    private widgetKey: string,
  ) {}

  private url(path: string): string {
    return this.apiBase + "/api/v1/widget" + path;
  }

  private headers(json = true): Record<string, string> {
    const h: Record<string, string> = { "X-Widget-Key": this.widgetKey };
    if (json) h["Content-Type"] = "application/json";
    if (this.token) h["Authorization"] = "Bearer " + this.token;
    return h;
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    const res = await fetch(this.url(path), init);
    if (!res.ok) {
      throw new ApiError(res.status, await res.text().catch(() => ""));
    }
    return (await res.json()) as T;
  }

  bootstrap(): Promise<WidgetBootstrap> {
    return this.req<WidgetBootstrap>(
      "/bootstrap?key=" + encodeURIComponent(this.widgetKey),
    );
  }

  async session(payload: {
    visitor_token?: string | null;
    login_info?: LoginInfo | null;
    page?: PageInfo;
    lang?: string;
  }): Promise<SessionResponse> {
    const res = await this.req<SessionResponse>("/session", {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ widget_key: this.widgetKey, ...payload }),
    });
    this.token = res.visitor_token;
    return res;
  }

  identify(info: LoginInfo): Promise<{ ok: boolean }> {
    return this.req("/identify", {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ login_info: info }),
    });
  }

  history(before?: string, limit = 50): Promise<{ messages: WireMessage[] }> {
    const q = new URLSearchParams();
    if (before) q.set("before", before);
    q.set("limit", String(limit));
    return this.req("/messages?" + q.toString(), { headers: this.headers(false) });
  }

  sendMessage(content: MessageContent, clientMsgId: string): Promise<SendResponse> {
    return this.req("/messages", {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ client_msg_id: clientMsgId, content }),
    });
  }

  async upload(file: File): Promise<UploadResponse> {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const res = await fetch(this.url("/uploads"), {
      method: "POST",
      headers: this.headers(false),
      body: fd,
    });
    if (!res.ok) throw new ApiError(res.status, await res.text().catch(() => ""));
    return (await res.json()) as UploadResponse;
  }

  lead(fields: Record<string, unknown>, page?: PageInfo): Promise<{ ok: boolean }> {
    return this.req("/lead", {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ fields, page }),
    });
  }

  track(
    event: string,
    props: Record<string, unknown>,
    page: PageInfo,
  ): Promise<{ ok: boolean }> {
    return this.req("/track", {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify({ event, props, page }),
    });
  }

  /** Long-poll fallback; server holds the request up to `wait` seconds. */
  events(cursor: number, wait = 25, signal?: AbortSignal): Promise<EventsResponse> {
    return this.req("/events?cursor=" + cursor + "&wait=" + wait, {
      headers: this.headers(false),
      signal,
    });
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    body: string,
  ) {
    super("api " + status + ": " + body.slice(0, 200));
  }
}
