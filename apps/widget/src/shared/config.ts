/**
 * Widget bootstrap config — shape returned by
 * GET {apiBase}/api/v1/widget/bootstrap?key={widget_key}.
 * All fields tolerant to absence; the widget renders with defaults.
 */

/** Per-language text map, e.g. { "en": "...", "zh-Hant": "..." }. */
export type LocalizedText = Record<string, string> | string | null | undefined;

export interface PreChatFieldOption {
  value: string;
  label: LocalizedText;
}

export interface PreChatField {
  key: string;
  type: "text" | "email" | "phone" | "textarea" | "select";
  label: LocalizedText;
  placeholder?: LocalizedText;
  required?: boolean;
  options?: PreChatFieldOption[];
}

export interface WidgetBootstrap {
  widget_key: string;
  brand?: {
    name?: string | null;
    avatar_url?: string | null;
    welcome_text?: LocalizedText;
  } | null;
  appearance?: {
    position?: "right" | "left";
    primary_color?: string | null;
    offset_x?: number;
    offset_y?: number;
    /** false only for plans with brand removal (>= Pro). */
    show_branding?: boolean;
    launcher_text?: LocalizedText;
  } | null;
  /** Default UI language when auto-detect has no match ("en" | "zh-Hant" | "zh-CN"). */
  locale_default?: string | null;
  home?: {
    enabled?: boolean;
    banners?: { image_url: string; link_url?: string }[];
    reply_hint?: string;
  } | null;
  pre_chat?: {
    enabled?: boolean;
    /** When true the visitor must submit the form before chatting. */
    required_before_chat?: boolean;
    fields?: PreChatField[];
  } | null;
  offline?: {
    is_online?: boolean;
    /** Show an email field so offline visitors can be replied to by mail. */
    email_fallback?: boolean;
    notice?: LocalizedText;
  } | null;
  features?: {
    file_upload?: boolean;
    emoji?: boolean;
  } | null;
}

export const DEFAULT_PRIMARY = "#4F46E5";

export function localized(t: LocalizedText, lang: string): string {
  if (!t) return "";
  if (typeof t === "string") return t;
  return t[lang] ?? t["en"] ?? Object.values(t)[0] ?? "";
}
