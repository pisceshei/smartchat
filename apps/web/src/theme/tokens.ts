/**
 * SmartChat design system — single source of truth.
 *
 * Derived with ui-ux-pro-max (`--design-system`, density 8/10, "Data-Dense
 * Dashboard" style, professional navy + blue CTA family) and adapted to our
 * own brand palette. Light theme first; every color is exported both as an
 * AntD 5 theme token override and as a CSS variable (injected at bootstrap)
 * so plain CSS and AntD components share one palette.
 *
 * Rules encoded here (do not violate in components):
 * - Blue-primary professional SaaS back-office. No raw hex in components —
 *   use tokens / CSS vars.
 * - 4px spacing grid, dense (dashboard density): 4/8/12/16/24/32.
 * - Type scale: 12 / 13 / 14 (base) / 16 / 20 / 24. Line-height ≥ 1.5 body.
 * - Radius scale: 6 (controls) / 8 (cards) / 10 (modals/large surfaces).
 * - Two-layer soft shadows only; no heavy drop shadows.
 * - Text contrast ≥ 4.5:1 on light surfaces (slate-900/700 on white).
 * - SVG icons only (@ant-design/icons), never emoji-as-icon.
 */
import type { ThemeConfig } from "antd";

/* ---------------------------------------------------------------- palette */

export const brand = {
  50: "#EEF3FE",
  100: "#D9E4FD",
  200: "#B3C9FB",
  300: "#85A8F6",
  400: "#5A87EF",
  500: "#3D6CEB",
  600: "#2C5CE6", // primary
  700: "#1F44B0",
  800: "#173389",
  900: "#122A6E",
} as const;

export const slate = {
  50: "#F8FAFC",
  100: "#F1F5F9",
  200: "#E2E8F0",
  300: "#CBD5E1",
  400: "#94A3B8",
  500: "#64748B",
  600: "#475569",
  700: "#334155",
  800: "#1E293B",
  900: "#0F172A",
} as const;

export const semantic = {
  success: "#16A34A",
  successBg: "#EAF8F0",
  warning: "#D97706",
  warningBg: "#FEF6E7",
  error: "#DC2626",
  errorBg: "#FDECEC",
  info: brand[600],
  infoBg: brand[50],
} as const;

/** Dark navy icon rail (left navigation) — deliberately darker than content
 *  surfaces so the 8-module IA reads as app chrome, not page content. */
export const rail = {
  bg: "#0E1A3C",
  bgHover: "rgba(255,255,255,0.08)",
  bgActive: "rgba(61,108,235,0.32)",
  text: "#ADBBDD",
  textActive: "#FFFFFF",
  border: "rgba(255,255,255,0.08)",
  width: 64,
  widthExpanded: 220,
} as const;

export const fontFamily =
  "Inter, -apple-system, 'Segoe UI', Roboto, 'PingFang TC', 'Microsoft JhengHei', 'Noto Sans TC', 'Helvetica Neue', Arial, sans-serif";

export const fontFamilyMono =
  "'JetBrains Mono', 'Fira Code', Consolas, 'Courier New', monospace";

/* --------------------------------------------------------- css variables */

export const cssVars: Record<string, string> = {
  "--sc-primary": brand[600],
  "--sc-primary-hover": brand[500],
  "--sc-primary-active": brand[700],
  "--sc-primary-bg": brand[50],
  "--sc-primary-bg-strong": brand[100],
  "--sc-bg-layout": "#F4F6FA",
  "--sc-bg-container": "#FFFFFF",
  "--sc-bg-subtle": slate[50],
  "--sc-border": slate[200],
  "--sc-border-strong": slate[300],
  "--sc-text": slate[800],
  "--sc-text-heading": slate[900],
  "--sc-text-secondary": slate[500],
  "--sc-text-tertiary": slate[400],
  "--sc-success": semantic.success,
  "--sc-success-bg": semantic.successBg,
  "--sc-warning": semantic.warning,
  "--sc-warning-bg": semantic.warningBg,
  "--sc-error": semantic.error,
  "--sc-error-bg": semantic.errorBg,
  "--sc-rail-bg": rail.bg,
  "--sc-rail-text": rail.text,
  "--sc-rail-text-active": rail.textActive,
  "--sc-rail-hover": rail.bgHover,
  "--sc-rail-active": rail.bgActive,
  "--sc-bubble-in": "#FFFFFF",
  "--sc-bubble-out": brand[50],
  "--sc-bubble-note": "#FEF9E7",
  "--sc-bubble-note-border": "#F5E1A4",
  "--sc-shadow-sm": "0 1px 2px rgba(15,23,42,0.06), 0 1px 3px rgba(15,23,42,0.08)",
  "--sc-shadow-md": "0 4px 12px rgba(15,23,42,0.08), 0 2px 4px rgba(15,23,42,0.05)",
  "--sc-shadow-lg": "0 12px 32px rgba(15,23,42,0.12), 0 4px 8px rgba(15,23,42,0.06)",
  "--sc-radius": "8px",
  "--sc-radius-sm": "6px",
  "--sc-radius-lg": "10px",
  "--sc-font": fontFamily,
  "--sc-font-mono": fontFamilyMono,
};

export function injectCssVars(): void {
  const root = document.documentElement;
  for (const [k, v] of Object.entries(cssVars)) root.style.setProperty(k, v);
}

/* ------------------------------------------------------------ antd theme */

export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: brand[600],
    colorInfo: brand[600],
    colorSuccess: semantic.success,
    colorWarning: semantic.warning,
    colorError: semantic.error,
    colorLink: brand[600],
    colorTextBase: slate[800],
    colorText: slate[800],
    colorTextHeading: slate[900],
    colorTextSecondary: slate[500],
    colorTextTertiary: slate[400],
    colorTextQuaternary: slate[300],
    colorBorder: slate[200],
    colorBorderSecondary: slate[100],
    colorBgLayout: "#F4F6FA",
    colorBgContainer: "#FFFFFF",
    colorBgElevated: "#FFFFFF",
    colorFillSecondary: slate[100],
    colorFillTertiary: slate[50],
    fontFamily,
    fontSize: 14,
    fontSizeSM: 12,
    fontSizeLG: 16,
    fontSizeHeading1: 24,
    fontSizeHeading2: 20,
    fontSizeHeading3: 16,
    borderRadius: 8,
    borderRadiusSM: 6,
    borderRadiusLG: 10,
    controlHeight: 32,
    controlHeightSM: 26,
    controlHeightLG: 40,
    lineHeight: 1.572,
    motionDurationMid: "0.2s",
    motionDurationSlow: "0.3s",
    boxShadow: cssVars["--sc-shadow-md"],
    boxShadowSecondary: cssVars["--sc-shadow-lg"],
    wireframe: false,
  },
  components: {
    Layout: {
      headerBg: "#FFFFFF",
      headerHeight: 52,
      headerPadding: "0 16px",
      siderBg: rail.bg,
      bodyBg: "#F4F6FA",
    },
    Menu: {
      itemBorderRadius: 6,
      itemMarginInline: 8,
      itemHeight: 36,
      subMenuItemBg: "transparent",
    },
    Button: {
      fontWeight: 500,
      primaryShadow: "0 2px 4px rgba(44,92,230,0.24)",
    },
    Table: {
      headerBg: slate[50],
      headerColor: slate[600],
      headerSplitColor: "transparent",
      cellPaddingBlock: 10,
      cellPaddingInline: 12,
      rowHoverBg: brand[50],
    },
    Tabs: {
      titleFontSize: 14,
      horizontalItemPadding: "10px 4px",
      horizontalMargin: "0 0 12px 0",
    },
    Card: {
      paddingLG: 20,
      headerFontSize: 15,
      headerHeight: 48,
    },
    Modal: { titleFontSize: 16 },
    Drawer: { footerPaddingBlock: 12 },
    Badge: { fontSizeSM: 11 },
    Segmented: {
      itemSelectedBg: "#FFFFFF",
      trackBg: slate[100],
    },
    Tag: { defaultBg: slate[50] },
    Tooltip: { fontSize: 12 },
    Collapse: {
      headerPadding: "10px 12px",
      contentPadding: "4px 12px 12px",
      headerBg: "transparent",
    },
    Form: {
      labelColor: slate[600],
      verticalLabelPadding: "0 0 4px",
      itemMarginBottom: 18,
    },
    Input: { paddingBlock: 5 },
    Select: { optionSelectedBg: brand[50] },
  },
};

/** Channel brand accent colors (our own approximations, used for icon chips
 *  and status accents — official logo assets are NOT bundled). */
export const channelColors: Record<string, string> = {
  widget: brand[600],
  whatsapp_app: "#22B357",
  whatsapp_api: "#128C7E",
  messenger: "#0A7CFF",
  instagram: "#D6336C",
  telegram_app: "#2AA5DA",
  telegram_bot: "#2AA5DA",
  email: slate[500],
  line_app: "#06C755",
  line_oa: "#06C755",
  wechat: "#07C160",
  wechat_kf: "#07C160",
  wecom: "#0082EF",
  tiktok_app: "#111111",
  tiktok_business: "#111111",
  youtube: "#E52D27",
  zalo_app: "#0068FF",
  slack: "#611F69",
  vk: "#0077FF",
};
