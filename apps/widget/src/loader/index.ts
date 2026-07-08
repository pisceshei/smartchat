/**
 * SmartChat widget loader — the file embedded on merchant sites as
 * /js/project_{widget_key}.js. Vanilla TS, no framework, budget < 25KB raw.
 *
 * Responsibilities:
 *  - resolve widget key + endpoints, fetch bootstrap config
 *  - render launcher button + unread badge + panel that hosts the chat iframe
 *  - postMessage bridge to the chat app
 *  - legacy `ssq.push([...])` API surface + modern `window.SmartChat`
 *  - auto page_view tracking (initial + history pushState/replaceState/popstate)
 */
import type { WidgetBootstrap } from "../shared/config";
import { DEFAULT_PRIMARY, localized } from "../shared/config";
import type {
  ChatToLoader,
  Endpoints,
  LoaderToChat,
  LoginInfo,
  PageInfo,
} from "../shared/protocol";
import { unwrap, wrap } from "../shared/protocol";

// Replaced with the real key when served as /js/project_{key}.js.
const INJECTED_KEY = "__WIDGET_KEY__";

interface LoaderSettings {
  apiBase?: string;
  wsBase?: string;
  assetBase?: string;
  key?: string;
  lang?: string;
}

type UnreadCb = (count: number) => void;

interface SsqLike {
  push(cmd: unknown[]): void;
}

declare global {
  interface Window {
    ssq?: unknown[] | SsqLike;
    SmartChat?: Record<string, unknown>;
    SMARTCHAT_SETTINGS?: LoaderSettings;
    __SMARTCHAT_LOADED__?: boolean;
  }
}

(function boot() {
  const w = window;
  if (w.__SMARTCHAT_LOADED__) return;
  w.__SMARTCHAT_LOADED__ = true;

  const script = document.currentScript as HTMLScriptElement | null;
  const settings: LoaderSettings = w.SMARTCHAT_SETTINGS || {};

  // ---- key + endpoint resolution -----------------------------------------
  function scriptUrl(): URL | null {
    try {
      return script && script.src ? new URL(script.src) : null;
    } catch {
      return null;
    }
  }

  function resolveKey(): string {
    if (settings.key) return settings.key;
    if (INJECTED_KEY && INJECTED_KEY.indexOf("__") !== 0) return INJECTED_KEY;
    const ds = script && script.getAttribute("data-key");
    if (ds) return ds;
    const u = scriptUrl();
    if (u) {
      const m = /project_([A-Za-z0-9_-]+)\.js/.exec(u.pathname);
      if (m) return m[1];
      const qk = u.searchParams.get("key");
      if (qk) return qk;
    }
    return "";
  }

  const key = resolveKey();
  if (!key) return; // nothing we can do without a widget key

  const srcOrigin = (() => {
    const u = scriptUrl();
    return u ? u.origin : location.origin;
  })();

  const apiBase = (settings.apiBase || srcOrigin).replace(/\/$/, "");
  const wsBase = (
    settings.wsBase || apiBase.replace(/^http/, "ws")
  ).replace(/\/$/, "");
  const assetBase = (settings.assetBase || srcOrigin).replace(/\/$/, "");
  const endpoints: Endpoints = { apiBase, wsBase };

  // Raw BCP47 tag, passed through to the chat app whose detectLang() picks
  // the UI locale; uiLang is the same mapping applied locally for launcher
  // text lookups.
  const lang = (
    settings.lang ||
    document.documentElement.lang ||
    navigator.language ||
    "en"
  ).trim() || "en";
  const uiLang = (() => {
    const l = lang.toLowerCase();
    if (l.indexOf("zh") !== 0) return "en";
    return /^zh[-_]?(cn|sg|hans)/.test(l) ? "zh-CN" : "zh-Hant";
  })();

  // ---- state ---------------------------------------------------------------
  let config: WidgetBootstrap | null = null;
  let iframe: HTMLIFrameElement | null = null;
  let iframeReady = false;
  let isOpen = false;
  let unread = 0;
  let loginInfo: LoginInfo | null = null;
  const unreadCbs: UnreadCb[] = [];
  const outbox: LoaderToChat[] = []; // buffered until iframe ready
  let chatOrigin = "";

  let root: HTMLElement | null = null;
  let launcher: HTMLButtonElement | null = null;
  let badge: HTMLSpanElement | null = null;
  let panel: HTMLDivElement | null = null;

  function pageInfo(): PageInfo {
    return {
      url: location.href,
      title: document.title,
      referrer: document.referrer || undefined,
    };
  }

  // ---- bridge ---------------------------------------------------------------
  function post(msg: LoaderToChat): void {
    if (iframe && iframeReady && iframe.contentWindow) {
      iframe.contentWindow.postMessage(wrap(msg), chatOrigin);
    } else {
      outbox.push(msg);
    }
  }

  function flushOutbox(): void {
    while (outbox.length) {
      const m = outbox.shift();
      if (m && iframe && iframe.contentWindow) {
        iframe.contentWindow.postMessage(wrap(m), chatOrigin);
      }
    }
  }

  window.addEventListener("message", (ev: MessageEvent) => {
    if (!chatOrigin || ev.origin !== chatOrigin) return;
    const msg = unwrap<ChatToLoader>(ev.data);
    if (!msg) return;
    switch (msg.t) {
      case "ready": {
        iframeReady = true;
        if (iframe && iframe.contentWindow) {
          iframe.contentWindow.postMessage(
            wrap<LoaderToChat>({
              t: "init",
              config: config as WidgetBootstrap,
              endpoints,
              widgetKey: key,
              lang,
              page: pageInfo(),
              loginInfo,
              open: isOpen,
            }),
            chatOrigin,
          );
        }
        flushOutbox();
        break;
      }
      case "unread":
        setUnread(msg.count);
        break;
      case "request_close":
        close();
        break;
      case "request_open":
        open();
        break;
    }
  });

  // ---- UI -------------------------------------------------------------------
  const CSS = [
    ":host{all:initial}",
    "*{box-sizing:border-box;margin:0;padding:0}",
    ".sc-root{position:fixed;z-index:2147483000;bottom:var(--sc-oy);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang TC','Microsoft JhengHei',sans-serif}",
    ".sc-root.right{right:var(--sc-ox)}",
    ".sc-root.left{left:var(--sc-ox)}",
    ".sc-launcher{position:relative;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;background:var(--sc-primary);color:#fff;display:flex;align-items:center;justify-content:center;box-shadow:0 6px 24px rgba(0,0,0,.22);transition:transform .18s ease,box-shadow .18s ease;-webkit-tap-highlight-color:transparent}",
    ".sc-launcher:hover{transform:scale(1.06);box-shadow:0 10px 28px rgba(0,0,0,.26)}",
    ".sc-launcher svg{width:26px;height:26px;transition:opacity .15s ease,transform .2s ease;position:absolute}",
    ".sc-ic-x{opacity:0;transform:rotate(-45deg)}",
    ".sc-root.open .sc-ic-x{opacity:1;transform:rotate(0)}",
    ".sc-root.open .sc-ic-chat{opacity:0;transform:rotate(45deg)}",
    ".sc-badge{position:absolute;top:-4px;right:-4px;min-width:19px;height:19px;padding:0 5px;border-radius:10px;background:#EF4444;color:#fff;font-size:11px;font-weight:700;line-height:19px;text-align:center;display:none;box-shadow:0 1px 4px rgba(0,0,0,.3)}",
    ".sc-root.hasunread .sc-badge{display:block}",
    ".sc-panel{position:fixed;bottom:calc(var(--sc-oy) + 68px);width:384px;height:min(640px,calc(100vh - 110px));min-height:320px;border-radius:16px;overflow:hidden;box-shadow:0 12px 48px rgba(15,23,42,.28);background:#fff;opacity:0;pointer-events:none;transform:translateY(12px) scale(.97);transition:opacity .2s ease,transform .2s ease}",
    ".sc-root.right .sc-panel{right:var(--sc-ox);transform-origin:bottom right}",
    ".sc-root.left .sc-panel{left:var(--sc-ox);transform-origin:bottom left}",
    ".sc-root.open .sc-panel{opacity:1;pointer-events:auto;transform:translateY(0) scale(1)}",
    ".sc-frame{width:100%;height:100%;border:0;display:block;background:#fff}",
    "@media(max-width:640px){.sc-panel{left:0!important;right:0!important;top:0;bottom:0;width:100%;height:100%;border-radius:0}.sc-root.open .sc-launcher{opacity:0;pointer-events:none}}",
  ].join("");

  const ICON_CHAT =
    '<svg class="sc-ic-chat" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 3C7 3 3 6.6 3 11c0 2.2 1 4.2 2.7 5.6-.1 1-.5 2.1-1.4 3 1.7 0 3.2-.6 4.3-1.3 1.1.4 2.2.7 3.4.7 5 0 9-3.6 9-8s-4-8-9-8Z" fill="currentColor"/></svg>';
  const ICON_X =
    '<svg class="sc-ic-x" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';

  function mountUi(cfg: WidgetBootstrap): void {
    const ap = cfg.appearance || {};
    const host = document.createElement("div");
    host.setAttribute("data-smartchat", key);
    const shadow = host.attachShadow ? host.attachShadow({ mode: "open" }) : host;

    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);

    root = document.createElement("div");
    root.className = "sc-root " + (ap.position === "left" ? "left" : "right");
    root.style.setProperty("--sc-primary", ap.primary_color || DEFAULT_PRIMARY);
    root.style.setProperty("--sc-ox", (ap.offset_x ?? 20) + "px");
    root.style.setProperty("--sc-oy", (ap.offset_y ?? 20) + "px");

    panel = document.createElement("div");
    panel.className = "sc-panel";

    iframe = document.createElement("iframe");
    iframe.className = "sc-frame";
    iframe.title = (cfg.brand && cfg.brand.name) || "SmartChat";
    iframe.setAttribute("allow", "microphone; clipboard-write");
    const chatUrl = new URL(assetBase + "/chat/index.html");
    chatUrl.searchParams.set("k", key);
    chatUrl.searchParams.set("po", location.origin);
    chatUrl.searchParams.set("lang", lang);
    chatOrigin = chatUrl.origin;
    iframe.src = chatUrl.toString();
    panel.appendChild(iframe);

    launcher = document.createElement("button");
    launcher.className = "sc-launcher";
    launcher.type = "button";
    launcher.setAttribute(
      "aria-label",
      localized(ap.launcher_text, uiLang) || "Chat",
    );
    launcher.innerHTML = ICON_CHAT + ICON_X;
    badge = document.createElement("span");
    badge.className = "sc-badge";
    launcher.appendChild(badge);
    launcher.addEventListener("click", toggle);

    root.appendChild(panel);
    root.appendChild(launcher);
    shadow.appendChild(root);
    document.body.appendChild(host);
  }

  // ---- public actions --------------------------------------------------------
  let wantOpen = false; // chatOpen called before the UI mounted

  function open(): void {
    if (!root) {
      wantOpen = true;
      return;
    }
    if (isOpen) return;
    isOpen = true;
    root.classList.add("open");
    root.classList.remove("hasunread");
    post({ t: "visibility", open: true });
  }

  function close(): void {
    wantOpen = false;
    if (!isOpen || !root) return;
    isOpen = false;
    root.classList.remove("open");
    post({ t: "visibility", open: false });
  }

  function toggle(): void {
    isOpen ? close() : open();
  }

  function setUnread(count: number): void {
    unread = count;
    if (root && badge) {
      badge.textContent = count > 9 ? "9+" : String(count);
      root.classList.toggle("hasunread", count > 0 && !isOpen);
    }
    for (const cb of unreadCbs) {
      try {
        cb(count);
      } catch {
        /* user callback errors are not ours */
      }
    }
  }

  function setLoginInfo(info: LoginInfo): void {
    if (!info || typeof info !== "object") return;
    loginInfo = info;
    post({ t: "login", info });
  }

  function sendTextMessage(text: string): void {
    if (typeof text !== "string" || !text.trim()) return;
    post({ t: "send_text", text });
  }

  function track(event: string, props?: Record<string, unknown>): void {
    if (typeof event !== "string" || !event) return;
    post({ t: "track", event, props: props || {}, page: pageInfo() });
  }

  function onUnRead(cb: UnreadCb): void {
    if (typeof cb !== "function") return;
    unreadCbs.push(cb);
    try {
      cb(unread);
    } catch {
      /* ignore */
    }
  }

  // ---- ssq compat + modern API -------------------------------------------------
  function exec(cmd: unknown): void {
    if (!Array.isArray(cmd) || typeof cmd[0] !== "string") return;
    const [name, a1, a2] = cmd;
    switch (name) {
      case "setLoginInfo":
        setLoginInfo(a1 as LoginInfo);
        break;
      case "chatOpen":
        open();
        break;
      case "chatClose":
        close();
        break;
      case "onUnRead":
        onUnRead(a1 as UnreadCb);
        break;
      case "sendTextMessage":
        sendTextMessage(a1 as string);
        break;
      case "track":
        track(a1 as string, a2 as Record<string, unknown>);
        break;
      default:
        break; // unknown commands ignored for forward compatibility
    }
  }

  const pending: unknown[] = Array.isArray(w.ssq) ? (w.ssq as unknown[]).slice() : [];
  w.ssq = { push: exec };
  w.SmartChat = {
    open,
    close,
    toggle,
    setLoginInfo,
    sendTextMessage,
    track,
    onUnread: onUnRead,
    isOpen: () => isOpen,
    getUnread: () => unread,
  };

  // ---- page view tracking -------------------------------------------------------
  let lastTracked = "";
  function pageView(): void {
    if (location.href === lastTracked) return;
    const referrer = lastTracked || document.referrer || undefined;
    lastTracked = location.href;
    post({
      t: "page_view",
      page: { url: location.href, title: document.title, referrer },
    });
  }

  function hookHistory(): void {
    const fire = () => setTimeout(pageView, 0);
    const orig = {
      pushState: history.pushState,
      replaceState: history.replaceState,
    };
    history.pushState = function (...args: Parameters<History["pushState"]>) {
      const r = orig.pushState.apply(this, args);
      fire();
      return r;
    };
    history.replaceState = function (
      ...args: Parameters<History["replaceState"]>
    ) {
      const r = orig.replaceState.apply(this, args);
      fire();
      return r;
    };
    window.addEventListener("popstate", fire);
    window.addEventListener("hashchange", fire);
  }

  // ---- bootstrap ------------------------------------------------------------------
  function fetchConfig(attempt: number): void {
    fetch(apiBase + "/api/v1/widget/bootstrap?key=" + encodeURIComponent(key), {
      method: "GET",
      mode: "cors",
    })
      .then((r) => {
        if (!r.ok) throw new Error("bootstrap " + r.status);
        return r.json();
      })
      .then((cfg: WidgetBootstrap) => {
        config = cfg;
        whenBody(() => {
          mountUi(cfg);
          hookHistory();
          pageView();
          for (const cmd of pending) exec(cmd);
          if (wantOpen) open();
        });
      })
      .catch(() => {
        if (attempt < 2) setTimeout(() => fetchConfig(attempt + 1), 4000);
      });
  }

  function whenBody(fn: () => void): void {
    if (document.body) fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  }

  fetchConfig(0);
})();
