/** Chat iframe URL contract. Pointing the iframe at a path the edge doesn't
 *  route (/chat/…) fell through to the admin-SPA catch-all and rendered the
 *  LOGIN page inside the widget panel — lock the canonical /widget-app path. */
import { describe, expect, it } from "vitest";
import { buildChatIframeUrl, CHAT_APP_PATH } from "../src/loader/chatUrl";

const BASE = "https://chat.chilling.com.hk";

describe("buildChatIframeUrl", () => {
  it("points at /widget-app/index.html, never the legacy /chat/ path", () => {
    const u = buildChatIframeUrl(BASE, "cb7a196a5d9306f5", "https://www.chill.love", "zh-HK");
    expect(u.pathname).toBe("/widget-app/index.html");
    expect(u.toString()).not.toContain("/chat/");
    expect(CHAT_APP_PATH).toBe("/widget-app/index.html");
  });

  it("round-trips k/po/lang query params exactly", () => {
    const u = buildChatIframeUrl(BASE, "cb7a196a5d9306f5", "https://www.chill.love", "zh-HK");
    expect(u.searchParams.get("k")).toBe("cb7a196a5d9306f5");
    expect(u.searchParams.get("po")).toBe("https://www.chill.love");
    expect(u.searchParams.get("lang")).toBe("zh-HK");
  });

  it("keeps the asset base origin (the chatOrigin postMessage contract)", () => {
    expect(buildChatIframeUrl(BASE, "k1", "https://shop.example", "en").origin).toBe(BASE);
  });

  it("works with a ported dev base", () => {
    const u = buildChatIframeUrl("http://localhost:8788", "demo1234", "http://localhost:8788", "en");
    expect(u.origin).toBe("http://localhost:8788");
    expect(u.pathname).toBe("/widget-app/index.html");
  });
});
