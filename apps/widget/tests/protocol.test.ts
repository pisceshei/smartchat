import { describe, expect, it } from "vitest";

import { localized } from "../src/shared/config";
import { unwrap, wrap } from "../src/shared/protocol";

describe("bridge envelope", () => {
  it("round-trips messages", () => {
    const msg = { t: "unread" as const, count: 3 };
    const env = wrap(msg);
    expect(env.__sc).toBe(1);
    expect(unwrap(env)).toEqual(msg);
  });

  it("rejects unrelated postMessage traffic", () => {
    expect(unwrap(null)).toBeNull();
    expect(unwrap("string")).toBeNull();
    expect(unwrap({})).toBeNull();
    expect(unwrap({ __sc: 2, msg: {} })).toBeNull();
    expect(unwrap({ __sc: 1 })).toBeNull();
    expect(unwrap({ __sc: 1, msg: "not-an-object" })).toBeNull();
  });
});

describe("localized", () => {
  it("plain strings pass through", () => {
    expect(localized("hello", "en")).toBe("hello");
  });

  it("selects requested language, falls back to en, then any", () => {
    expect(localized({ en: "Hi", "zh-Hant": "您好" }, "zh-Hant")).toBe("您好");
    expect(localized({ en: "Hi" }, "zh-Hant")).toBe("Hi");
    expect(localized({ fr: "Salut" }, "zh-Hant")).toBe("Salut");
    expect(localized(null, "en")).toBe("");
    expect(localized(undefined, "en")).toBe("");
  });
});
