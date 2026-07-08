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

export function applyEvent(qc: QueryClient, evt: WsEnvelope): void {
  const rt = useRealtimeStore.getState();
  const payload = evt.payload ?? {};

  switch (evt.type) {
    case "message.created": {
      const msg = payload["message"] as Message | undefined;
      if (!msg) break;
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
      const msg = payload["message"] as Message | undefined;
      if (msg) upsertMessage(qc, msg);
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
      const conv = payload["conversation"] as Conversation | undefined;
      if (conv) {
        const found = patchConversationLists(qc, conv.id, conv);
        if (!found) void qc.invalidateQueries({ queryKey: ["conversations"] });
        qc.setQueryData<Conversation>(["conversation", conv.id], (old) =>
          old ? { ...old, ...conv } : conv,
        );
      } else if (evt.conversation_id) {
        void qc.invalidateQueries({ queryKey: ["conversation", evt.conversation_id] });
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
