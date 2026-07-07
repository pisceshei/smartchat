/** Generated-style typed endpoints matching the backend module routes
 *  (apps/api/app/modules/*). One namespace per backend module. */
import { http } from "./client";
import type {
  AuthOut,
  ApiTokenCreated,
  ApiTokenInfo,
  AuditEntry,
  AuthResponse,
  ChannelAccount,
  ChannelIdentity,
  ChannelType,
  Contact,
  ConversationSettings,
  Conversation,
  ConversationTranslateConfig,
  CursorPage,
  CustomFieldDef,
  FileRef,
  FilterPredicate,
  InboxSummary,
  InboxView,
  Member,
  MemberGroup,
  MemberShifts,
  MergeCandidate,
  Message,
  MessageContent,
  OrderBrief,
  Paginated,
  QuickReply,
  QuickReplyFolder,
  Role,
  Tag,
  User,
  WebhookConfig,
  WidgetConfig,
  WorkspaceBrief,
} from "./types";

/* ---------------------------------------------------------------- auth */

function normalizeAuth(o: AuthOut): AuthResponse {
  return {
    token: o.access_token,
    refreshToken: o.refresh_token,
    user: { id: o.user_id, email: o.email, name: o.display_name },
    workspaces: o.workspaces,
  };
}

export const authApi = {
  login: async (body: { email: string; password: string }): Promise<AuthResponse> =>
    normalizeAuth(await http<AuthOut>("POST", "/auth/login", { body })),
  register: async (body: {
    email: string;
    password: string;
    name: string;
    workspace_name: string;
  }): Promise<AuthResponse> =>
    normalizeAuth(await http<AuthOut>("POST", "/auth/register", { body })),
  me: () => http<User>("GET", "/auth/me"),
};

export const workspacesApi = {
  list: () => http<WorkspaceBrief[]>("GET", "/workspaces"),
};

/* ---------------------------------------------------------------- inbox */

export type InboxTab = "mine" | "bot" | "ai" | "unassigned" | "all" | "team";
export type InboxListFilter = "all" | "unread" | "needs_reply";

/** Backend returns each list item / detail as {conversation, contact, tags/tag_ids}
 * with a nested translation map; the UI wants one flat Conversation. */
function flattenConversation(row: {
  conversation: Record<string, unknown> & { id: string };
  contact?: unknown;
  tags?: Tag[];
  tag_ids?: string[];
}): Conversation {
  const conv = row.conversation as unknown as Conversation & {
    translation?: ConversationTranslateConfig;
  };
  return {
    ...conv,
    contact: (row.contact as Conversation["contact"]) ?? conv.contact ?? null,
    tags: row.tags ?? conv.tags,
    bot_managed: (conv as { ai_state?: string }).ai_state
      ? (conv as { ai_state?: string }).ai_state !== "off"
      : conv.bot_managed,
    translate: (conv.translation as ConversationTranslateConfig) ?? conv.translate ?? null,
  };
}

export const inboxApi = {
  summary: () => http<InboxSummary>("GET", "/inbox/unread-summary"),

  listConversations: async (params: {
    tab?: InboxTab;
    view_id?: string;
    q?: string;
    filter?: InboxListFilter;
    cursor?: string;
    limit?: number;
  }): Promise<CursorPage<Conversation>> => {
    const res = await http<{
      items: Parameters<typeof flattenConversation>[0][];
      next_cursor: string | null;
    }>("GET", "/inbox/conversations", { query: params });
    return { items: res.items.map(flattenConversation), next_cursor: res.next_cursor };
  },

  getConversation: async (id: string): Promise<Conversation> =>
    flattenConversation(
      await http<Parameters<typeof flattenConversation>[0]>(
        "GET",
        `/inbox/conversations/${id}`,
      ),
    ),

  listMessages: (conversationId: string, params: { before?: string; limit?: number } = {}) =>
    http<CursorPage<Message>>("GET", `/inbox/conversations/${conversationId}/messages`, {
      query: params,
    }),

  sendMessage: (
    conversationId: string,
    body: { client_msg_id: string; content: MessageContent; is_note?: boolean },
  ) => http<Message>("POST", `/inbox/conversations/${conversationId}/messages`, { body }),

  markRead: (conversationId: string) =>
    http<void>("POST", `/inbox/conversations/${conversationId}/read`),

  /** Dispatches to the backend's dedicated action routes (the backend has no
   * single PATCH — assign/close/reopen/tags/translation/managed are distinct
   * endpoints per plan A.5). One field per call from the UI. */
  updateConversation: async (
    conversationId: string,
    body: Partial<{
      status: "open" | "closed";
      assignee_member_id: string | null;
      bot_managed: boolean;
      translate: { enabled: boolean; agent_lang?: string | null; customer_lang?: string | null };
      tag_ids: string[];
    }>,
  ): Promise<Conversation> => {
    const base = `/inbox/conversations/${conversationId}`;
    if (body.status === "closed") return http<Conversation>("POST", `${base}/close`);
    if (body.status === "open") return http<Conversation>("POST", `${base}/reopen`);
    if (body.tag_ids !== undefined)
      return http<Conversation>("PUT", `${base}/tags`, { body: { tag_ids: body.tag_ids } });
    if (body.assignee_member_id !== undefined)
      return http<Conversation>("POST", `${base}/assign`, {
        body: { member_id: body.assignee_member_id },
      });
    if (body.bot_managed !== undefined)
      return http<Conversation>("PATCH", `${base}/managed`, { body: { managed: body.bot_managed } });
    if (body.translate !== undefined)
      return http<Conversation>("PATCH", `${base}/translation`, {
        body: {
          enabled: body.translate.enabled,
          agent_lang: body.translate.agent_lang ?? null,
          customer_lang: body.translate.customer_lang ?? null,
        },
      });
    return http<Conversation>("GET", base);
  },

  history: (conversationId: string) =>
    http<Conversation[]>("GET", `/inbox/conversations/${conversationId}/sessions`),

  listViews: () => http<InboxView[]>("GET", "/inbox/views"),
  createView: (body: Omit<InboxView, "id" | "created_at">) =>
    http<InboxView>("POST", "/inbox/views", { body }),
  deleteView: (id: string) => http<void>("DELETE", `/inbox/views/${id}`),
};

/* -------------------------------------------------------------- contacts */

export const contactsApi = {
  list: async (params: {
    page?: number;
    page_size?: number;
    q?: string;
    filters?: FilterPredicate[];
  }): Promise<Paginated<Contact>> => {
    const page = params.page ?? 1;
    const pageSize = params.page_size ?? 20;
    const res = await http<{ items: Contact[]; total: number; limit: number; offset: number }>(
      "POST",
      "/contacts/query",
      {
        body: {
          q: params.q || null,
          predicates: params.filters ?? [],
          logic: "and",
          limit: pageSize,
          offset: (page - 1) * pageSize,
        },
      },
    );
    return { items: res.items, total: res.total, page, page_size: pageSize };
  },
  /** Backend returns a 360 wrapper {contact, identities, tags, orders,
   * conversations, merge_history, ...}. Flatten it to the UI's Contact shape
   * (contact fields hoisted, identities/tags kept as arrays). */
  get: async (id: string): Promise<Contact> => {
    const d = await http<{
      contact: Record<string, unknown> & { id: string };
      identities?: ChannelIdentity[];
      tags?: Tag[];
    }>("GET", `/contacts/${id}`);
    return {
      ...(d.contact as unknown as Contact),
      one_id: d.contact.id as string,
      channel_identities: d.identities ?? [],
      tags: d.tags ?? [],
    };
  },
  update: (id: string, body: Partial<Contact>) =>
    http<Contact>("PATCH", `/contacts/${id}`, { body }),
  create: (body: { display_name: string; email?: string; phone?: string }) =>
    http<Contact>("POST", "/contacts", { body }),
  setTags: (id: string, tag_ids: string[]) =>
    http<Contact>("PATCH", `/contacts/${id}`, { body: { tag_ids } }),
  mergeCandidates: (id: string, status?: "suggested" | "linked" | "dismissed") =>
    http<MergeCandidate[]>("GET", `/contacts/${id}/merge-candidates`, {
      query: { status },
    }),
  merge: (candidateId: string) =>
    http<void>("POST", `/contacts/merge-candidates/${candidateId}/merge`),
  dismissMerge: (candidateId: string) =>
    http<void>("POST", `/contacts/merge-candidates/${candidateId}/dismiss`),
  activities: (id: string) => http<AuditEntry[]>("GET", `/contacts/${id}/activities`),
  orders: (id: string) => http<OrderBrief[]>("GET", `/contacts/${id}/orders`),
  export: (params: { q?: string; filters?: FilterPredicate[] }) =>
    http<{ task_id: string }>("POST", "/contacts/export", { body: params }),
  conversations: (id: string) => http<Conversation[]>("GET", `/contacts/${id}/conversations`),
};

/* ------------------------------------------------------------------ tags */

export const tagsApi = {
  list: (kind: "visitor" | "conversation") => http<Tag[]>("GET", "/tags", { query: { kind } }),
  create: (body: { kind: "visitor" | "conversation"; name: string; color: string }) =>
    http<Tag>("POST", "/tags", { body }),
  update: (id: string, body: { name?: string; color?: string }) =>
    http<Tag>("PATCH", `/tags/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/tags/${id}`),
};

/* ---------------------------------------------------------- quick replies */

export const quickRepliesApi = {
  folders: () => http<QuickReplyFolder[]>("GET", "/quick-replies/folders"),
  createFolder: (body: { name: string; visibility: "personal" | "public" }) =>
    http<QuickReplyFolder>("POST", "/quick-replies/folders", { body }),
  list: (params: { folder_id?: string; q?: string } = {}) =>
    http<QuickReply[]>("GET", "/quick-replies", { query: params }),
  create: (body: {
    title: string;
    content: string;
    folder_id?: string | null;
    visibility: "personal" | "public";
  }) => http<QuickReply>("POST", "/quick-replies", { body }),
  update: (id: string, body: Partial<QuickReply>) =>
    http<QuickReply>("PATCH", `/quick-replies/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/quick-replies/${id}`),
};

/* ---------------------------------------------------------- custom fields */

export const customFieldsApi = {
  list: () => http<CustomFieldDef[]>("GET", "/custom-fields"),
  create: (body: Omit<CustomFieldDef, "id" | "created_at">) =>
    http<CustomFieldDef>("POST", "/custom-fields", { body }),
  update: (id: string, body: Partial<CustomFieldDef>) =>
    http<CustomFieldDef>("PATCH", `/custom-fields/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/custom-fields/${id}`),
};

/* -------------------------------------------------------------- channels */

export const channelsApi = {
  listAccounts: () => http<ChannelAccount[]>("GET", "/channels/accounts"),
  connect: (channelType: ChannelType, body: Record<string, unknown>) =>
    http<ChannelAccount>("POST", `/channels/${channelType}/accounts`, { body }),
  removeAccount: (id: string) => http<void>("DELETE", `/channels/accounts/${id}`),
};

/* --------------------------------------------------------------- widgets */

export const widgetsApi = {
  list: () => http<WidgetConfig[]>("GET", "/widgets"),
  get: (id: string) => http<WidgetConfig>("GET", `/widgets/${id}`),
  create: (body: { name: string; domain?: string }) => http<WidgetConfig>("POST", "/widgets", { body }),
  update: (id: string, body: Partial<WidgetConfig>) =>
    http<WidgetConfig>("PATCH", `/widgets/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/widgets/${id}`),
};

/* ----------------------------------------------------------------- team */

export const membersApi = {
  list: () => http<Member[]>("GET", "/members"),
  invite: (body: { email: string; role_id?: string; group_ids?: string[] }) =>
    http<Member>("POST", "/members/invite", { body }),
  update: (
    id: string,
    body: Partial<{ role_id: string; group_ids: string[]; daily_cap: number; enabled: boolean }>,
  ) => http<Member>("PATCH", `/members/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/members/${id}`),
};

export const rolesApi = {
  list: () => http<Role[]>("GET", "/roles"),
  create: (body: { name: string; permissions: string[] }) => http<Role>("POST", "/roles", { body }),
  update: (id: string, body: { name?: string; permissions?: string[] }) =>
    http<Role>("PATCH", `/roles/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/roles/${id}`),
};

export const groupsApi = {
  list: () => http<MemberGroup[]>("GET", "/member-groups"),
  create: (body: { name: string; description?: string }) =>
    http<MemberGroup>("POST", "/member-groups", { body }),
  update: (id: string, body: { name?: string; description?: string }) =>
    http<MemberGroup>("PATCH", `/member-groups/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/member-groups/${id}`),
};

export const shiftsApi = {
  get: (memberId: string) => http<MemberShifts>("GET", `/members/${memberId}/shifts`),
  save: (memberId: string, body: MemberShifts) =>
    http<MemberShifts>("PUT", `/members/${memberId}/shifts`, { body }),
  getEnabled: () => http<{ enabled: boolean }>("GET", "/settings/shifts"),
  setEnabled: (enabled: boolean) =>
    http<{ enabled: boolean }>("PUT", "/settings/shifts", { body: { enabled } }),
};

/* -------------------------------------------------------------- settings */

export const settingsApi = {
  getConversation: () => http<ConversationSettings>("GET", "/settings/conversation"),
  saveConversation: (body: ConversationSettings) =>
    http<ConversationSettings>("PUT", "/settings/conversation", { body }),

  getApiToken: () => http<ApiTokenInfo>("GET", "/settings/developer/token"),
  createApiToken: () => http<ApiTokenCreated>("POST", "/settings/developer/token"),

  getWebhook: () => http<WebhookConfig>("GET", "/settings/developer/webhook"),
  saveWebhook: (body: WebhookConfig) =>
    http<WebhookConfig>("PUT", "/settings/developer/webhook", { body }),
  testWebhook: () => http<{ ok: boolean }>("POST", "/settings/developer/webhook/test"),
};

/* ----------------------------------------------------------------- files */

export const filesApi = {
  upload: (file: File): Promise<FileRef> => {
    const fd = new FormData();
    fd.append("file", file);
    return http<FileRef>("POST", "/files", { body: fd });
  },
};
