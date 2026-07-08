/** Channel gallery catalog — 19 channel types from the plan's integration
 *  matrix. `connectable` marks P1 channels with working connect modals. */
import type { ChannelType } from "@/api/types";
import type { I18nKey } from "@/i18n";

export interface ChannelMeta {
  type: ChannelType;
  name: string;
  descKey: I18nKey;
  connectable: boolean;
  beta?: boolean;
  /** Short 1–2 char glyph for channels without a bundled vector icon. */
  glyph?: string;
}

export const CHANNEL_CATALOG: ChannelMeta[] = [
  { type: "widget", name: "聊天外掛", descKey: "int.channel.desc.widget", connectable: true },
  { type: "whatsapp_api", name: "WhatsApp API", descKey: "int.channel.desc.whatsapp_api", connectable: true },
  { type: "whatsapp_app", name: "WhatsApp App", descKey: "int.channel.desc.whatsapp_app", connectable: true, beta: true },
  { type: "messenger", name: "Messenger", descKey: "int.channel.desc.messenger", connectable: true },
  { type: "instagram", name: "Instagram", descKey: "int.channel.desc.instagram", connectable: true },
  { type: "telegram_bot", name: "Telegram Bot", descKey: "int.channel.desc.telegram_bot", connectable: true },
  { type: "telegram_app", name: "Telegram App", descKey: "int.channel.desc.generic", connectable: false },
  { type: "email", name: "Email", descKey: "int.channel.desc.email", connectable: true },
  { type: "line_oa", name: "LINE 官方帳號", descKey: "int.channel.desc.line_oa", connectable: true, glyph: "LN" },
  { type: "line_app", name: "LINE App", descKey: "int.channel.desc.line_app", connectable: false, glyph: "LN" },
  { type: "youtube", name: "YouTube 評論", descKey: "int.channel.desc.youtube", connectable: true },
  { type: "tiktok_app", name: "TikTok App", descKey: "int.channel.desc.generic", connectable: false, glyph: "TT" },
  { type: "tiktok_business", name: "TikTok 商業號", descKey: "int.channel.desc.tiktok_business", connectable: true, glyph: "TT" },
  { type: "wechat_kf", name: "微信客服", descKey: "int.channel.desc.wechat_kf", connectable: true },
  { type: "wecom", name: "企業微信", descKey: "int.channel.desc.wecom", connectable: true },
  { type: "wechat", name: "微信", descKey: "int.channel.desc.generic", connectable: false },
  { type: "zalo_app", name: "Zalo OA", descKey: "int.channel.desc.zalo_app", connectable: true, glyph: "Za" },
  { type: "slack", name: "Slack", descKey: "int.channel.desc.slack", connectable: true },
  { type: "vk", name: "VKontakte", descKey: "int.channel.desc.vk", connectable: true, glyph: "VK" },
];

export const CHANNEL_NAME: Record<string, string> = Object.fromEntries(
  CHANNEL_CATALOG.map((c) => [c.type, c.name]),
);

/** Translate-target language options for the inbox translate toggle. */
export const TRANSLATE_LANGS: { value: string; label: string }[] = [
  { value: "zh-TW", label: "繁體中文" },
  { value: "zh-CN", label: "简体中文" },
  { value: "en", label: "English" },
  { value: "ja", label: "日本語" },
  { value: "ko", label: "한국어" },
  { value: "th", label: "ไทย" },
  { value: "vi", label: "Tiếng Việt" },
  { value: "id", label: "Bahasa Indonesia" },
  { value: "ms", label: "Bahasa Melayu" },
  { value: "es", label: "Español" },
  { value: "pt", label: "Português" },
  { value: "fr", label: "Français" },
  { value: "de", label: "Deutsch" },
  { value: "ar", label: "العربية" },
  { value: "ru", label: "Русский" },
];

export const TAG_COLORS = [
  "#2C5CE6",
  "#16A34A",
  "#D97706",
  "#DC2626",
  "#7C3AED",
  "#0891B2",
  "#DB2777",
  "#4D7C0F",
  "#9333EA",
  "#64748B",
];
