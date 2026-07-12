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
import { DEFAULT_PRIMARY, localized, mapLangTag } from "../shared/config";
import { buildChatIframeUrl } from "./chatUrl";
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
  /** Set false to disable page-view tracking AND the history.pushState patch
   * (for hosts with anti-tamper scripts or strict consent requirements). */
  trackPageViews?: boolean;
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
  // Claim the singleton only once we know we can actually run, so a broken
  // first snippet (no key) doesn't brick a later correct one.
  w.__SMARTCHAT_LOADED__ = true;

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
  const uiLang = mapLangTag(lang);
  const trackPages = settings.trackPageViews !== false;

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

  let hostEl: HTMLElement | null = null;
  let root: HTMLElement | null = null;
  let launcher: HTMLButtonElement | null = null;
  let badge: HTMLSpanElement | null = null;
  let panel: HTMLDivElement | null = null;
  let unhookHistory: (() => void) | null = null;

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
      // If the iframe never becomes ready (CSP/adblock/network), don't let the
      // buffer grow forever inside the HOST page.
      if (outbox.length > 50) outbox.shift();
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

  const onMessage = (ev: MessageEvent): void => {
    if (!chatOrigin || ev.origin !== chatOrigin) return;
    // Only OUR iframe may drive the launcher — not any other frame that
    // happens to be served from the SmartChat origin.
    if (!iframe || ev.source !== iframe.contentWindow) return;
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
  };
  window.addEventListener("message", onMessage);

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
    // A CLOSED panel must be `visibility:hidden`, not just opacity+pointer-events:
    // several mobile engines still hit-test a cross-origin iframe under a
    // pointer-events:none ancestor, and the invisible frame swallows touchstart —
    // on ≤640px (where the open panel is full-screen) that killed swipe gestures
    // (e.g. Fecify 裝修 carousels) across the ENTIRE host page. visibility:hidden
    // removes the subtree from hit-testing in every engine and keeps the iframe
    // loaded. The delayed `visibility 0s .2s` keeps the close animation visible.
    ".sc-panel{position:fixed;bottom:calc(var(--sc-oy) + 68px);width:384px;height:min(640px,calc(100vh - 110px));height:min(640px,calc(100dvh - 110px));min-height:320px;border-radius:16px;overflow:hidden;box-shadow:0 12px 48px rgba(15,23,42,.28);background:#fff;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(12px) scale(.97);transition:opacity .2s ease,transform .2s ease,visibility 0s linear .2s}",
    ".sc-root.right .sc-panel{right:var(--sc-ox);transform-origin:bottom right}",
    ".sc-root.left .sc-panel{left:var(--sc-ox);transform-origin:bottom left}",
    ".sc-root.open .sc-panel{opacity:1;visibility:visible;pointer-events:auto;transform:translateY(0) scale(1);transition:opacity .2s ease,transform .2s ease}",
    ".sc-frame{width:100%;height:100%;border:0;display:block;background:#fff}",
    // Full-screen geometry ONLY while open — a closed panel must never span the
    // viewport on mobile, whatever its visibility.
    "@media(max-width:640px){.sc-root.open .sc-panel{left:0!important;right:0!important;top:0;bottom:0;width:100%;height:100%;border-radius:0}.sc-root.open .sc-launcher{opacity:0;pointer-events:none}}",
    "@media print{.sc-root{display:none!important}}",
  ].join("");

  const ICON_CHAT =
    '<svg class="sc-ic-chat" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 3C7 3 3 6.6 3 11c0 2.2 1 4.2 2.7 5.6-.1 1-.5 2.1-1.4 3 1.7 0 3.2-.6 4.3-1.3 1.1.4 2.2.7 3.4.7 5 0 9-3.6 9-8s-4-8-9-8Z" fill="currentColor"/></svg>';
  const ICON_X =
    '<svg class="sc-ic-x" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>';

  function mountUi(cfg: WidgetBootstrap): void {
    if (root) return; // idempotent — a retry must never mount a second widget
    const ap = cfg.appearance || {};
    const host = document.createElement("div");
    host.setAttribute("data-smartchat", key);
    // Without Shadow DOM our stylesheet (incl. the `*` reset) would become
    // PAGE-GLOBAL and destroy the merchant's layout — no widget beats that.
    if (!host.attachShadow) return;
    // Style the host itself defensively: fixed + zero-size takes it out of body
    // flow (no phantom height at the page bottom), immunizes it against theme
    // rules like `div:empty{display:none}` or reveal-animation transforms that
    // would otherwise hide the widget or hijack its fixed positioning.
    host.style.cssText =
      "all:initial;position:fixed!important;bottom:0;right:0;width:0;height:0;display:block!important;z-index:2147483000";
    const shadow = host.attachShadow({ mode: "open" });

    // Constructed stylesheets are exempt from the host page's style-src CSP
    // (an inline <style> is not, even inside a shadow root).
    let styled = false;
    try {
      if (shadow.adoptedStyleSheets !== undefined) {
        const sheet = new CSSStyleSheet();
        sheet.replaceSync(CSS);
        shadow.adoptedStyleSheets = [sheet];
        styled = true;
      }
    } catch {
      /* constructed sheets unsupported → <style> fallback */
    }
    if (!styled) {
      const style = document.createElement("style");
      style.textContent = CSS;
      shadow.appendChild(style);
    }

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
    iframe.setAttribute("allow", "clipboard-write");
    iframe.setAttribute(
      "sandbox",
      "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox",
    );
    const chatUrl = buildChatIframeUrl(assetBase, key, location.origin, lang);
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
    hostEl = host;
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

  function hide(): void {
    if (root) root.style.display = "none";
  }

  function show(): void {
    if (root) root.style.display = "";
  }

  /** Remove the widget from the page entirely: DOM, history patch, listeners,
   * globals. Lets SPA hosts and A/B setups unload us cleanly. */
  function destroy(): void {
    try {
      close();
      if (hostEl && hostEl.parentNode) hostEl.parentNode.removeChild(hostEl);
      hostEl = null;
      root = launcher = badge = panel = null;
      iframe = null;
      iframeReady = false;
      if (unhookHistory) {
        unhookHistory();
        unhookHistory = null;
      }
      window.removeEventListener("message", onMessage);
      if (w.SmartChat === api) delete w.SmartChat;
      w.__SMARTCHAT_LOADED__ = false;
    } catch {
      /* teardown must never throw into the host */
    }
  }

  // ssq compat: claim the global only when it is absent or a plain pre-seed
  // array. A LIVE foreign object (e.g. the site still runs real SaleSmartly
  // during migration) is left untouched — the modern API below always works.
  const existingSsq = w.ssq;
  const pending: unknown[] = Array.isArray(existingSsq) ? existingSsq.slice() : [];
  if (existingSsq === undefined || Array.isArray(existingSsq)) {
    w.ssq = { push: exec };
  }
  const api = {
    open,
    close,
    toggle,
    hide,
    show,
    destroy,
    setLoginInfo,
    sendTextMessage,
    track,
    onUnread: onUnRead,
    isOpen: () => isOpen,
    getUnread: () => unread,
  };
  w.SmartChat = api;

  // ---- page view tracking -------------------------------------------------------
  let lastTracked = "";
  let pvTimer: ReturnType<typeof setTimeout> | undefined;
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
    if (!trackPages) return;
    // Debounced: SPAs that replaceState on every scroll (scrollspy) must not
    // turn into a POST /track per pixel.
    const fire = () => {
      clearTimeout(pvTimer);
      pvTimer = setTimeout(pageView, 500);
    };
    try {
      // Sites that freeze/seal `history` (anti-tamper) make these assignments
      // throw in strict mode — tracking degrades, the widget must live on.
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
      unhookHistory = () => {
        history.pushState = orig.pushState;
        history.replaceState = orig.replaceState;
        window.removeEventListener("popstate", fire);
        window.removeEventListener("hashchange", fire);
        clearTimeout(pvTimer);
      };
    } catch {
      /* frozen history — SPA navigations just won't be tracked */
    }
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
          // A mount-time exception must neither loop into the retry path
          // (duplicate widgets) nor propagate into the host page.
          try {
            mountUi(cfg);
            hookHistory();
            if (trackPages) pageView();
            for (const cmd of pending) exec(cmd);
            if (wantOpen) open();
          } catch {
            /* never break the merchant's page */
          }
        });
      })
      .catch(() => {
        // Network/HTTP failures only — mount errors are contained above.
        if (attempt < 2) setTimeout(() => fetchConfig(attempt + 1), 4000);
      });
  }

  function whenBody(fn: () => void): void {
    if (document.body) fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  }

  fetchConfig(0);
})();
