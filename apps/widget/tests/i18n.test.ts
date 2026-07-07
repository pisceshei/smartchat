import { describe, expect, it } from "vitest";

import { detectLang, getLang, setLang, t } from "../src/chat/i18n";

describe("detectLang", () => {
  it("maps any zh-* to zh-Hant", () => {
    expect(detectLang("zh-TW")).toBe("zh-Hant");
    expect(detectLang("zh-HK")).toBe("zh-Hant");
    expect(detectLang("zh-CN")).toBe("zh-Hant");
    expect(detectLang("zh-Hant")).toBe("zh-Hant");
  });

  it("maps everything else to en", () => {
    expect(detectLang("en-US")).toBe("en");
    expect(detectLang("ja-JP")).toBe("en");
    expect(detectLang("fr")).toBe("en");
  });
});

describe("t", () => {
  it("returns per-language strings and falls back to en, then the key", () => {
    setLang("zh-Hant");
    expect(getLang()).toBe("zh-Hant");
    expect(t("online")).toBe("在線");
    setLang("en");
    expect(t("online")).toBe("Online");
    expect(t("__nope__")).toBe("__nope__");
  });
});
