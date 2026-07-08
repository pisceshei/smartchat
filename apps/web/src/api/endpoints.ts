/** Generated-style typed endpoints matching the backend module routes
 *  (apps/api/app/modules/*). One namespace per backend module. */
import { API_BASE, http, httpBlob } from "./client";
import { useAuthStore } from "@/stores/auth";
import type {
  AiAgent,
  ComposerAssistMode,
  Flow,
  FlowCategory,
  FlowGraph,
  FlowStats7d,
  FlowSummary,
  FlowTemplate,
  FlowTestResult,
  FlowValidationResult,
  Intent,
  KbCollection,
  KbDocType,
  KbDocument,
  KbFaqPair,
  KeywordDict,
  KeywordDictItem,
  TranslateResult,
} from "./types";
import type {
  AuthOut,
  ApiTokenCreated,
  ApiTokenInfo,
  AuditEntry,
  AuthResponse,
  BridgeDeviceStatus,
  ChannelAccount,
  ChannelIdentity,
  ChannelType,
  Contact,
  DeviceAccount,
  DeviceQr,
  DeviceStatus,
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
import type {
  AdsReport,
  AiSummaryReport,
  Broadcast,
  BroadcastListItem,
  BroadcastRun,
  BroadcastSchedule,
  BroadcastSendRules,
  BroadcastType,
  ChannelsReport,
  CheckoutDuration,
  CheckoutResult,
  CustomerDimension,
  CustomersReport,
  EdmCampaign,
  EdmProvider,
  Invoice,
  MsgTemplate,
  OnlineTimeReport,
  OrderPreview,
  Plan,
  PointsLedgerPage,
  PointsTopupResult,
  RecipientPage,
  RecipientState,
  ReportExportJob,
  ReportExportStatus,
  ReportFilters,
  ReportShare,
  Segment,
  SegmentDefinition,
  SegmentMode,
  ServiceOverviewReport,
  StripeConfig,
  StripeConfigUpdate,
  SmsSignature,
  SplitLink,
  SplitLinkClickStats,
  SplitLinkTarget,
  SplitStrategy,
  Subscription,
  SubscriptionAddons,
  SummaryReport,
  TemplateChannel,
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
    // Backend `advance_read` requires a JSON body (ReadIn, all-optional). Sending
    // no body makes FastAPI reject it with 422, so always send an empty object.
    http<{ ok: boolean; agent_unread_count: number }>(
      "POST",
      `/inbox/conversations/${conversationId}/read`,
      { body: {} },
    ),

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

/** UI filter grammar → backend Predicate grammar. The drawer speaks
 *  empty/not_empty + tag/is_blacklisted/assignee; the backend speaks
 *  not_exists/exists + tag_id/blacklisted/assignee_member_id — sending the
 *  UI names raw gets a 422. */
const PREDICATE_OP_MAP: Record<string, string> = {
  empty: "not_exists",
  not_empty: "exists",
};
const PREDICATE_FIELD_MAP: Record<string, string> = {
  tag: "tag_id",
  is_blacklisted: "blacklisted",
  assignee: "assignee_member_id",
};

function toBackendPredicates(filters?: FilterPredicate[]): Record<string, unknown>[] {
  return (filters ?? []).map((f) => ({
    field: PREDICATE_FIELD_MAP[f.field] ?? f.field,
    op: PREDICATE_OP_MAP[f.op] ?? f.op,
    value: f.value ?? null,
  }));
}

/** The list endpoint returns flat rows; older builds omitted the array
 *  fields entirely, which crashed the customers table (undefined.slice).
 *  Normalize here exactly like get() does so the UI never sees undefined. */
function normalizeContact(it: Contact & { last_seen_at?: string | null }): Contact {
  return {
    ...it,
    channel_identities: it.channel_identities ?? [],
    tags: it.tags ?? [],
    one_id: it.one_id ?? it.id,
    last_active_at: it.last_active_at ?? it.last_seen_at ?? null,
  };
}

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
          predicates: toBackendPredicates(params.filters),
          logic: "and",
          limit: pageSize,
          offset: (page - 1) * pageSize,
        },
      },
    );
    return {
      items: (res.items ?? []).map(normalizeContact),
      total: res.total,
      page,
      page_size: pageSize,
    };
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
  /** Backend streams text/csv directly (no task envelope) — fetch the blob
   *  and trigger a browser download. */
  export: async (params: { q?: string; filters?: FilterPredicate[] }): Promise<void> => {
    const blob = await httpBlob("POST", "/contacts/export", {
      body: {
        q: params.q || null,
        predicates: toBackendPredicates(params.filters),
        logic: "and",
      },
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `contacts-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
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

/* ------------------------------------------- whatsapp/line app (QR bridge) */

/** QR-scan device provisioning for the whatsapp_app / line_app channels.
 *  `connect()` creates the bridge device (returns account_id — tolerant of the
 *  serializer emitting either account_id or id); the modal then polls `qr()` +
 *  `status()` every ~2s until status === "online". These routes are served by
 *  the QR-flow branch in channels/router.py backed by the bridge-wa service —
 *  callers must degrade gracefully (a 404/501 surfaces while the bridge is not
 *  yet deployed). */
export const devicesApi = {
  connect: async (
    channelType: ChannelType,
    body: Record<string, unknown> = {},
  ): Promise<DeviceAccount> => {
    const res = await http<Record<string, unknown>>(
      "POST",
      `/channels/${channelType}/accounts`,
      { body },
    );
    return {
      account_id: String(res.account_id ?? res.id ?? ""),
      status: (res.status as BridgeDeviceStatus) ?? "provisioning",
    };
  },
  qr: (channelType: ChannelType, accountId: string) =>
    http<DeviceQr>("GET", `/channels/${channelType}/${accountId}/qr`),
  status: (channelType: ChannelType, accountId: string) =>
    http<DeviceStatus>("GET", `/channels/${channelType}/${accountId}/status`),
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

/* ============================================================ P2: automation */

export const flowsApi = {
  /* categories (left folder tree) */
  categories: () => http<FlowCategory[]>("GET", "/flow-categories"),
  createCategory: (body: { name: string }) =>
    http<FlowCategory>("POST", "/flow-categories", { body }),
  renameCategory: (id: string, body: { name: string }) =>
    http<FlowCategory>("PATCH", `/flow-categories/${id}`, { body }),
  removeCategory: (id: string) => http<void>("DELETE", `/flow-categories/${id}`),

  /* flow list + crud */
  list: (params: { category_id?: string; q?: string } = {}) =>
    http<FlowSummary[]>("GET", "/flows", { query: params }),
  get: (id: string) => http<Flow>("GET", `/flows/${id}`),
  create: (body: {
    name: string;
    channel_type: string;
    category_id?: string | null;
    template_slug?: string | null;
  }) => http<Flow>("POST", "/flows", { body }),
  update: (
    id: string,
    body: Partial<{
      name: string;
      enabled: boolean;
      channel_type: string;
      category_id: string | null;
      priority: number;
    }>,
  ) => http<FlowSummary>("PATCH", `/flows/${id}`, { body }),
  duplicate: (id: string) => http<Flow>("POST", `/flows/${id}/duplicate`),
  remove: (id: string) => http<void>("DELETE", `/flows/${id}`),

  /* editor — draft saved via PATCH /flows/{id} {draft_graph} (no separate route) */
  saveDraft: (id: string, graph: FlowGraph) =>
    http<Flow>("PATCH", `/flows/${id}`, { body: { draft_graph: graph } }),
  validate: (id: string, graph: FlowGraph) =>
    http<FlowValidationResult>("POST", `/flows/${id}/validate`, { body: { graph } }),
  publish: (id: string, graph: FlowGraph) =>
    http<FlowValidationResult & { version_id?: string }>("POST", `/flows/${id}/publish`, {
      body: { graph },
    }),
  testRun: (id: string, body: { graph: FlowGraph; input?: string }) =>
    http<FlowTestResult>("POST", `/flows/${id}/test-run`, { body }),

  /* data drill-down */
  stats: (id: string, params: { from?: string; to?: string } = {}) =>
    http<{ summary: FlowStats7d; nodes: Record<string, number> }>("GET", `/flows/${id}/stats`, {
      query: params,
    }),

  /* template gallery — "use" = create a flow seeded from the template slug */
  templates: () => http<FlowTemplate[]>("GET", "/flow-templates"),
  useTemplate: (
    template: { id: string; channel_type: string },
    body: { name: string; category_id?: string | null },
  ) =>
    http<Flow>("POST", "/flows", {
      body: {
        name: body.name,
        channel_type: template.channel_type,
        category_id: body.category_id ?? null,
        template_slug: template.id,
      },
    }),
};

export const keywordDictsApi = {
  list: () => http<KeywordDict[]>("GET", "/flow-keyword-dicts"),
  create: (body: { name: string }) => http<KeywordDict>("POST", "/flow-keyword-dicts", { body }),
  rename: (id: string, body: { name: string }) =>
    http<KeywordDict>("PATCH", `/flow-keyword-dicts/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/flow-keyword-dicts/${id}`),
  items: (dictId: string) =>
    http<KeywordDictItem[]>("GET", `/flow-keyword-dicts/${dictId}/items`),
  setItems: (dictId: string, keywords: string[]) =>
    http<KeywordDictItem[]>("PUT", `/flow-keyword-dicts/${dictId}/items`, { body: { keywords } }),
};

/* ================================================================= P2: AI */

export const aiApi = {
  listAgents: () => http<AiAgent[]>("GET", "/ai/agents"),
  getAgent: (id: string) => http<AiAgent>("GET", `/ai/agents/${id}`),
  createAgent: (body: Partial<AiAgent> & { name: string }) =>
    http<AiAgent>("POST", "/ai/agents", { body }),
  updateAgent: (id: string, body: Partial<AiAgent>) =>
    http<AiAgent>("PATCH", `/ai/agents/${id}`, { body }),
  removeAgent: (id: string) => http<void>("DELETE", `/ai/agents/${id}`),
};

export const kbApi = {
  collections: () => http<KbCollection[]>("GET", "/ai/kb/collections"),
  createCollection: (body: { name: string; description?: string }) =>
    http<KbCollection>("POST", "/ai/kb/collections", { body }),
  updateCollection: (id: string, body: { name?: string; description?: string }) =>
    http<KbCollection>("PATCH", `/ai/kb/collections/${id}`, { body }),
  removeCollection: (id: string) => http<void>("DELETE", `/ai/kb/collections/${id}`),

  documents: (collectionId: string) =>
    http<KbDocument[]>("GET", `/ai/kb/collections/${collectionId}/documents`),
  /* Backend has ONE document-create route discriminated by source_type
   * (prose/upload/url → text; faq/product → items[]). */
  addFile: async (collectionId: string, file: File): Promise<KbDocument> => {
    const text = await file.text(); // client-side extract (binary/PDF is P3)
    return http<KbDocument>("POST", `/ai/kb/collections/${collectionId}/documents`, {
      body: { title: file.name, source_type: "upload", text },
    });
  },
  addFaq: (collectionId: string, body: { title: string; pairs: KbFaqPair[] }) =>
    http<KbDocument>("POST", `/ai/kb/collections/${collectionId}/documents`, {
      body: { title: body.title, source_type: "faq", items: body.pairs },
    }),
  addUrl: (collectionId: string, body: { url: string }) =>
    http<KbDocument>("POST", `/ai/kb/collections/${collectionId}/documents`, {
      body: { title: body.url, source_type: "url", source_ref: body.url },
    }),
  addText: (collectionId: string, body: { title: string; text: string }) =>
    http<KbDocument>("POST", `/ai/kb/collections/${collectionId}/documents`, {
      body: { title: body.title, source_type: "prose", text: body.text },
    }),
  importProducts: (collectionId: string, body: { source: string; items?: unknown[] }) =>
    http<KbDocument>("POST", `/ai/kb/collections/${collectionId}/documents`, {
      body: { title: "商品匯入", source_type: "product", items: body.items ?? [], source_ref: body.source },
    }),
  reingest: (_collectionId: string, docId: string) =>
    http<KbDocument>("POST", `/ai/kb/documents/${docId}/reingest`),
  removeDocument: (_collectionId: string, docId: string) =>
    http<void>("DELETE", `/ai/kb/documents/${docId}`),
};

export const intentsApi = {
  list: () => http<Intent[]>("GET", "/ai/intents"),
  create: (body: { name: string; description?: string; examples: string[] }) =>
    http<Intent>("POST", "/ai/intents", { body }),
  update: (id: string, body: Partial<Intent>) =>
    http<Intent>("PATCH", `/ai/intents/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/ai/intents/${id}`),
};

/* --------------------------------------------------------- translate / assist */

export const translateApi = {
  /** On-demand translation for the inbox inline display + outbound preview.
   * Backend contract uses dst_lang/src_lang. */
  translate: (body: {
    text: string;
    target_lang: string;
    source_lang?: string | null;
    engine?: string;
  }) =>
    http<TranslateResult>("POST", "/ai/translate", {
      body: { text: body.text, dst_lang: body.target_lang, src_lang: body.source_lang ?? null },
    }),

  /** Translate one existing message (persists + realtime patch). */
  translateMessage: (conversationId: string, messageId: string, agentLang?: string) =>
    http<TranslateResult>(
      "POST",
      `/ai/conversations/${conversationId}/messages/${messageId}/translate`,
      { body: { agent_lang: agentLang ?? null } },
    ),
};

/** Composer AI-assist streams tokens over SSE (text/event-stream). We read the
 * body reader directly (the JSON `http` helper can't stream). Emits each delta
 * to `onDelta` and resolves with the full text. Falls back to a plain-text
 * body if the server doesn't use SSE framing. */
export async function composeAssistStream(
  body: {
    conversation_id: string;
    mode: ComposerAssistMode;
    text: string;
    target_lang?: string;
    tone?: string;
  },
  onDelta: (full: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const { token, workspaceId } = useAuthStore.getState();
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "text/event-stream" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (workspaceId) headers["X-Workspace-Id"] = workspaceId;

  // Backend contract: POST /ai/assist {op, text, params}; SSE frames
  // {type:delta,text} … {type:done,text,balance_after} | {type:error,code,detail}
  const params: Record<string, unknown> = {};
  if (body.tone) params.tone = body.tone;
  if (body.target_lang) params.target_lang = body.target_lang;
  const res = await fetch(`${API_BASE}/ai/assist`, {
    method: "POST",
    headers,
    body: JSON.stringify({ op: body.mode, text: body.text, params }),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail: string | undefined;
    try {
      detail = ((await res.json()) as { detail?: string }).detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail ?? `assist ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";

  const pushDelta = (piece: string) => {
    if (!piece) return;
    full += piece;
    onDelta(full);
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line; tolerate bare \n streams too.
    const parts = buffer.split(/\r?\n/);
    buffer = parts.pop() ?? "";
    for (const line of parts) {
      const trimmed = line.trimStart();
      if (!trimmed) continue;
      if (trimmed.startsWith("data:")) {
        const payload = trimmed.slice(5).trim();
        if (payload === "[DONE]") continue;
        try {
          const obj = JSON.parse(payload) as {
            type?: string;
            delta?: string;
            text?: string;
            code?: string;
            detail?: string;
          };
          if (obj.type === "error") {
            throw new Error(obj.detail ?? obj.code ?? "assist error");
          }
          if (obj.type === "done") {
            // authoritative full text — replace accumulator, don't append
            full = obj.text ?? full;
            onDelta(full);
          } else {
            // {type:delta,text:piece} or legacy {delta}
            pushDelta(obj.delta ?? obj.text ?? "");
          }
        } catch (e) {
          if (e instanceof Error && e.message.includes("assist")) throw e;
          pushDelta(payload);
        }
      } else {
        // non-SSE plain text line
        pushDelta(line + "\n");
      }
    }
  }
  if (buffer.trim()) pushDelta(buffer);
  return full;
}

/** Doc-type labels for the KB add-document picker (kept out of i18n since the
 * set is small and stable, mirroring constants/channels.ts precedent). */
export const KB_DOC_TYPES: { type: KbDocType; label: string }[] = [
  { type: "file", label: "上傳檔案" },
  { type: "faq", label: "FAQ 問答" },
  { type: "product", label: "商品匯入" },
  { type: "url", label: "網址匯入" },
  { type: "text", label: "純文字" },
];

/* ============================================================ P3: marketing */

export const segmentsApi = {
  list: () => http<Segment[]>("GET", "/segments"),
  get: (id: string) => http<Segment>("GET", `/segments/${id}`),
  create: (body: { name: string; mode: SegmentMode; definition: SegmentDefinition }) =>
    http<Segment>("POST", "/segments", { body }),
  update: (id: string, body: { name?: string; definition?: SegmentDefinition }) =>
    http<Segment>("PATCH", `/segments/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/segments/${id}`),
  /** Live audience-size estimate (backend runs it under a 5s statement timeout). */
  estimate: (definition: SegmentDefinition) =>
    http<{ count: number }>("POST", "/segments/estimate", { body: { definition } }),
};

export interface BroadcastCreateBody {
  name: string;
  type: BroadcastType;
  channel_type: string;
  channel_account_id: string;
  segment_id: string;
  template_id: string;
  variable_mapping: Record<string, string>;
  schedule: BroadcastSchedule;
  send_rules: BroadcastSendRules;
}

export const broadcastsApi = {
  list: (params: { type?: BroadcastType; status?: string; q?: string } = {}) =>
    http<BroadcastListItem[]>("GET", "/broadcasts", { query: params }),
  get: (id: string) => http<Broadcast>("GET", `/broadcasts/${id}`),
  create: (body: BroadcastCreateBody) => http<Broadcast>("POST", "/broadcasts", { body }),
  update: (id: string, body: Partial<BroadcastCreateBody>) =>
    http<Broadcast>("PATCH", `/broadcasts/${id}`, { body }),
  pause: (id: string) => http<Broadcast>("POST", `/broadcasts/${id}/pause`),
  resume: (id: string) => http<Broadcast>("POST", `/broadcasts/${id}/resume`),
  cancel: (id: string) => http<Broadcast>("POST", `/broadcasts/${id}/cancel`),
  remove: (id: string) => http<void>("DELETE", `/broadcasts/${id}`),
  recycleBin: () => http<BroadcastListItem[]>("GET", "/broadcasts/recycle-bin"),
  restore: (id: string) => http<Broadcast>("POST", `/broadcasts/${id}/restore`),
  runs: (id: string) => http<BroadcastRun[]>("GET", `/broadcasts/${id}/runs`),
  recipients: (
    id: string,
    runId: string,
    params: { state?: RecipientState; cursor?: string } = {},
  ) =>
    http<RecipientPage>("GET", `/broadcasts/${id}/runs/${runId}/recipients`, { query: params }),
};

/** Message templates — channel-scoped. Body shape is channel-specific (see the
 *  contract in api/types.ts). */
export const msgTemplatesApi = {
  list: (channel: TemplateChannel) => http<MsgTemplate[]>("GET", `/msg-templates/${channel}`),
  get: (channel: TemplateChannel, id: string) =>
    http<MsgTemplate>("GET", `/msg-templates/${channel}/${id}`),
  create: (channel: TemplateChannel, body: Record<string, unknown>) =>
    http<MsgTemplate>("POST", `/msg-templates/${channel}`, { body }),
  update: (channel: TemplateChannel, id: string, body: Record<string, unknown>) =>
    http<MsgTemplate>("PATCH", `/msg-templates/${channel}/${id}`, { body }),
  remove: (channel: TemplateChannel, id: string) =>
    http<void>("DELETE", `/msg-templates/${channel}/${id}`),
  /** Pull latest Meta approval status for the WhatsApp templates of an account. */
  syncWhatsapp: (channelAccountId: string) =>
    http<{ synced: number }>("POST", "/msg-templates/whatsapp/sync", {
      body: { channel_account_id: channelAccountId },
    }),
  /* SMS signatures */
  signatures: () => http<SmsSignature[]>("GET", "/msg-templates/sms/signatures"),
  createSignature: (body: { name: string; text: string }) =>
    http<SmsSignature>("POST", "/msg-templates/sms/signatures", { body }),
};

export const splitLinksApi = {
  list: () => http<SplitLink[]>("GET", "/split-links"),
  get: (id: string) => http<SplitLink>("GET", `/split-links/${id}`),
  create: (body: {
    name: string;
    channel_type: string;
    strategy: SplitStrategy;
    targets: SplitLinkTarget[];
    prefill_text: string;
  }) => http<SplitLink>("POST", "/split-links", { body }),
  update: (
    id: string,
    body: Partial<{
      name: string;
      strategy: SplitStrategy;
      targets: SplitLinkTarget[];
      prefill_text: string;
      status: "active" | "disabled";
    }>,
  ) => http<SplitLink>("PATCH", `/split-links/${id}`, { body }),
  remove: (id: string) => http<void>("DELETE", `/split-links/${id}`),
  clicks: (id: string, params: { from?: string; to?: string } = {}) =>
    http<SplitLinkClickStats>("GET", `/split-links/${id}/clicks`, { query: params }),
};

export const edmApi = {
  list: () => http<EdmCampaign[]>("GET", "/edm"),
  create: (body: {
    name: string;
    provider: EdmProvider;
    segment_id: string;
    template_id: string;
    schedule: BroadcastSchedule;
  }) => http<EdmCampaign>("POST", "/edm", { body }),
};

/** Umbrella namespace mirroring the backend 行銷 module grouping. */
export const marketingApi = {
  segments: segmentsApi,
  broadcasts: broadcastsApi,
  templates: msgTemplatesApi,
  splitLinks: splitLinksApi,
  edm: edmApi,
};

/* ============================================================== P3: reports */

function reportQuery(f: ReportFilters): Record<string, string | undefined> {
  return {
    from: f.from,
    to: f.to,
    interval: f.interval,
    channel_type: f.channel_type ?? undefined,
    channel_account_id: f.channel_account_id ?? undefined,
    member_id: f.member_id ?? undefined,
  };
}

export const reportsApi = {
  serviceOverview: (f: ReportFilters = {}) =>
    http<ServiceOverviewReport>("GET", "/reports/service-overview", { query: reportQuery(f) }),
  customers: (f: ReportFilters & { dimension?: CustomerDimension } = {}) =>
    http<CustomersReport>("GET", "/reports/customers", {
      query: { ...reportQuery(f), dimension: f.dimension },
    }),
  onlineTime: (f: ReportFilters = {}) =>
    http<OnlineTimeReport>("GET", "/reports/online-time", { query: reportQuery(f) }),
  summary: (f: ReportFilters = {}) =>
    http<SummaryReport>("GET", "/reports/summary", { query: reportQuery(f) }),
  channels: (f: ReportFilters = {}) =>
    http<ChannelsReport>("GET", "/reports/channels", { query: reportQuery(f) }),
  adsFacebook: (f: ReportFilters = {}) =>
    http<AdsReport>("GET", "/reports/ads/facebook", { query: reportQuery(f) }),
  adsMessenger: (f: ReportFilters = {}) =>
    http<AdsReport>("GET", "/reports/ads/messenger", { query: reportQuery(f) }),
  aiSummary: (f: ReportFilters = {}) =>
    http<AiSummaryReport>("GET", "/reports/ai-summary", { query: reportQuery(f) }),

  /** Async CSV export → poll for the signed MinIO URL. */
  export: (report: string, f: ReportFilters & { dimension?: CustomerDimension } = {}) =>
    http<ReportExportJob>("POST", `/reports/${report}/export`, {
      body: { ...reportQuery(f), dimension: f.dimension },
    }),
  exportStatus: (jobId: string) =>
    http<ReportExportStatus>("GET", `/reports/exports/${jobId}`),
  share: (report: string, config: Record<string, unknown>) =>
    http<ReportShare>("POST", `/reports/${report}/share`, { body: { config } }),
};

/* ============================================================== P3: billing */

export interface CheckoutBody {
  plan_code: string;
  duration_days: CheckoutDuration;
  addons: Partial<SubscriptionAddons>;
  use_balance?: boolean;
}

export const billingApi = {
  plans: () => http<Plan[]>("GET", "/billing/plans"),
  subscription: () => http<Subscription>("GET", "/billing/subscription"),
  checkout: (body: CheckoutBody) => http<CheckoutResult>("POST", "/billing/checkout", { body }),
  topupPoints: (points: number) =>
    http<PointsTopupResult>("POST", "/billing/points/topup", { body: { points } }),
  pointsLedger: (cursor?: string) =>
    http<PointsLedgerPage>("GET", "/billing/points/ledger", { query: { cursor } }),
  invoices: () => http<Invoice[]>("GET", "/billing/invoices"),
  /** super_admin-only no-charge plan switch (the self-use path). */
  adminChangePlan: (body: {
    plan_code: string;
    duration_days: CheckoutDuration;
    addons: Partial<SubscriptionAddons>;
  }) => http<Subscription>("POST", "/billing/admin/change-plan", { body }),

  /** super_admin-only platform Stripe key management. GET returns the
   *  publishable key + booleans (never the secrets); PUT stores the raw keys
   *  encrypted server-side. Once saved, checkout uses the live key. */
  stripeConfig: {
    get: () => http<StripeConfig>("GET", "/billing/stripe-config"),
    save: (body: StripeConfigUpdate) =>
      http<StripeConfig>("PUT", "/billing/stripe-config", { body }),
  },
};

/** Client-side order estimate for the live 訂單預覽 while the user adjusts the
 *  plan/duration/addons. The definitive numbers come back from
 *  billingApi.checkout — this only powers the responsive preview.
 *  Duration discounts: 30d 0 / 90d .10 / 180d .15 / 360d .20 / 720d .25.
 *  7d = 試用 (nominal trial fee, no long-term discount). */
export const DURATION_DISCOUNT: Record<CheckoutDuration, number> = {
  7: 0,
  30: 0,
  90: 0.1,
  180: 0.15,
  360: 0.2,
  720: 0.25,
};

/** 7-day trial is billed as a nominal fraction of the monthly price (matches
 *  the captured Max 7-day 原價 $19.90 ≈ 10% of $199). */
export const TRIAL_FRACTION = 0.1;
export const HANDLING_FEE_RATE = 0.07;

export function estimateOrder(params: {
  plan_monthly: number;
  duration_days: CheckoutDuration;
  addon_monthly_total: number;
  balance: number;
  use_balance: boolean;
  currency?: string;
}): OrderPreview {
  const { plan_monthly, duration_days, addon_monthly_total, balance, use_balance } = params;
  const round2 = (n: number) => Math.round(n * 100) / 100;
  const months = duration_days === 7 ? TRIAL_FRACTION : duration_days / 30;
  const base_price = round2((plan_monthly + addon_monthly_total) * months);
  const discount = round2(base_price * DURATION_DISCOUNT[duration_days]);
  const handling_fee = round2((base_price - discount) * HANDLING_FEE_RATE);
  const net = base_price - discount + handling_fee;
  const balance_applied = use_balance ? round2(Math.min(Math.max(balance, 0), net)) : 0;
  const amount_due = round2(Math.max(net - balance_applied, 0));
  return {
    base_price,
    discount,
    handling_fee,
    balance_applied,
    amount_due,
    currency: params.currency ?? "USD",
  };
}
