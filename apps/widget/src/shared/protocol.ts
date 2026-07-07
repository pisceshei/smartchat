/**
 * postMessage bridge protocol between the loader (host page) and the chat
 * app (iframe). Every frame is wrapped in an envelope carrying the `__sc`
 * marker so unrelated postMessage traffic is ignored.
 */
import type { WidgetBootstrap } from "./config";

export const BRIDGE_VERSION = 1;

export interface Endpoints {
  /** REST origin, e.g. https://chat.example.com (no trailing slash). */
  apiBase: string;
  /** WebSocket origin, e.g. wss://chat.example.com (no trailing slash). */
  wsBase: string;
}

export interface PageInfo {
  url: string;
  title: string;
  referrer?: string;
}

export interface LoginInfo {
  user_id?: string;
  user_name?: string;
  email?: string;
  phone?: string;
  [k: string]: unknown;
}

/** Loader → chat iframe. */
export type LoaderToChat =
  | {
      t: "init";
      config: WidgetBootstrap;
      endpoints: Endpoints;
      widgetKey: string;
      lang: string;
      page: PageInfo;
      loginInfo: LoginInfo | null;
      open: boolean;
    }
  | { t: "visibility"; open: boolean }
  | { t: "login"; info: LoginInfo }
  | { t: "send_text"; text: string }
  | { t: "page_view"; page: PageInfo }
  | { t: "track"; event: string; props: Record<string, unknown>; page: PageInfo };

/** Chat iframe → loader. */
export type ChatToLoader =
  | { t: "ready" }
  | { t: "unread"; count: number }
  | { t: "request_close" }
  | { t: "request_open" };

export interface Envelope<T> {
  __sc: 1;
  v: number;
  msg: T;
}

export function wrap<T>(msg: T): Envelope<T> {
  return { __sc: 1, v: BRIDGE_VERSION, msg };
}

export function unwrap<T>(data: unknown): T | null {
  if (
    typeof data === "object" &&
    data !== null &&
    (data as Record<string, unknown>).__sc === 1 &&
    typeof (data as Record<string, unknown>).msg === "object"
  ) {
    return (data as Envelope<T>).msg;
  }
  return null;
}
