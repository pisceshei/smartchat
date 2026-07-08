/** messageFromPayload — the widget's realtime frame decoder. Visitor frames
 *  are whitelist-filtered by the gateway (flat id/content/text_plain/
 *  created_at/…, never message_id or the nested message), so the flat `id`
 *  path is what AI/agent replies arrive on. Locks the "AI reply dropped on
 *  the widget" regression. */
import { describe, expect, it } from "vitest";
import { messageFromPayload } from "../src/chat/controller";

const BASE = {
  id: "m1",
  conversation_id: "c1",
  sender_type: "ai_agent",
  content: { blocks: [{ kind: "text", text: "您好！" }] },
  text_plain: "您好！",
  created_at: "2026-07-09T12:00:01Z",
  delivery_status: "sent",
  client_msg_id: null,
};

describe("messageFromPayload", () => {
  it("builds a message from whitelisted flat fields (id + content + created_at)", () => {
    const m = messageFromPayload({ ...BASE });
    expect(m).toBeDefined();
    expect(m!.id).toBe("m1");
    expect(m!.content).toEqual(BASE.content);
    expect(m!.created_at).toBe("2026-07-09T12:00:01Z");
    expect(m!.sender_type).toBe("ai_agent");
  });

  it("synthesizes a text block from text_plain when content is missing", () => {
    const { content: _drop, ...rest } = BASE;
    const m = messageFromPayload(rest);
    expect(m).toBeDefined();
    expect(m!.content.blocks).toEqual([{ kind: "text", text: "您好！" }]);
  });

  it("returns undefined without any id (frame dropped, no crash)", () => {
    const { id: _drop, ...rest } = BASE;
    expect(messageFromPayload(rest)).toBeUndefined();
  });

  it("accepts message_id as a fallback id key", () => {
    const { id: _drop, ...rest } = BASE;
    const m = messageFromPayload({ ...rest, message_id: "m9" });
    expect(m?.id).toBe("m9");
  });

  it("drops internal notes", () => {
    expect(messageFromPayload({ ...BASE, is_note: true })).toBeUndefined();
  });

  it("prefers the nested message when present", () => {
    const nested = { ...BASE, id: "nested-id" };
    const m = messageFromPayload({ ...BASE, message: nested });
    expect(m?.id).toBe("nested-id");
  });
});
