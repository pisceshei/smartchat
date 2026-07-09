/** Pure form/preview logic for the widget config editor (WidgetConfigPage).
 *  Extracted so the pieces that caused the "儲存壞了" incident are unit-tested:
 *  - previewHtml must render SAVED values, not "SmartChat" defaults
 *  - fully-empty Form.List rows must never block the save
 *  - a validation failure must name the tab + field, and the page must jump there
 */
import type { WidgetBannerItem, WidgetConfigJson, WidgetPrechatField } from "@/api/types";
import { t, type I18nKey } from "@/i18n";

export interface FormShape {
  name: string;
  brand_name: string;
  welcome_text: string;
  avatar_url: string;
  position: "right" | "left";
  primary_color: string;
  launcher_text: string;
  remove_branding: boolean;
  home_enabled: boolean;
  banners: WidgetBannerItem[];
  reply_hint: string;
  prechat_enabled: boolean;
  prechat_required: boolean;
  prechat_fields: WidgetPrechatField[];
  ai_agent_id: string | null;
  member_ids: string[];
  domains_text: string;
}

// which tab each form field lives on — 儲存 must be able to JUMP to a failed
// field's tab; a silently rejected validateFields with the error hidden on
// another tab reads as "儲存壞了" (it did, in production).
export const FIELD_TAB: Record<string, string> = {
  name: "brand",
  brand_name: "brand",
  welcome_text: "brand",
  avatar_url: "brand",
  position: "appearance",
  primary_color: "appearance",
  launcher_text: "appearance",
  remove_branding: "appearance",
  home_enabled: "home",
  banners: "home",
  reply_hint: "home",
  prechat_enabled: "prechat",
  prechat_required: "prechat",
  prechat_fields: "prechat",
  ai_agent_id: "routing",
  member_ids: "routing",
  domains_text: "domains",
};

const TAB_LABEL: Record<string, I18nKey> = {
  brand: "widget.config.brand",
  appearance: "widget.config.appearance",
  home: "widget.config.home",
  prechat: "widget.config.prechat",
  routing: "widget.config.routing",
  domains: "widget.config.domains",
};

const FIELD_LABEL: Record<string, I18nKey> = {
  name: "widget.name",
  brand_name: "widget.config.brandName",
  welcome_text: "widget.config.greeting",
  avatar_url: "widget.config.avatarUrl",
  position: "widget.config.position",
  primary_color: "widget.config.color",
  launcher_text: "widget.config.launcherText",
  remove_branding: "widget.config.removeBranding",
  home_enabled: "widget.config.homeEnabled",
  banners: "widget.config.banners",
  reply_hint: "widget.config.replyHint",
  prechat_enabled: "widget.config.prechatEnabled",
  prechat_required: "widget.config.prechatRequired",
  prechat_fields: "widget.config.prechatFields",
  ai_agent_id: "widget.config.aiAgent",
  member_ids: "widget.config.assignMembers",
  domains_text: "widget.config.allowedDomains",
};

const SUB_FIELD_LABEL: Record<string, I18nKey> = {
  "banners.image_url": "widget.config.bannerImage",
  "banners.link_url": "widget.config.bannerLink",
  "prechat_fields.key": "widget.config.fieldKey",
  "prechat_fields.label": "widget.config.fieldLabel",
};

export function toForm(
  name: string,
  cfg: WidgetConfigJson,
  brandRemoved: boolean,
  domains: string[],
): FormShape {
  return {
    name,
    brand_name: cfg.brand?.name ?? "",
    welcome_text: cfg.brand?.welcome_text ?? "",
    avatar_url: cfg.brand?.avatar_url ?? "",
    position: cfg.appearance?.position === "left" ? "left" : "right",
    primary_color: cfg.appearance?.primary_color ?? "#2C5CE6",
    launcher_text: cfg.appearance?.launcher_text ?? "",
    remove_branding: brandRemoved,
    home_enabled: cfg.home?.enabled ?? false,
    banners: cfg.home?.banners ?? [],
    reply_hint: cfg.home?.reply_hint ?? "",
    prechat_enabled: cfg.pre_chat?.enabled ?? false,
    prechat_required: cfg.pre_chat?.required_before_chat ?? false,
    prechat_fields: cfg.pre_chat?.fields ?? [],
    ai_agent_id: cfg.routing?.ai_agent_id ?? null,
    member_ids: cfg.routing?.member_ids ?? [],
    domains_text: domains.join("\n"),
  };
}

/** Full merged nested config for PATCH — preserves keys the editor doesn't
 *  surface (offline/features/strategy). */
export function toConfig(cfg: WidgetConfigJson, v: FormShape): WidgetConfigJson {
  return {
    ...cfg,
    brand: {
      ...cfg.brand,
      name: v.brand_name || undefined,
      welcome_text: v.welcome_text || undefined,
      avatar_url: v.avatar_url || undefined,
    },
    appearance: {
      ...cfg.appearance,
      position: v.position,
      primary_color: typeof v.primary_color === "string" ? v.primary_color : "#2C5CE6",
      launcher_text: v.launcher_text || undefined,
    },
    home: {
      ...cfg.home,
      enabled: v.home_enabled,
      banners: (v.banners ?? []).filter((b) => b?.image_url),
      reply_hint: v.reply_hint || undefined,
    },
    pre_chat: {
      ...cfg.pre_chat,
      enabled: v.prechat_enabled,
      required_before_chat: v.prechat_required,
      fields: (v.prechat_fields ?? []).filter((f) => f?.key && f?.label),
    },
    offline: cfg.offline,
    routing: {
      ...cfg.routing,
      ai_agent_id: v.ai_agent_id ?? null,
      member_ids: v.member_ids ?? [],
    },
    features: cfg.features,
  };
}

const blank = (s?: string | null) => !s || !String(s).trim();

/** Fully-empty Form.List rows carry no intent — drop them silently before
 *  validation (toConfig discards them at save time anyway; validating them
 *  first was the "點儲存完全沒反應" trap: a required-field reject with the row
 *  hidden on another tab). Partially-filled rows are KEPT so validation
 *  produces a named error instead of silently losing input. */
export function pruneEmptyRows(v: FormShape): FormShape {
  return {
    ...v,
    banners: (v.banners ?? []).filter((b) => !(blank(b?.image_url) && blank(b?.link_url))),
    prechat_fields: (v.prechat_fields ?? []).filter((f) => !(blank(f?.key) && blank(f?.label))),
  };
}

export interface ErrorFieldInfo {
  name: (string | number)[];
  errors: string[];
}

/** Human-readable description of the FIRST validation failure: which tab to
 *  jump to, which field to scroll to, and a toast naming tab/row/field. */
export function describeValidationError(fields: ErrorFieldInfo[] | undefined): {
  tab?: string;
  name?: (string | number)[];
  text: string;
} {
  const first = fields?.[0];
  if (!first) return { text: t("common.operationFailed") };
  const top = String(first.name[0]);
  const tab = FIELD_TAB[top];
  let field = FIELD_LABEL[top] ? t(FIELD_LABEL[top]) : top;
  if (first.name.length >= 3) {
    const subKey = SUB_FIELD_LABEL[`${top}.${String(first.name[2])}`];
    if (subKey) {
      field = t("widget.config.errorListRow", {
        list: field,
        n: Number(first.name[1]) + 1,
        sub: t(subKey),
      });
    }
  }
  return {
    tab,
    name: first.name,
    text: t("widget.config.errorToast", {
      tab: tab ? t(TAB_LABEL[tab]) : "",
      field,
      error: first.errors?.[0] ?? t("common.required"),
    }),
  };
}

export function previewHtml(v: Partial<FormShape>): string {
  // escape for double-quoted HTML attributes AND element text — a `"` in a
  // banner URL / prechat label would otherwise break out of the attribute
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  // primary_color is interpolated into <style>/inline style — only allow a hex
  // literal so a crafted string can't close the tag and inject markup
  const rawColor = typeof v.primary_color === "string" ? v.primary_color : "";
  const color = /^#[0-9a-fA-F]{3,8}$/.test(rawColor) ? rawColor : "#2C5CE6";
  const pos = v.position === "left" ? "left" : "right";
  const greeting = esc(v.welcome_text || "您好！有什麼可以幫到您？");
  const brand = esc(v.brand_name || "SmartChat");
  const banners = v.home_enabled
    ? (v.banners ?? [])
        .filter((b) => b?.image_url)
        .slice(0, 3)
        .map((b) => `<div class="bn"><img src="${esc(b.image_url)}" alt="" /></div>`)
        .join("")
    : "";
  const replyHint = v.home_enabled && v.reply_hint ? `<div class="hint">${esc(v.reply_hint)}</div>` : "";
  const prechat = v.prechat_enabled
    ? `<div class="pre">${(v.prechat_fields ?? [])
        .filter((f) => f?.label)
        .map((f) =>
          f.type === "textarea"
            ? `<textarea placeholder="${esc(f.label)}"></textarea>`
            : `<input placeholder="${esc(f.label)}" />`,
        )
        .join("")}<button>開始對話</button></div>`
    : "";
  return `<!doctype html><html><head><meta charset="utf-8"><style>
  body{margin:0;font-family:-apple-system,'Segoe UI','PingFang TC','Microsoft JhengHei',sans-serif;background:#e9edf3;height:100vh;overflow:hidden}
  .page{padding:14px;color:#9aa4b5;font-size:12px}
  .panel{position:absolute;bottom:84px;${pos}:16px;width:280px;height:340px;background:#fff;border-radius:14px;box-shadow:0 12px 32px rgba(15,23,42,.18);display:flex;flex-direction:column;overflow:hidden}
  .hd{background:${color};color:#fff;padding:12px 14px;font-weight:600;font-size:14px}
  .hd small{display:block;font-weight:400;opacity:.85;font-size:11px}
  .body{flex:1;padding:12px;background:#f6f8fb;overflow:auto}
  .msg{background:#fff;border-radius:10px;border-top-left-radius:3px;padding:8px 10px;font-size:12.5px;max-width:80%;box-shadow:0 1px 2px rgba(15,23,42,.08)}
  .bn{margin-top:8px;border-radius:10px;overflow:hidden;background:#dfe4ec}
  .bn img{display:block;width:100%;height:64px;object-fit:cover}
  .hint{margin-top:8px;font-size:11px;color:#8a94a6;text-align:center}
  .in{border:none;border-top:1px solid #e5e9f0;padding:10px 12px;font-size:12px;color:#9aa4b5;background:#fff}
  .fab{position:absolute;bottom:20px;${pos}:16px;width:52px;height:52px;border-radius:50%;background:${color};box-shadow:0 8px 20px rgba(15,23,42,.25);display:flex;align-items:center;justify-content:center}
  .fab svg{width:26px;height:26px;fill:#fff}
  .brand{font-size:10px;color:#b3bcc9;text-align:center;padding:4px}
  .pre{padding:10px 0 0;display:flex;flex-direction:column;gap:6px}
  .pre input,.pre textarea{border:1px solid #dfe4ec;border-radius:8px;padding:7px 9px;font-size:12px;font-family:inherit;resize:none}
  .pre button{background:${color};color:#fff;border:none;border-radius:8px;padding:8px;font-size:12.5px;margin-top:2px}
  </style></head><body>
  <div class="page">www.example.com</div>
  <div class="panel">
    <div class="hd">${brand}<small>線上客服</small></div>
    <div class="body"><div class="msg">${greeting}</div>${banners}${replyHint}${prechat}</div>
    <div class="in">輸入訊息…</div>
    ${v.remove_branding ? "" : '<div class="brand">Powered by SmartChat</div>'}
  </div>
  <div class="fab"><svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.6 3.8c-.5.4-1.4.1-1.4-.6V5.5Z"/></svg></div>
  </body></html>`;
}
