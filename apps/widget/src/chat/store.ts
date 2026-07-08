/** Minimal observable store + Preact hook — no external state library. */
import { useEffect, useState } from "preact/hooks";

import type { WidgetBootstrap } from "../shared/config";
import type { WireMessage } from "../shared/content";
import type { LoginInfo } from "../shared/protocol";
import type { Lang } from "./i18n";

export type ConnState = "boot" | "connecting" | "online" | "reconnecting";

/** A message in the UI list: server message or optimistic local echo. */
export interface UiMessage extends WireMessage {
  local_state?: "pending" | "failed";
}

export interface AppState {
  ready: boolean;
  config: WidgetBootstrap | null;
  lang: Lang;
  open: boolean;
  conn: ConnState;
  /** current screen inside the panel (home only when config.home.enabled) */
  view: "home" | "chat";
  messages: UiMessage[];
  agentTyping: boolean;
  unread: number;
  /** pre-chat form must be completed before composing */
  prechatBlocking: boolean;
  /** pre-chat form visible (blocking or optional) */
  prechatVisible: boolean;
  offlineEmailSaved: boolean;
  loginInfo: LoginInfo | null;
  /** ids of quick_buttons blocks already answered (chips disabled) */
  answeredQuickBlocks: string[];
  /** transient composer error toast (upload too large / failed) */
  composerError: string | null;
}

export interface Store<T> {
  get(): T;
  set(patch: Partial<T> | ((s: T) => Partial<T>)): void;
  subscribe(fn: (s: T) => void): () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let state = initial;
  const subs = new Set<(s: T) => void>();
  return {
    get: () => state,
    set(patch) {
      const p = typeof patch === "function" ? (patch as (s: T) => Partial<T>)(state) : patch;
      state = { ...state, ...p };
      subs.forEach((fn) => fn(state));
    },
    subscribe(fn) {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
  };
}

export const store = createStore<AppState>({
  ready: false,
  config: null,
  lang: "en",
  open: false,
  conn: "boot",
  view: "chat",
  messages: [],
  agentTyping: false,
  unread: 0,
  prechatBlocking: false,
  prechatVisible: false,
  offlineEmailSaved: false,
  loginInfo: null,
  answeredQuickBlocks: [],
  composerError: null,
});

export function useAppState(): AppState {
  const [s, setS] = useState(store.get());
  useEffect(() => store.subscribe(setS), []);
  return s;
}

/** Insert or replace a message, keeping the list ordered by created_at. */
export function upsertMessage(msg: UiMessage): void {
  store.set((s) => {
    const list = s.messages.slice();
    const byId = list.findIndex((m) => m.id === msg.id);
    const byClient =
      byId < 0 && msg.client_msg_id
        ? list.findIndex((m) => m.client_msg_id === msg.client_msg_id)
        : -1;
    const idx = byId >= 0 ? byId : byClient;
    if (idx >= 0) list[idx] = { ...list[idx], ...msg };
    else list.push(msg);
    list.sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0));
    return { messages: list };
  });
}

export function markMessage(id: string, patch: Partial<UiMessage>): void {
  store.set((s) => ({
    messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)),
  }));
}
