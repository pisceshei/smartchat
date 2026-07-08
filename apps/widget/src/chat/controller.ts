/**
 * Chat app controller — owns the WidgetApi, the postMessage bridge to the
 * loader, the visitor session (localStorage token), and the realtime feed.
 * Components call the exported actions; state flows through ./store.
 */
import { localized, type WidgetBootstrap } from "../shared/config";
import type {
  CardButton,
  ContentBlock,
  MessageContent,
  QuickButton,
  WireMessage,
} from "../shared/content";
import type {
  ChatToLoader,
  Endpoints,
  LoaderToChat,
  LoginInfo,
  PageInfo,
} from "../shared/protocol";
import { unwrap, wrap } from "../shared/protocol";
import { newClientMsgId, WidgetApi, type WidgetEvent } from "./api";
import { detectLang, setLang, t } from "./i18n";
import { Realtime } from "./realtime";
import { markMessage, store, upsertMessage, type UiMessage } from "./store";

let api: WidgetApi | null = null;
let rt: Realtime | null = null;
let widgetKey = "";
let parentOrigin = "";
let embedded = false;
let currentPage: PageInfo = { url: "", title: "" };
let sessionReady = false;
let pendingLogin: LoginInfo | null = null;
const preSessionQueue: Array<() => void> = [];
let typingTimer: ReturnType<typeof setTimeout> | null = null;
let lastTypingSentAt = 0;
let composerErrorTimer: ReturnType<typeof setTimeout> | null = null;

const LS = {
  token: () => `sc:${widgetKey}:token`,
  prechat: () => `sc:${widgetKey}:prechat`,
  offlineEmail: () => `sc:${widgetKey}:offline_email`,
};

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage blocked (e.g. 3rd-party cookies off) — session-only mode */
  }
}

// ---------------------------------------------------------------------------
// bootstrap / bridge
// ---------------------------------------------------------------------------

export function bootChat(): void {
  const qs = new URLSearchParams(location.search);
  parentOrigin = qs.get("po") || "";
  embedded = window.parent !== window && !!parentOrigin;

  if (embedded) {
    window.addEventListener("message", onParentMessage);
    postToParent({ t: "ready" });
  } else {
    // standalone (dev / direct link): fetch config ourselves and open.
    const key = qs.get("k") || "";
    const apiBase = qs.get("api") || location.origin;
    const wsBase = qs.get("ws") || apiBase.replace(/^http/, "ws");
    const boot = new WidgetApi(apiBase.replace(/\/$/, ""), key);
    boot
      .bootstrap()
      .then((config) => {
        init({
          t: "init",
          config,
          endpoints: { apiBase: apiBase.replace(/\/$/, ""), wsBase: wsBase.replace(/\/$/, "") },
          widgetKey: key,
          lang: qs.get("lang") || "",
          page: { url: location.href, title: document.title },
          loginInfo: null,
          open: true,
        });
      })
      .catch((e) => {
        // invalid key or an init crash — keep the skeleton but surface why
        console.error("[smartchat-widget] standalone boot failed", e);
      });
  }
}

function postToParent(msg: ChatToLoader): void {
  if (embedded && window.parent) {
    window.parent.postMessage(wrap(msg), parentOrigin);
  }
}

function onParentMessage(ev: MessageEvent): void {
  if (ev.origin !== parentOrigin) return;
  const msg = unwrap<LoaderToChat>(ev.data);
  if (!msg) return;
  switch (msg.t) {
    case "init":
      init(msg);
      break;
    case "visibility":
      setVisibility(msg.open);
      break;
    case "login":
      applyLogin(msg.info);
      break;
    case "send_text":
      sendText(msg.text);
      break;
    case "page_view":
      currentPage = msg.page;
      trackEvent("page_view", {}, msg.page);
      break;
    case "track":
      trackEvent(msg.event, msg.props, msg.page);
      break;
  }
}

let initialised = false;

function init(msg: Extract<LoaderToChat, { t: "init" }>): void {
  if (initialised) return;
  initialised = true;

  widgetKey = msg.widgetKey;
  currentPage = msg.page;
  pendingLogin = msg.loginInfo;
  const config = msg.config;
  const lang = detectLang(msg.lang || config.locale_default);
  setLang(lang);

  const prechatDone = lsGet(LS.prechat()) === "1" || !!msg.loginInfo;
  const pc = config.pre_chat;
  const prechatEnabled = !!pc?.enabled && (pc.fields?.length ?? 0) > 0;

  store.set({
    ready: true,
    config,
    lang,
    open: msg.open,
    conn: "connecting",
    view: config.home?.enabled ? "home" : "chat",
    prechatBlocking: prechatEnabled && !!pc?.required_before_chat && !prechatDone,
    prechatVisible: prechatEnabled && !prechatDone,
    offlineEmailSaved: !!lsGet(LS.offlineEmail()),
    loginInfo: msg.loginInfo,
  });

  api = new WidgetApi(msg.endpoints.apiBase, widgetKey);
  establishSession(msg.endpoints, config, 0);
}

// ---------------------------------------------------------------------------
// session + realtime
// ---------------------------------------------------------------------------

function establishSession(endpoints: Endpoints, config: WidgetBootstrap, attempt: number): void {
  if (!api) return;
  const stored = lsGet(LS.token());
  api
    .session({
      visitor_token: stored,
      login_info: pendingLogin,
      page: currentPage,
      lang: store.get().lang,
    })
    .then(async (res) => {
      lsSet(LS.token(), res.visitor_token);
      sessionReady = true;
      pendingLogin = null;

      let maxSeq = res.seq ?? 0;
      try {
        const hist = await api!.history();
        for (const m of hist.messages) {
          upsertMessage(m as UiMessage);
          if (typeof m.seq === "number" && m.seq > maxSeq) maxSeq = m.seq;
        }
      } catch {
        /* history is best-effort; realtime still delivers new messages */
      }
      ensureWelcome(config);

      rt = new Realtime({
        wsBase: endpoints.wsBase,
        api: api!,
        onEvent: handleEvent,
        onState: (s) => store.set({ conn: s === "connecting" ? "connecting" : s }),
        onResync: resyncHistory,
      });
      rt.start(maxSeq);

      // Initial page_view arrives from the loader bridge (queued until now);
      // standalone dev mode tracks nothing extra to avoid double counting.
      while (preSessionQueue.length) preSessionQueue.shift()!();
    })
    .catch(() => {
      sessionReady = false;
      const delay = Math.min(2_000 * 2 ** attempt, 60_000);
      setTimeout(() => establishSession(endpoints, config, attempt + 1), delay);
    });
}

function ensureWelcome(config: WidgetBootstrap): void {
  const s = store.get();
  if (s.messages.length > 0) return;
  const text =
    localized(config.brand?.welcome_text, s.lang) || t("welcome_default");
  if (!text) return;
  upsertMessage({
    id: "welcome",
    sender_type: "member",
    sender_name: config.brand?.name || null,
    content: { blocks: [{ kind: "text", text }] },
    created_at: new Date().toISOString(),
  });
}

async function resyncHistory(): Promise<void> {
  if (!api) return;
  try {
    const hist = await api.history();
    let maxSeq = 0;
    store.set((s) => {
      const pendingLocal = s.messages.filter((m) => m.local_state);
      const merged: UiMessage[] = [...(hist.messages as UiMessage[])];
      for (const p of pendingLocal) {
        if (!merged.some((m) => m.client_msg_id && m.client_msg_id === p.client_msg_id)) {
          merged.push(p);
        }
      }
      merged.sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
      return { messages: merged };
    });
    for (const m of hist.messages) {
      if (typeof m.seq === "number" && m.seq > maxSeq) maxSeq = m.seq;
    }
    rt?.advance(maxSeq);
  } catch {
    /* next resync will retry */
  }
}

let agentTypingTimer: ReturnType<typeof setTimeout> | null = null;

function handleEvent(_seq: number, event: WidgetEvent): void {
  switch (event.type) {
    case "message.created": {
      const m = event.payload["message"] as WireMessage | undefined;
      if (!m) return;
      upsertMessage({ ...m, local_state: undefined } as UiMessage);
      if (m.sender_type !== "contact") {
        store.set({ agentTyping: false });
        const s = store.get();
        if (!s.open || s.view === "home") {
          const unread = s.unread + 1;
          store.set({ unread });
          if (!s.open) postToParent({ t: "unread", count: unread });
        }
      }
      break;
    }
    case "message.updated": {
      const m = event.payload["message"] as WireMessage | undefined;
      if (m) upsertMessage(m as UiMessage);
      break;
    }
    case "typing": {
      const actor = event.payload["actor"];
      if (actor === "contact") return;
      store.set({ agentTyping: true });
      if (agentTypingTimer) clearTimeout(agentTypingTimer);
      agentTypingTimer = setTimeout(() => store.set({ agentTyping: false }), 4_000);
      break;
    }
    case "widget.status": {
      // agents came online / went offline while the widget is open
      const isOnline = event.payload["is_online"];
      if (typeof isOnline === "boolean") {
        store.set((s) =>
          s.config
            ? {
                config: {
                  ...s.config,
                  offline: { ...(s.config.offline || {}), is_online: isOnline },
                },
              }
            : {},
        );
      }
      break;
    }
    default:
      break; // forward-compatible: unknown events ignored
  }
}

// ---------------------------------------------------------------------------
// actions (called from components / bridge)
// ---------------------------------------------------------------------------

function setVisibility(open: boolean): void {
  store.set({ open });
  if (open) {
    store.set({ unread: 0 });
    postToParent({ t: "unread", count: 0 });
  }
}

export function requestClose(): void {
  if (embedded) postToParent({ t: "request_close" });
}

export function goChat(): void {
  store.set({ view: "chat", unread: 0 });
  postToParent({ t: "unread", count: 0 });
}

export function goHome(): void {
  store.set({ view: "home" });
}

function applyLogin(info: LoginInfo): void {
  store.set({ loginInfo: info, prechatBlocking: false, prechatVisible: false });
  if (sessionReady && api) {
    api.identify(info).catch(() => undefined);
  } else {
    pendingLogin = info;
  }
}

function whenSession(fn: () => void): void {
  if (sessionReady) fn();
  else preSessionQueue.push(fn);
}

function trackEvent(event: string, props: Record<string, unknown>, page: PageInfo): void {
  whenSession(() => {
    api?.track(event, props, page).catch(() => undefined);
  });
}

export function sendText(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  sendContent({ blocks: [{ kind: "text", text: trimmed }] });
}

export function sendContent(content: MessageContent): void {
  const clientMsgId = newClientMsgId();
  const local: UiMessage = {
    id: "local-" + clientMsgId,
    sender_type: "contact",
    content,
    client_msg_id: clientMsgId,
    created_at: new Date().toISOString(),
    local_state: "pending",
  };
  upsertMessage(local);
  deliver(local);
}

function deliver(local: UiMessage): void {
  whenSession(() => {
    api!
      .sendMessage(local.content, local.client_msg_id!)
      .then((res) => {
        // Do NOT advance the realtime cursor here: other events with lower
        // seq may still be in flight on the socket and would be skipped.
        // The WS echo of this message is deduped by id in upsertMessage.
        upsertMessage({ ...res.message, local_state: undefined } as UiMessage);
      })
      .catch(() => {
        markMessage(local.id, { local_state: "failed" });
      });
  });
}

export function retryMessage(id: string): void {
  const msg = store.get().messages.find((m) => m.id === id);
  if (!msg || msg.local_state !== "failed") return;
  markMessage(id, { local_state: "pending" });
  deliver(msg);
}

const MAX_UPLOAD = 20 * 1024 * 1024;

export function composerError(text: string): void {
  store.set({ composerError: text });
  if (composerErrorTimer) clearTimeout(composerErrorTimer);
  composerErrorTimer = setTimeout(() => store.set({ composerError: null }), 4_000);
}

export async function sendFile(file: File): Promise<void> {
  if (!api) return;
  if (file.size > MAX_UPLOAD) {
    composerError(t("upload_too_large"));
    return;
  }
  try {
    const up = await api.upload(file);
    const mime = up.mime || file.type || "";
    const mediaType = mime.startsWith("image/")
      ? "image"
      : mime.startsWith("video/")
        ? "video"
        : mime.startsWith("audio/")
          ? "audio"
          : "file";
    const block: ContentBlock = {
      kind: "media",
      media_type: mediaType,
      file_id: up.file_id,
      url: up.url,
      mime,
      size: up.size ?? file.size,
      name: up.name ?? file.name,
    };
    sendContent({ blocks: [block] });
  } catch {
    composerError(t("upload_failed"));
  }
}

export function tapQuickButton(messageId: string, btn: QuickButton): void {
  store.set((s) => ({
    answeredQuickBlocks: [...s.answeredQuickBlocks, messageId],
  }));
  sendContent({
    blocks: [{ kind: "button_reply", payload: btn.id, text: btn.text }],
  });
}

export function tapCardButton(btn: CardButton): void {
  if (btn.action === "url") {
    window.open(btn.value, "_blank", "noopener");
  } else {
    sendContent({
      blocks: [{ kind: "button_reply", payload: btn.value, text: btn.text }],
    });
  }
}

export function submitPrechat(values: Record<string, unknown>): void {
  lsSet(LS.prechat(), "1");
  store.set({ prechatBlocking: false, prechatVisible: false });
  whenSession(() => {
    api
      ?.lead(values, currentPage)
      .then(() => {
        // Reference behavior: the submitted details also land in the
        // conversation as a normal visitor message, one field per line.
        const s = store.get();
        const fields = s.config?.pre_chat?.fields ?? [];
        const lines = fields
          .filter((f) => typeof values[f.key] === "string" && (values[f.key] as string).trim())
          .map((f) => `${localized(f.label, s.lang) || f.key}: ${values[f.key] as string}`);
        if (lines.length > 0) sendText(lines.join("\n"));
      })
      .catch(() => undefined);
  });
  if (typeof values["email"] === "string" && values["email"]) {
    lsSet(LS.offlineEmail(), values["email"] as string);
    store.set({ offlineEmailSaved: true });
  }
}

export function skipPrechat(): void {
  if (store.get().prechatBlocking) return;
  store.set({ prechatVisible: false });
}

export function saveOfflineEmail(email: string): void {
  lsSet(LS.offlineEmail(), email);
  store.set({ offlineEmailSaved: true });
  whenSession(() => {
    api?.lead({ email }, currentPage).catch(() => undefined);
  });
}

/** Composer keystroke → throttled visitor typing signal (WS-only, best effort). */
export function notifyTyping(): void {
  const now = Date.now();
  if (now - lastTypingSentAt < 3_000) return;
  lastTypingSentAt = now;
  rt?.sendTyping();
  if (typingTimer) clearTimeout(typingTimer);
}
