import { describe, expect, it } from "vitest";

import { detectLang, getLang, setLang, t } from "../src/chat/i18n";
import { mapLangTag } from "../src/shared/config";

describe("detectLang", () => {
  it("maps traditional zh variants to zh-Hant", () => {
    expect(detectLang("zh-TW")).toBe("zh-Hant");
    expect(detectLang("zh-HK")).toBe("zh-Hant");
    expect(detectLang("zh-Hant")).toBe("zh-Hant");
    expect(detectLang("zh")).toBe("zh-Hant");
  });

  it("maps simplified zh variants to zh-CN", () => {
    expect(detectLang("zh-CN")).toBe("zh-CN");
    expect(detectLang("zh-SG")).toBe("zh-CN");
    expect(detectLang("zh-Hans")).toBe("zh-CN");
    expect(detectLang("zh-Hans-CN")).toBe("zh-CN");
  });

  it("maps bare CMS locale codes (Fecify <html lang>) correctly", () => {
    // Fecify stores set lang="tw"/"cn"/"hk" — a plain zh-prefix check misread
    // these as English and the widget spoke English on a 繁中 shop.
    expect(detectLang("tw")).toBe("zh-Hant");
    expect(detectLang("hk")).toBe("zh-Hant");
    expect(detectLang("mo")).toBe("zh-Hant");
    expect(detectLang("cht")).toBe("zh-Hant");
    expect(detectLang("cn")).toBe("zh-CN");
    expect(detectLang("sg")).toBe("zh-CN");
    expect(detectLang("chs")).toBe("zh-CN");
  });

  it("maps everything else to en", () => {
    expect(detectLang("en-US")).toBe("en");
    expect(detectLang("ja-JP")).toBe("en");
    expect(detectLang("fr")).toBe("en");
    // "tw"/"cn" must match as WHOLE tokens only — not as prefixes of other tags
    expect(detectLang("twi")).toBe("en"); // Twi (Akan)
    expect(mapLangTag("cnr")).toBe("en"); // Montenegrin
  });
});

describe("mapLangTag", () => {
  it("is null/empty safe", () => {
    expect(mapLangTag(null)).toBe("en");
    expect(mapLangTag(undefined)).toBe("en");
    expect(mapLangTag("")).toBe("en");
    expect(mapLangTag("  ")).toBe("en");
  });

  it("handles case and underscore separators", () => {
    expect(mapLangTag("TW")).toBe("zh-Hant");
    expect(mapLangTag("zh_CN")).toBe("zh-CN");
    expect(mapLangTag("ZH_TW")).toBe("zh-Hant");
    expect(mapLangTag("hk-HK")).toBe("zh-Hant");
  });
});

describe("t", () => {
  it("returns per-language strings and falls back to en, then the key", () => {
    setLang("zh-Hant");
    expect(getLang()).toBe("zh-Hant");
    expect(t("online")).toBe("在線");
    setLang("zh-CN");
    expect(t("online")).toBe("在线");
    expect(t("home_new_conversation")).toBe("新对话");
    setLang("en");
    expect(t("online")).toBe("Online");
    expect(t("__nope__")).toBe("__nope__");
  });
});
