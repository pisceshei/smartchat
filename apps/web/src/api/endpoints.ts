/** Generated-style typed endpoints matching the backend module routes
 *  (apps/api/app/modules/*). One namespace per backend module. */
import { http } from "./client";
import type {
  ApiTokenCreated,
  ApiTokenInfo,
  AuditEntry,
  AuthResponse,
  ChannelAccount,
  ChannelType,
  Contact,
  ConversationSettings,
  Conversation,
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

export const authApi = {
  login: (body: { email: string; password: string }) =>
    http<AuthResponse>("POST", "/auth/login", { body }),
  register: (body: { email: string; password: string; name: string; workspace_name: string }) =>
    http<AuthResponse>("POST", "/auth/register", { body }),
  me: () => http<User>("GET", "/auth/me"),
};

export const workspacesApi = {
  list: () => http<WorkspaceBrief[]>("GET", "/workspaces"),
};

/* ---------------------------------------------------------------- inbox */

export type InboxTab = "mine" | "bot" | "ai" | "unassigned" | "all" | "team";
export type InboxListFilter = "all" | "unread" | "needs_reply";

export const inboxApi = {
  summary: () => http<InboxSummary>("GET", "/inbox/summary"),

  listConversations: (params: {
    tab?: InboxTab;
    view_id?: string;
    q?: string;
    filter?: InboxListFilter;
    cursor?: string;
    limit?: number;
  }) => http<CursorPage<Conversation>>("GET", "/conversations", { query: params }),

  getConversation: (id: string) => http<Conversation>("GET", `/conversations/${id}`),

  listMessages: (conversationId: string, params: { before?: string; limit?: number } = {}) =>
    http<CursorPage<Message>>("GET", `/conversations/${conversationId}/messages`, {
      query: params,
    }),

  sendMessage: (
    conversationId: string,
    body: { client_msg_id: string; content: MessageContent; is_note?: boolean },
  ) => http<Message>("POST", `/conversations/${conversationId}/messages`, { body }),

  markRead: (conversationId: string) =>
    http<void>("POST", `/conversations/${conversationId}/read`),

  updateConversation: (
    conversationId: string,
    body: Partial<{
      status: "open" | "closed";
      assignee_member_id: string | null;
      bot_managed: boolean;
      remark: string | null;
      translate: { enabled: boolean; agent_lang?: string | null; customer_lang?: string | null };
      tag_ids: string[];
    }>,
  ) => http<Conversation>("PATCH", `/conversations/${conversationId}`, { body }),

  history: (conversationId: string) =>
    http<Conversation[]>("GET", `/conversations/${conversationId}/history`),

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
  get: (id: string) => http<Contact>("GET", `/contacts/${id}`),
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
