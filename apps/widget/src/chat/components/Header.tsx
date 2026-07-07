import type { WidgetBootstrap } from "../../shared/config";
import { requestClose } from "../controller";
import { t } from "../i18n";
import type { ConnState } from "../store";

export function Header(props: { config: WidgetBootstrap; conn: ConnState }) {
  const { config, conn } = props;
  const brand = config.brand || {};
  const isOnline = config.offline?.is_online !== false;
  const status =
    conn === "connecting" || conn === "boot"
      ? t("connecting")
      : conn === "reconnecting"
        ? t("reconnecting")
        : isOnline
          ? t("online")
          : t("offline");
  const dotClass = conn === "online" && isOnline ? "on" : conn === "online" ? "off" : "conn";

  return (
    <header class="sc-header">
      <div class="sc-header-brand">
        {brand.avatar_url ? (
          <img class="sc-avatar" src={brand.avatar_url} alt="" />
        ) : (
          <div class="sc-avatar sc-avatar-fallback">
            {(brand.name || "S").slice(0, 1).toUpperCase()}
          </div>
        )}
        <div class="sc-header-text">
          <div class="sc-brand-name">{brand.name || "SmartChat"}</div>
          <div class="sc-status">
            <span class={"sc-dot " + dotClass} />
            {status}
          </div>
        </div>
      </div>
      <button
        class="sc-icon-btn"
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
    </header>
  );
}
