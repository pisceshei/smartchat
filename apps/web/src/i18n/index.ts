import { zhHant, type I18nKey } from "./zh-Hant";

export type { I18nKey };

const dictionaries: Record<string, Record<I18nKey, string>> = {
  "zh-Hant": zhHant,
};

let current = "zh-Hant";

export function setLocale(locale: string): void {
  if (dictionaries[locale]) current = locale;
}

/** Translate a key; `{param}` placeholders are interpolated. */
export function t(key: I18nKey, params?: Record<string, string | number>): string {
  let s: string = dictionaries[current][key] ?? key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      s = s.replaceAll(`{${k}}`, String(v));
    }
  }
  return s;
}
