/** Regression locks for realtime cache application. The three historical
 *  failure modes these pin down:
 *  - "empty bubble": a message.created without content fabricated a
 *    blocks:[] row and skipped the refetch fallback
 *  - "tick wipe": a delivery-status message.updated rebuilt a full row and
 *    blanked the cached body
 *  - "frozen inbox": unusable payloads silently dropped instead of
 *    invalidating
 */
import { describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import type { Conversation, CursorPage, Message, WsEnvelope } from "@/api/types";
import { applyEvent } from "./applyEvent";

const CONV = "c1";
const MSG_KEY = ["messages", CONV];

function seededClient(messages: Partial<Message>[] = []): QueryClient {
  const qc = new QueryClient();
  qc.setQueryData(MSG_KEY, {
    pages: [{ items: messages as Message[], next_cursor: null }],
    pageParams: [null],
  });
  return qc;
}

function threadMessages(qc: QueryClient): Message[] {
  const data = qc.getQueryData<{ pages: CursorPage<Message>[] }>(MSG_KEY);
  return data?.pages.flatMap((p) => p.items) ?? [];
}

function evt(type: string, payload: Record<string, unknown>): WsEnvelope {
  return { type, ts: "2026-07-09T12:00:00Z", conversation_id: CONV, payload } as WsEnvelope;
}

const FULL_ROW = {
  id: "m2",
  conversation_id: CONV,
  direction: "out",
  sender_type: "ai_agent",
  msg_type: "text",
  content: { blocks: [{ kind: "text", text: "您好！" }, { kind: "product_card", title: "薰衣草蠟燭" }] },
  text_plain: "您好！",
  is_note: false,
  client_msg_id: null,
  delivery_status: "pending",
  created_at: "2026-07-09T12:00:01Z",
};

describe("message.created", () => {
  it("appends the full body from the nested message (content.blocks intact)", () => {
    const qc = seededClient([]);
    applyEvent(qc, evt("message.created", { message_id: "m2", message: FULL_ROW }));
    const msgs = threadMessages(qc);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toEqual(FULL_ROW.content);
  });

  it("synthesizes a text block from text_plain when content is stripped, and refetches", () => {
    const qc = seededClient([]);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    applyEvent(
      qc,
      evt("message.created", {
        message_id: "m2",
        conversation_id: CONV,
        sender_type: "ai_agent",
        text_plain: "您好！",
        delivery_status: "pending",
        created_at: "2026-07-09T12:00:01Z",
      }),
    );
    const msgs = threadMessages(qc);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content.blocks).toEqual([{ kind: "text", text: "您好！" }]);
    expect(msgs[0].created_at).toBe("2026-07-09T12:00:01Z");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MSG_KEY });
  });

  it("falls back to invalidateQueries on an unusable payload — never freezes, never inserts an empty bubble", () => {
    const qc = seededClient([]);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    // no content, no text_plain — the pre-fix code fabricated blocks:[]
    applyEvent(qc, evt("message.created", { message_id: "m2", sender_type: "ai_agent" }));
    expect(threadMessages(qc)).toHaveLength(0);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MSG_KEY });
    for (const m of threadMessages(qc)) {
      expect(m.content.blocks.length).toBeGreaterThan(0);
    }
  });

  it("does not mutate agent_unread_count in conversation lists (round-7 guard)", () => {
    const qc = seededClient([]);
    qc.setQueryData(["conversations", { tab: "ai" }], {
      pages: [
        {
          items: [
            { id: CONV, agent_unread_count: 3, snippet: "old", needs_reply: false } as unknown as Conversation,
          ],
          next_cursor: null,
        },
      ],
      pageParams: [null],
    });
    applyEvent(qc, evt("message.created", { message_id: "m2", message: FULL_ROW }));
    const lists = qc.getQueryData<{ pages: CursorPage<Conversation>[] }>([
      "conversations",
      { tab: "ai" },
    ]);
    expect(lists?.pages[0].items[0].agent_unread_count).toBe(3);
    expect(lists?.pages[0].items[0].snippet).toBe("您好！");
  });
});

describe("message.updated", () => {
  it("patches only the delivery tick, preserving content and created_at (tick-wipe lock)", () => {
    const qc = seededClient([{ ...FULL_ROW } as unknown as Message]);
    applyEvent(
      qc,
      evt("message.updated", {
        message_id: "m2",
        id: "m2",
        conversation_id: CONV,
        delivery_status: "read",
        external_message_id: "3EB0X",
      }),
    );
    const [m] = threadMessages(qc);
    expect(m.delivery_status).toBe("read");
    expect(m.content).toEqual(FULL_ROW.content); // body untouched
    expect(m.created_at).toBe(FULL_ROW.created_at); // no reorder
  });

  it("patches translations without clobbering content (translate-wipe lock)", () => {
    const qc = seededClient([{ ...FULL_ROW } as unknown as Message]);
    applyEvent(
      qc,
      evt("message.updated", {
        message_id: "m2",
        conversation_id: CONV,
        translations: { en: "Hello!" },
      }),
    );
    const [m] = threadMessages(qc);
    expect((m as unknown as Record<string, unknown>)["translations"]).toEqual({ en: "Hello!" });
    expect(m.content).toEqual(FULL_ROW.content);
  });

  it("invalidates on an empty patch — a gateway-slimmed translate/content push is never silently dropped", () => {
    // the gateway strips content/translations off message.updated for
    // non-open conversations (and the SPA never sends focus frames), so a
    // translate push arrives as {message_id} only — it must trigger a refetch
    const qc = seededClient([{ ...FULL_ROW } as unknown as Message]);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    applyEvent(
      qc,
      evt("message.updated", { message_id: "m2", conversation_id: CONV }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MSG_KEY });
    expect(threadMessages(qc)[0].content).toEqual(FULL_ROW.content); // untouched
  });

  it("invalidates the thread when the target message is not cached", () => {
    const qc = seededClient([]); // cache exists but row missing
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    applyEvent(
      qc,
      evt("message.updated", {
        message_id: "missing",
        conversation_id: CONV,
        delivery_status: "sent",
      }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MSG_KEY });
  });

  it("invalidates the thread for id-less bulk updates (read watermark)", () => {
    const qc = seededClient([{ ...FULL_ROW } as unknown as Message]);
    const invalidate = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, evt("message.updated", { read_watermark: "2026-07-09T12:01:00Z" }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: MSG_KEY });
    // and the cached body was not touched
    expect(threadMessages(qc)[0].content).toEqual(FULL_ROW.content);
  });
});
