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
 *  flat event fields (message_id/direction/content/...). Payload-shape drift
 *  must degrade to invalidation, never to a silently frozen inbox. */
function messageFromEvent(evt: WsEnvelope): Message | undefined {
  const payload = evt.payload ?? {};
  const nested = payload["message"] as Message | undefined;
  if (nested?.id && nested.conversation_id) return nested;
  const id = payload["message_id"] as string | undefined;
  const conversationId =
    (payload["conversation_id"] as string | undefined) ?? evt.conversation_id ?? undefined;
  if (!id || !conversationId) return undefined;
  return {
    id,
    conversation_id: conversationId,
    channel_identity_id: (payload["channel_identity_id"] as string | null) ?? null,
    direction: (payload["direction"] as string) ?? "out",
    sender_type: (payload["sender_type"] as string) ?? "",
    sender_id: (payload["sender_id"] as string | null) ?? null,
    msg_type: (payload["msg_type"] as string) ?? "text",
    content: (payload["content"] as Message["content"]) ?? { blocks: [] },
    text_plain: (payload["text_plain"] as string | null) ?? null,
    is_note: payload["is_note"] === true,
    client_msg_id: (payload["client_msg_id"] as string | null) ?? null,
    delivery_status: (payload["delivery_status"] as string | null) ?? null,
    created_at: (evt.ts as string) ?? new Date().toISOString(),
  } as unknown as Message;
}

export function applyEvent(qc: QueryClient, evt: WsEnvelope): void {
  const rt = useRealtimeStore.getState();
  const payload = evt.payload ?? {};

  switch (evt.type) {
    case "message.created": {
      const msg = messageFromEvent(evt);
      if (!msg) {
        // shape we don't understand — refetch rather than freeze
        void qc.invalidateQueries({ queryKey: ["conversations"] });
        if (evt.conversation_id) {
          void qc.invalidateQueries({ queryKey: ["messages", evt.conversation_id] });
        }
        void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
        break;
      }
      upsertMessage(qc, msg);
      const inList = patchConversationLists(
        qc,
        msg.conversation_id,
        (c) => ({
          ...c,
          snippet: msg.text_plain ?? c.snippet,
          last_message_at: msg.created_at,
          needs_reply: msg.direction === "in" ? true : msg.is_note ? c.needs_reply : false,
          agent_unread_count:
            msg.direction === "in" ? c.agent_unread_count + 1 : c.agent_unread_count,
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
      const msg = messageFromEvent(evt);
      if (msg) upsertMessage(qc, msg);
      else if (evt.conversation_id) {
        void qc.invalidateQueries({ queryKey: ["messages", evt.conversation_id] });
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
      const conversationId = (payload["conversation_id"] as string) ?? evt.conversation_id;
      // Backend emits `unread.changed` with the key `count` (services/realtime
      // unread.py); keep `agent_unread_count` as a legacy fallback.
      const count = (payload["count"] ?? payload["agent_unread_count"]) as number | undefined;
      if (conversationId && typeof count === "number") {
        patchConversationLists(qc, conversationId, { agent_unread_count: count });
      }
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
