/** react-query hooks for the inbox module (list, messages, mutations with
 *  optimistic updates keyed by client_msg_id). */
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { inboxApi, type InboxListFilter, type InboxTab } from "@/api/endpoints";
import type {
  Conversation,
  CursorPage,
  Message,
  MessageContent,
} from "@/api/types";
import { newClientId } from "@/utils/id";
import { useAuthStore } from "@/stores/auth";

export interface ConvListParams {
  tab: InboxTab;
  viewId?: string;
  q?: string;
  filter: InboxListFilter;
}

export function useInboxSummary() {
  return useQuery({
    queryKey: ["inbox-summary"],
    queryFn: () => inboxApi.summary(),
    refetchInterval: 60_000,
    retry: 1,
  });
}

export function useInboxViews() {
  return useQuery({
    queryKey: ["inbox-views"],
    queryFn: () => inboxApi.listViews(),
    retry: 1,
  });
}

export function useConversations(params: ConvListParams) {
  return useInfiniteQuery({
    queryKey: ["conversations", params],
    queryFn: ({ pageParam }) =>
      inboxApi.listConversations({
        tab: params.viewId ? undefined : params.tab,
        view_id: params.viewId,
        q: params.q || undefined,
        filter: params.filter,
        cursor: pageParam || undefined,
        limit: 30,
      }),
    initialPageParam: "",
    getNextPageParam: (last: CursorPage<Conversation>) => last.next_cursor ?? undefined,
    retry: 1,
  });
}

export function useConversation(id?: string) {
  return useQuery({
    queryKey: ["conversation", id],
    queryFn: () => inboxApi.getConversation(id!),
    enabled: !!id,
    retry: 1,
  });
}

export function useMessages(conversationId?: string) {
  return useInfiniteQuery({
    queryKey: ["messages", conversationId],
    queryFn: ({ pageParam }) =>
      inboxApi.listMessages(conversationId!, {
        before: pageParam || undefined,
        limit: 40,
      }),
    initialPageParam: "",
    getNextPageParam: (last: CursorPage<Message>) => last.next_cursor ?? undefined,
    enabled: !!conversationId,
    retry: 1,
  });
}

type MsgPages = { pages: CursorPage<Message>[]; pageParams: unknown[] };

export function useSendMessage(conversationId: string) {
  const qc = useQueryClient();
  const user = useAuthStore((s) => s.user);

  return useMutation({
    mutationFn: (vars: { content: MessageContent; isNote: boolean; clientMsgId: string }) =>
      inboxApi.sendMessage(conversationId, {
        client_msg_id: vars.clientMsgId,
        content: vars.content,
        is_note: vars.isNote,
      }),
    onMutate: async (vars) => {
      const key = ["messages", conversationId];
      await qc.cancelQueries({ queryKey: key });
      const optimistic: Message = {
        id: `optimistic-${vars.clientMsgId}`,
        workspace_id: "",
        conversation_id: conversationId,
        direction: "out",
        sender_type: "member",
        sender_name: user?.name ?? null,
        msg_type: "text",
        content: vars.content,
        text_plain: vars.content.blocks
          .map((b) => ("text" in b ? (b as { text: string }).text : ""))
          .join("\n"),
        is_note: vars.isNote,
        delivery_status: "pending",
        client_msg_id: vars.clientMsgId,
        created_at: new Date().toISOString(),
      };
      qc.setQueryData<MsgPages>(key, (old) => {
        if (!old) {
          return { pages: [{ items: [optimistic], next_cursor: null }], pageParams: [""] };
        }
        const pages = [...old.pages];
        pages[0] = { ...pages[0], items: [...pages[0].items, optimistic] };
        return { ...old, pages };
      });
      return { clientMsgId: vars.clientMsgId };
    },
    onSuccess: (serverMsg, vars) => {
      const key = ["messages", conversationId];
      qc.setQueryData<MsgPages>(key, (old) => {
        if (!old) return old;
        const pages = old.pages.map((p) => ({
          ...p,
          items: p.items.map((m) => (m.client_msg_id === vars.clientMsgId ? serverMsg : m)),
        }));
        return { ...old, pages };
      });
    },
    onError: (_err, vars) => {
      const key = ["messages", conversationId];
      qc.setQueryData<MsgPages>(key, (old) => {
        if (!old) return old;
        const pages = old.pages.map((p) => ({
          ...p,
          items: p.items.map((m) =>
            m.client_msg_id === vars.clientMsgId
              ? { ...m, delivery_status: "failed" as const }
              : m,
          ),
        }));
        return { ...old, pages };
      });
    },
  });
}

type ConvPages = { pages: CursorPage<Conversation>[]; pageParams: unknown[] };

export function useUpdateConversation(conversationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Parameters<typeof inboxApi.updateConversation>[1]) =>
      inboxApi.updateConversation(conversationId, body),
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: ["conversation", conversationId] });
      const prev = qc.getQueryData<Conversation>(["conversation", conversationId]);
      if (prev) {
        qc.setQueryData<Conversation>(["conversation", conversationId], {
          ...prev,
          ...(body.status ? { status: body.status } : {}),
          ...(body.bot_managed !== undefined ? { bot_managed: body.bot_managed } : {}),
          ...(body.translate !== undefined ? { translate: body.translate } : {}),
        });
      }
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["conversation", conversationId], ctx.prev);
    },
    onSuccess: (conv) => {
      qc.setQueryData(["conversation", conversationId], conv);
      qc.setQueriesData<ConvPages>({ queryKey: ["conversations"] }, (data) => {
        if (!data) return data;
        return {
          ...data,
          pages: data.pages.map((p) => ({
            ...p,
            items: p.items.map((c) => (c.id === conv.id ? { ...c, ...conv } : c)),
          })),
        };
      });
      void qc.invalidateQueries({ queryKey: ["inbox-summary"] });
    },
  });
}

export function useMarkRead(conversationId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => inboxApi.markRead(conversationId),
    onMutate: () => {
      qc.setQueriesData<ConvPages>({ queryKey: ["conversations"] }, (data) => {
        if (!data) return data;
        return {
          ...data,
          pages: data.pages.map((p) => ({
            ...p,
            items: p.items.map((c) =>
              c.id === conversationId ? { ...c, agent_unread_count: 0 } : c,
            ),
          })),
        };
      });
    },
  });
}

export { newClientId };
