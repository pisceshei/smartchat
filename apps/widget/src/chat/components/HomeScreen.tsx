import { useState } from "preact/hooks";

import { localized, type SocialEntry, type WidgetBootstrap } from "../../shared/config";
import { goChat, goHome, requestClose } from "../controller";
import { t } from "../i18n";
import { SocialIcon } from "./SocialIcons";

function openBanner(url?: string): void {
  if (url) window.open(url, "_blank", "noopener");
}

/** "Contact us on <channel>" row — one icon button per connected channel.
 * kind="link" opens the deep link; kind="copy" copies the handle (WeChat etc.)
 * and flashes a confirmation. */
function SocialRow(props: { entries: SocialEntry[] }) {
  const [copied, setCopied] = useState<string | null>(null);
  const activate = (e: SocialEntry) => {
    if (e.kind === "link" && e.url) {
      window.open(e.url, "_blank", "noopener");
    } else if (e.kind === "copy" && e.value) {
      // only flash "Copied" once the write actually resolves — an iframe without
      // clipboard-write permission (or http) must not show a false confirmation
      const p = navigator.clipboard?.writeText(e.value);
      p?.then(() => {
        setCopied(e.channel_type);
        setTimeout(() => setCopied(null), 1600);
      }).catch(() => {
        /* clipboard blocked — leave the label unchanged */
      });
    }
  };
  return (
    <div class="sc-social">
      <div class="sc-social-title">{t("home_contact_via")}</div>
      <div class="sc-social-row">
        {props.entries.map((e) => (
          <button
            key={e.channel_type + ":" + (e.url || e.value || "")}
            type="button"
            class="sc-social-item"
            aria-label={e.label}
            title={e.kind === "copy" ? e.value : e.label}
            onClick={() => activate(e)}
          >
            <span class="sc-social-ic">
              <SocialIcon icon={e.icon_key} />
            </span>
            <span class="sc-social-label">
              {copied === e.channel_type ? t("home_copied") : e.label}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

export function HomeScreen(props: { config: WidgetBootstrap; lang: string }) {
  const { config, lang } = props;
  const brand = config.brand || {};
  const home = config.home || {};
  const isOnline = config.offline?.is_online !== false;
  const welcome = localized(brand.welcome_text, lang) || t("welcome_default");
  const social =
    config.social?.enabled === false ? [] : config.social?.channels || [];

  return (
    <div class="sc-home">
      <div class="sc-home-hero">
        <button
          class="sc-icon-btn sc-home-close"
          type="button"
          aria-label={t("close")}
          onClick={requestClose}
        >
          <svg viewBox="0 0 24 24" width="20" height="20">
            <path
              d="M5 12h14"
              stroke="currentColor"
              stroke-width="2.2"
              stroke-linecap="round"
            />
          </svg>
        </button>
        {brand.avatar_url ? (
          <img class="sc-home-avatar" src={brand.avatar_url} alt="" />
        ) : (
          <div class="sc-home-avatar sc-home-avatar-fallback">
            {(brand.name || "S").slice(0, 1).toUpperCase()}
          </div>
        )}
        <div class="sc-home-title">{brand.name || "SmartChat"}</div>
        <div class="sc-home-welcome">{welcome}</div>
        <div class="sc-home-status">
          <span class={"sc-dot " + (isOnline ? "on" : "off")} />
          {isOnline ? t("home_online_hint") : t("home_offline_hint")}
        </div>
      </div>
      <div class="sc-home-body">
        {(home.banners || []).map((b) =>
          b.image_url ? (
            <div
              class={"sc-home-banner" + (b.link_url ? " clickable" : "")}
              role={b.link_url ? "link" : undefined}
              onClick={() => openBanner(b.link_url)}
            >
              <img src={b.image_url} alt="" loading="lazy" />
            </div>
          ) : null,
        )}
        <button type="button" class="sc-home-newconv" onClick={goChat}>
          <div class="sc-home-newconv-text">
            <div class="sc-home-newconv-title">{t("home_new_conversation")}</div>
            <div class="sc-home-newconv-hint">{home.reply_hint || t("home_reply_hint")}</div>
          </div>
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
            <path
              d="M4.5 12 20 4.5 15 20l-3.2-5.3L4.5 12Z"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
            />
            <path
              d="m11.8 14.7 3.5-3.5"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
            />
          </svg>
        </button>
        {social.length > 0 ? <SocialRow entries={social} /> : null}
      </div>
    </div>
  );
}

/** Bottom Home/Chat tab bar — rendered on both views when home is enabled. */
export function BottomNav(props: { view: "home" | "chat"; unread: number }) {
  return (
    <nav class="sc-nav">
      <button
        type="button"
        class={"sc-nav-tab" + (props.view === "home" ? " active" : "")}
        onClick={goHome}
      >
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
          <path
            d="M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-4.5v-5h-5v5H5a1 1 0 0 1-1-1v-9.5Z"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linejoin="round"
          />
        </svg>
        {t("home_tab_home")}
      </button>
      <button
        type="button"
        class={"sc-nav-tab" + (props.view === "chat" ? " active" : "")}
        onClick={goChat}
      >
        <span class="sc-nav-ic">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none">
            <path
              d="M12 4c-4.4 0-8 3-8 6.8 0 1.9.9 3.6 2.4 4.8-.1.9-.5 1.9-1.2 2.7 1.5 0 2.8-.5 3.8-1.2 1 .3 2 .5 3 .5 4.4 0 8-3 8-6.8S16.4 4 12 4Z"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
            />
          </svg>
          {props.unread > 0 ? (
            <span class="sc-nav-badge">{props.unread > 9 ? "9+" : props.unread}</span>
          ) : null}
        </span>
        {t("home_tab_chat")}
      </button>
    </nav>
  );
}
