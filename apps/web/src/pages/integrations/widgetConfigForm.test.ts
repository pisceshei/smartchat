/** Locks the widget-config editor logic behind the "點儲存完全沒反應" incident:
 *  stale preview defaults, invisible empty-row validation traps, and
 *  context-free error toasts. */
import { describe, expect, it } from "vitest";
import type { WidgetConfigJson } from "@/api/types";
import {
  describeValidationError,
  type FormShape,
  previewHtml,
  pruneEmptyRows,
  toConfig,
  toForm,
} from "./widgetConfigForm";

const SAVED_CFG: WidgetConfigJson = {
  brand: { name: "CHILL LOVE", welcome_text: "歡迎光臨" },
  appearance: { position: "right", primary_color: "#123456" },
  home: { enabled: true, banners: [{ image_url: "https://img/a.jpg" }] },
  pre_chat: { enabled: false, fields: [] },
  offline: { auto_reply: "off-hours" },
  features: { file_upload: false },
} as unknown as WidgetConfigJson;

function savedForm(): FormShape {
  return toForm("CHILL LOVE", SAVED_CFG, false, ["www.chill.love"]);
}

describe("previewHtml", () => {
  it("renders SAVED brand/greeting from toForm — not the SmartChat defaults", () => {
    const html = previewHtml(savedForm());
    expect(html).toContain("CHILL LOVE");
    expect(html).toContain("歡迎光臨");
    expect(html).not.toContain(">SmartChat<");
    expect(html).toContain("#123456");
  });

  it("falls back to defaults only for an EMPTY seed", () => {
    const html = previewHtml({});
    expect(html).toContain("SmartChat");
    expect(html).toContain("您好！有什麼可以幫到您？");
  });
});

describe("pruneEmptyRows", () => {
  it("drops fully-empty banner and prechat rows", () => {
    const v = savedForm();
    v.banners = [{ image_url: "" }, { image_url: "https://img/b.jpg" }] as FormShape["banners"];
    v.prechat_fields = [
      { key: "", type: "text", label: "" },
      { key: "email", type: "email", label: "電郵" },
    ] as FormShape["prechat_fields"];
    const p = pruneEmptyRows(v);
    expect(p.banners).toHaveLength(1);
    expect(p.banners[0].image_url).toBe("https://img/b.jpg");
    expect(p.prechat_fields).toHaveLength(1);
    expect(p.prechat_fields[0].key).toBe("email");
  });

  it("KEEPS partially-filled rows so validation can name them", () => {
    const v = savedForm();
    v.banners = [{ image_url: "", link_url: "https://chill.love" }] as FormShape["banners"];
    v.prechat_fields = [{ key: "", type: "text", label: "姓名" }] as FormShape["prechat_fields"];
    const p = pruneEmptyRows(v);
    expect(p.banners).toHaveLength(1);
    expect(p.prechat_fields).toHaveLength(1);
  });
});

describe("describeValidationError", () => {
  it("names the tab, list, row and sub-field for a Form.List error", () => {
    const d = describeValidationError([
      { name: ["banners", 0, "image_url"], errors: ["此欄位為必填"] },
    ]);
    expect(d.tab).toBe("home");
    expect(d.name).toEqual(["banners", 0, "image_url"]);
    expect(d.text).toContain("首頁模式");
    expect(d.text).toContain("廣告位");
    expect(d.text).toContain("第 1 列");
    expect(d.text).toContain("圖片網址");
    expect(d.text).toContain("此欄位為必填");
  });

  it("maps a top-level field to its tab", () => {
    const d = describeValidationError([{ name: ["name"], errors: ["必填"] }]);
    expect(d.tab).toBe("brand");
    expect(d.text).toContain("品牌");
  });

  it("degrades gracefully with no error fields", () => {
    expect(describeValidationError(undefined).text.length).toBeGreaterThan(0);
    expect(describeValidationError([]).tab).toBeUndefined();
  });
});

describe("toConfig", () => {
  it("preserves unsurfaced offline/features keys (merge, not replace)", () => {
    const out = toConfig(SAVED_CFG, savedForm());
    expect(out.offline).toEqual({ auto_reply: "off-hours" });
    expect(out.features).toEqual({ file_upload: false });
  });
});
