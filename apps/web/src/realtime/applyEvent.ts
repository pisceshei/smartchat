/** Applies realtime events to react-query caches so the inbox stays live
 *  without refetch storms. Unknown/miss cases degrade to invalidation. */
import type { QueryClient } from "@tanstack/react-query";
import type { Conversation, CursorPage, Message, WsEnvelope } from "@/api/types";
import { useRealtimeStore } from "@/stores/realtime";

type ConvPages = { pages: CursorPage<Conversation>[]; pageParams: unknown[] };
type MsgPages = { pages: CursorPage<Message>[]; pageParams: unknown[] };

function upsertMessage(qc: QueryClient, msg: Message): void {
  const key = ["messages", msg.conversation_id];
  const existing = qc.getQueryData<MsgPages>(key);
  if (!existing) return;
  let replaced = false;
  const pages = existing.pages.map((page) => {
    const items = page.items.map((m) => {
      if (m.id === msg.id || (msg.client_msg_id && m.client_msg_id === msg.client_msg_id)) {
        replaced = true;
        return { ...m, ...msg };
      }
      return m;
    });
    return { ...page, items };
  });
  if (!replaced && pages.length > 0) {
    // newest page is index 0 (fetched with no cursor); append there
    pages[0] = { ...pages[0], items: [...pages[0].items, msg] };
  }
  qc.setQueryData<MsgPages>(key, { ...existing, pages });
}

function patchConversationLists(
  qc: QueryClient,
  conversationId: string,
  patch: Partial<Conversation> | ((c: Conversation) => Conversation),
  bumpToTop = false,
): boolean {
  let found = false;
  qc.setQueriesData<ConvPages>({ queryKey: ["conversations"] }, (data) => {
    if (!data) return data;
    const pages = data.pages.map((page, pi) => {
      let items = page.items.map((c) => {
        if (c.id !== conversationId) return c;
        found = true;
        return typeof patch === "function" ? patch(c) : { ...c, ...patch };
      });
      if (bumpToTop && pi === 0 && found) {
        const idx = items.findIndex((c) => c.id === conversationId);
        if (idx > 0) {
          const [conv] = items.splice(idx, 1);
          items = [conv, ...items];
        }
      }
      return { ...page, items };
    });
    return { ...data, pages };
  });
  // detail cache too
  const detail = qc.getQueryData<Conversation>(["conversation", conversationId]);
  if (detail) {
    qc.setQueryData<Conversation>(
      ["conversation", conversationId],
      typeof patch === "function" ? patch(detail) : { ...detail, ...patch },
    );
  }
  return found;
}

/** Prefer the nested full row; otherwise rebuild a usable Message from the
 *  flat event fields (message_id/direction/content/...). `complete: false`
 *  marks a body synthesized from text_plain (the gateway slims top-level
 *  content off frames for non-open conversations) — the caller must refetch
 *  so rich blocks (product cards, media) reconcile. A payload with NO usable
 *  body returns undefined — NEVER a fabricated empty-blocks message: that is
 *  exactly the "empty bubble until refresh" bug. Payload-shape drift must
 *  degrade to invalidation, never to a silently frozen inbox. */
function messageFromEvent(evt: WsEnvelope): { msg: Message; complete: boolean } | undefined {
  const payload = evt.payload ?? {};
  const nested = payload["message"] as Message | undefined;
  if (nested?.id && nested.conversation_id) return { msg: nested, complete: true };
  const id = (payload["message_id"] ?? payload["id"]) as string | undefined;
  const conversationId =
    (payload["conversation_id"] as string | undefined) ?? evt.conversation_id ?? undefined;
  if (!id || !conversationId) return undefined;
  let content = payload["content"] as Message["content"] | undefined;
  let complete = true;
  if (!content) {
    const text = payload["text_plain"];
    if (typeof text !== "string" || !text) return undefined;
    content = { blocks: [{ kind: "text", text }] } as Message["content"];
    complete = false;
  }
  const msg = {
    id,
    conversation_id: conversationId,
    channel_identity_id: (payload["channel_identity_id"] as string | null) ?? null,
    direction: (payload["direction"] as string) ?? "out",
    sender_type: (payload["sender_type"] as string) ?? "",
    sender_id: (payload["sender_id"] as string | null) ?? null,
    msg_type: (payload["msg_type"] as string) ?? "text",
    content,
    text_plain: (payload["text_plain"] as string | null) ?? null,
    is_note: payload["is_note"] === true,
    client_msg_id: (payload["client_msg_id"] as string | null) ?? null,
    delivery_status: (payload["delivery_status"] as string | null) ?? null,
    created_at:
      (payload["created_at"] as string) ?? (evt.ts as string) ?? new Date().toISOString(),
  } as unknown as Message;
  return { msg, complete };
}

/** Merge a partial patch into ONE cached message. Returns false only when the
 *  thread cache exists but the row is missing (caller should invalidate); an
 *  uncached thread returns true — the REST fetch on open is authoritative. */
function patchMessage(
  qc: QueryClient,
  conversationId: string,
  messageId: string,
  patch: Partial<Message>,
): boolean {
  const key = ["messages", conversationId];
  const existing = qc.getQueryData<MsgPages>(key);
  if (!existing) return true;
  let found = false;
  const pages = existing.pages.map((page) => ({
    ...page,
    items: page.items.map((m) => {
      if (m.id !== messageId) return m;
      found = true;
      return { ...m, ...patch };
    }),
  }));
  if (found) qc.setQueryData<MsgPages>(key, { ...existing, pages });
  return found;
}

export function applyEvent(qc: QueryClient, evt: WsEnvelope): void {
  const rt = useRealtimeStore.getState();
  const payload = evt.payload ?? {};

  switch (evt.type) {
    case "message.created": {
      const result = messageFromEvent(evt);
      if (!result) {
        // shape we don't understand — refetch rather than freeze
        void qc.invalidateQueries({ queryKey: ["conversations"] });
        if (evt.conversation_id) {
          void qc.invalidateQueries({ queryKey: ["messages", evt.conversation_id] });
        }
        void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
        break;
      }
      const { msg, complete } = result;
      upsertMessage(qc, msg);
      if (!complete) {
        // body was synthesized from text_plain — refetch so rich blocks
        // (product cards, media) replace the plain-text stand-in
        void qc.invalidateQueries({ queryKey: ["messages", msg.conversation_id] });
      }
      // NOTE: agent_unread_count is deliberately NOT touched here — the
      // conversation.updated event in the same batch carries the server's
      // authoritative count. A local +1 on top of it double-counts and makes
      // the badge visibly jitter on every message.
      const inList = patchConversationLists(
        qc,
        msg.conversation_id,
        (c) => ({
          ...c,
          snippet: msg.text_plain ?? c.snippet,
          last_message_at: msg.created_at,
          needs_reply: msg.direction === "in" ? true : msg.is_note ? c.needs_reply : false,
        }),
        true,
      );
      if (!inList) {
        void qc.invalidateQueries({ queryKey: ["conversations"] });
      }
      void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
      break;
    }

    case "message.updated": {
      // PATCH-ONLY: delivery-status ticks and translate updates carry partial
      // payloads. Rebuilding a full row here (the old behavior) spread an
      // empty-blocks body over the cached message and blanked the bubble on
      // every tick advance. Apply only the keys that are present.
      const nested = payload["message"] as Message | undefined;
      if (nested?.id && nested.conversation_id) {
        upsertMessage(qc, nested);
        break;
      }
      const msgId = (payload["message_id"] ?? payload["id"]) as string | undefined;
      const convId =
        (payload["conversation_id"] as string | undefined) ?? evt.conversation_id ?? undefined;
      if (!msgId || !convId) {
        // e.g. bulk read-watermark updates carry no message_id — refetch
        if (evt.conversation_id) {
          void qc.invalidateQueries({ queryKey: ["messages", evt.conversation_id] });
        }
        break;
      }
      const patch: Partial<Message> = {};
      const patchable = [
        "delivery_status", "content", "text_plain", "translations", "msg_type",
      ] as const;
      for (const k of patchable) {
        if (k in payload) (patch as Record<string, unknown>)[k] = payload[k];
      }
      if (Object.keys(patch).length === 0) {
        // an update frame with no renderable keys still signals a change the
        // gateway slimmed away (content/translations are stripped for
        // non-open conversations, and the SPA never sends focus frames) —
        // refetch rather than silently dropping it. Delivery ticks always
        // carry delivery_status, so they never take this branch.
        void qc.invalidateQueries({ queryKey: ["messages", convId] });
        break;
      }
      if (!patchMessage(qc, convId, msgId, patch)) {
        void qc.invalidateQueries({ queryKey: ["messages", convId] }); // miss → refetch, never freeze
      }
      break;
    }

    case "conversation.created": {
      void qc.invalidateQueries({ queryKey: ["conversations"] });
      void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
      break;
    }

    case "conversation.updated":
    case "conversation.assigned":
    case "conversation.resolved":
    case "conversation.reopened": {
      // Backend conversation events carry FLAT fields (status/handler/snippet/
      // needs_reply/...), not a nested `conversation` object — accept both.
      const conv = payload["conversation"] as Conversation | undefined;
      const convId =
        conv?.id ??
        ((payload["conversation_id"] as string | undefined) || evt.conversation_id) ??
        undefined;
      if (convId) {
        let patch: Partial<Conversation>;
        if (conv) {
          patch = conv;
        } else {
          patch = {};
          const fields = [
            "status", "handler", "assignee_member_id", "needs_reply",
            "agent_unread_count", "snippet", "bot_managed", "ai_state",
            "translation", "last_message_at",
          ] as const;
          for (const k of fields) {
            if (k in payload) (patch as Record<string, unknown>)[k] = payload[k];
          }
        }
        const found = patchConversationLists(qc, convId, (c) => ({ ...c, ...patch }));
        if (!found) {
          void qc.invalidateQueries({ queryKey: ["conversations"] });
          void qc.invalidateQueries({ queryKey: ["conversation", convId] });
        }
      } else {
        void qc.invalidateQueries({ queryKey: ["conversations"] });
      }
      void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
      break;
    }

    case "unread.changed": {
      // Per-MEMBER realtime counter (rt_unread hash) — a different number from
      // the conversation-level agent_unread_count shown on the list badge.
      // Writing it into the badge made two counters fight over one field
      // (visible flicker), so it only refreshes the sidebar totals now; the
      // badge follows conversation.updated exclusively.
      void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
      break;
    }

    case "typing": {
      const conversationId = (payload["conversation_id"] as string) ?? evt.conversation_id;
      if (conversationId) rt.setTyping(conversationId);
      break;
    }

    case "presence.member": {
      const memberId = payload["member_id"] as string;
      if (memberId) rt.setMemberPresence(memberId, payload["online"] === true);
      break;
    }

    case "presence.visitor": {
      const contactId = (payload["contact_id"] as string) ?? evt.contact_id;
      if (contactId) rt.setVisitorPresence(contactId, payload["online"] === true);
      break;
    }

    case "contact.updated":
    case "contact.merged": {
      const contactId = (payload["contact_id"] as string) ?? evt.contact_id;
      if (contactId) void qc.invalidateQueries({ queryKey: ["contact", contactId] });
      void qc.invalidateQueries({ queryKey: ["contacts"] });
      break;
    }

    case "channel.status": {
      void qc.invalidateQueries({ queryKey: ["channel-accounts"] });
      break;
    }

    default:
      break;
  }
}
