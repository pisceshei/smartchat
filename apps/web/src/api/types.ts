/** Typed mirror of the backend contracts (plan 附錄 A + py_contracts/content.py).
 *  Keep field names snake_case — they cross the wire as-is. */

/* ------------------------------------------------------------- channels */

export type ChannelType =
  | "widget"
  | "whatsapp_app"
  | "whatsapp_api"
  // backend canonical WhatsApp API transports (gallery card = whatsapp_api;
  // use galleryType() to map). ChannelAccount.channel_type carries these.
  | "whatsapp_cloud"
  | "whatsapp_bsp"
  | "messenger"
  | "instagram"
  | "telegram_app"
  | "telegram_bot"
  | "email"
  | "youtube"
  | "tiktok_app"
  | "tiktok_business"
  | "wechat_kf"
  | "line_app"
  | "line_oa"
  | "wecom"
  | "wechat"
  | "zalo_app"
  | "slack"
  | "vk";

export type ChannelAccountStatus = "active" | "error" | "pending" | "disconnected";

export interface ChannelAccount {
  id: string;
  workspace_id: string;
  channel_type: ChannelType;
  external_id: string;
  display_name: string;
  status: ChannelAccountStatus;
  config: Record<string, unknown>;
  created_at: string;
}

/** One WhatsApp number on a BSP (YCloud) account — the connect modal picker. */
export interface BspNumber {
  id: string | null;
  phone_number: string;
  display_phone_number: string | null;
  verified_name: string | null;
  waba_id: string | null;
  quality_rating: string | null;
  messaging_limit: string | null;
  status: string | null;
}

/* --------------------------------------------- whatsapp/line app (QR bridge)
 * The whatsapp_app / line_app channels pair a personal number through a
 * whatsmeow bridge (infra/bridge-wa). connect() provisions the device and
 * returns account_id; the modal then polls .../qr (a QR string to render as an
 * image) + .../status until the device reports "online". Statuses mirror
 * device_bridges.status (models/channels.py). */
export type BridgeDeviceStatus =
  | "provisioning"
  | "awaiting_qr"
  | "connecting"
  | "pairing"
  | "online"
  | "offline"
  | "logged_out"
  | "banned";

export interface DeviceAccount {
  account_id: string;
  status: BridgeDeviceStatus;
}

export interface DeviceQr {
  /** The QR string to render; null once paired or while (re)generating. */
  qr: string | null;
  status: BridgeDeviceStatus;
}

export interface DeviceStatus {
  status: BridgeDeviceStatus;
  jid?: string | null;
  phone?: string | null;
  pushname?: string | null;
}

/* ------------------------------------------------------- message content */

export interface TextBlock {
  kind: "text";
  text: string;
}

export interface MediaBlock {
  kind: "media";
  media_type: "image" | "video" | "audio" | "voice" | "file" | "sticker";
  file_id?: string | null;
  /** Resolved download URL provided by the API when serializing. */
  url?: string | null;
  caption?: string | null;
  mime?: string | null;
  size?: number | null;
  duration_ms?: number | null;
  width?: number | null;
  height?: number | null;
  file_name?: string | null;
}

export interface CardButton {
  text: string;
  action: "url" | "postback";
  value: string;
}

export interface ProductCardBlock {
  kind: "product_card";
  title: string;
  subtitle?: string | null;
  image_file_id?: string | null;
  image_url?: string | null;
  price?: string | null;
  currency?: string | null;
  url?: string | null;
  buttons: CardButton[];
}

export interface QuickButton {
  id: string;
  text: string;
}

export interface QuickButtonsBlock {
  kind: "quick_buttons";
  text: string;
  buttons: QuickButton[];
}

export interface ButtonReplyBlock {
  kind: "button_reply";
  payload: string;
  text: string;
  flow_session_id?: string | null;
}

export interface TemplateBlock {
  kind: "template";
  template_name: string;
  language: string;
  components: Record<string, unknown>;
  category?: string | null;
}

export interface LocationBlock {
  kind: "location";
  latitude: number;
  longitude: number;
  name?: string | null;
  address?: string | null;
}

export interface EmailBlock {
  kind: "email";
  subject?: string | null;
  text: string;
  html_body_file_id?: string | null;
  headers?: Record<string, unknown>;
  cc?: string[];
  bcc?: string[];
}

export interface SystemEventBlock {
  kind: "system_event";
  event: string;
  meta: Record<string, unknown>;
}

export type ContentBlock =
  | TextBlock
  | MediaBlock
  | ProductCardBlock
  | QuickButtonsBlock
  | ButtonReplyBlock
  | TemplateBlock
  | LocationBlock
  | EmailBlock
  | SystemEventBlock;

export interface MessageContent {
  blocks: ContentBlock[];
}

/* --------------------------------------------------------------- message */

export type DeliveryStatus = "pending" | "sent" | "delivered" | "read" | "failed";
export type SenderType = "contact" | "member" | "ai_agent" | "automation" | "flow" | "system";

export interface Message {
  id: string;
  workspace_id: string;
  conversation_id: string;
  direction: "in" | "out";
  sender_type: SenderType;
  sender_id?: string | null;
  sender_name?: string | null;
  msg_type: string;
  content: MessageContent;
  text_plain?: string | null;
  is_note: boolean;
  source_flow_id?: string | null;
  delivery_status: DeliveryStatus;
  client_msg_id?: string | null;
  created_at: string;
  translations?: Record<string, string> | null;
  error_reason?: string | null;
}

/* ---------------------------------------------------------- conversation */

export type ConversationStatus = "open" | "closed";
export type HandlerType = "bot" | "ai_agent" | "member" | "unassigned";

export interface Tag {
  id: string;
  workspace_id?: string;
  kind: "visitor" | "conversation";
  name: string;
  color: string;
  usage_count?: number;
  created_at?: string;
}

export interface ContactBrief {
  id: string;
  display_name: string | null;
  avatar_url?: string | null;
  email?: string | null;
  phone?: string | null;
  country?: string | null;
  language?: string | null;
}

export interface ConversationTranslateConfig {
  enabled: boolean;
  agent_lang?: string | null;
  customer_lang?: string | null;
}

export interface Conversation {
  id: string;
  workspace_id: string;
  contact_id: string;
  channel_type: ChannelType;
  channel_account_id: string;
  status: ConversationStatus;
  handler: HandlerType;
  assignee_member_id?: string | null;
  assignee_name?: string | null;
  bot_managed: boolean;
  needs_reply: boolean;
  agent_unread_count: number;
  snippet?: string | null;
  contact?: ContactBrief | null;
  tags?: Tag[];
  remark?: string | null;
  translate?: ConversationTranslateConfig | null;
  last_message_at?: string | null;
  created_at: string;
}

export interface InboxView {
  id: string;
  name: string;
  visibility: "personal" | "public";
  filters: {
    channel_type?: ChannelType | null;
    status?: ConversationStatus | null;
    assignee_member_id?: string | null;
    tag_id?: string | null;
  };
  created_at?: string;
}

export interface InboxSummary {
  mine: number;
  bot: number;
  ai: number;
  unassigned: number;
  all: number;
  team: number;
  views?: Record<string, number>;
}

/* ---------------------------------------------------------------- contact */

export interface ChannelIdentity {
  id: string;
  channel_type: ChannelType;
  channel_account_id: string;
  external_user_id: string;
  display_name?: string | null;
  avatar_url?: string | null;
}

export interface Contact {
  id: string;
  workspace_id: string;
  display_name: string | null;
  remark_name?: string | null;
  avatar_url?: string | null;
  email?: string | null;
  phone?: string | null;
  language?: string | null;
  country?: string | null;
  city?: string | null;
  timezone?: string | null;
  last_ip?: string | null;
  device?: string | null;
  browser?: string | null;
  custom: Record<string, unknown>;
  is_blacklisted: boolean;
  /** optional on the wire — the API client normalizes to [] (a bare read of
   *  these once crashed the whole customers page) */
  tags?: Tag[];
  channel_identities?: ChannelIdentity[];
  assignee_member_id?: string | null;
  assignee_name?: string | null;
  one_id?: string | null;
  last_active_at?: string | null;
  created_at: string;
}

export interface MergeCandidate {
  id: string;
  contact: ContactBrief;
  duplicate_contact: ContactBrief;
  match_field: "phone" | "email" | "logged_in_id" | "name";
  status: "suggested" | "linked" | "dismissed";
}

export interface AuditEntry {
  id: string;
  actor_name: string;
  action: string;
  detail?: string | null;
  created_at: string;
}

export interface OrderBrief {
  id: string;
  order_no: string;
  total: string;
  currency: string;
  status: string;
  created_at: string;
}

/* ---------------------------------------------------------- quick replies */

export interface QuickReplyFolder {
  id: string;
  name: string;
  visibility: "personal" | "public";
}

export interface QuickReply {
  id: string;
  folder_id?: string | null;
  title: string;
  content: string;
  shortcut?: string | null;
  visibility: "personal" | "public";
  starred: boolean;
  created_at?: string;
}

/* ---------------------------------------------------------- custom fields */

export interface CustomFieldDef {
  id: string;
  key: string;
  label: string;
  field_type: "text" | "number" | "date" | "select" | "bool";
  options?: string[] | null;
  created_at?: string;
}

/* ------------------------------------------------------------ team / rbac */

export interface Member {
  id: string;
  user_id?: string | null;
  member_type: "human" | "ai_agent";
  display_name: string;
  email?: string | null;
  avatar_url?: string | null;
  role_id?: string | null;
  role_name?: string | null;
  group_ids: string[];
  presence: "online" | "offline";
  daily_cap: number;
  active_conversations: number;
  today_total: number;
  enabled: boolean;
  created_at?: string;
}

export interface Role {
  id: string;
  name: string;
  is_system: boolean;
  permissions: string[]; // e.g. "inbox.view", "inbox.edit", "team.manage"
  member_count?: number;
}

export interface MemberGroup {
  id: string;
  name: string;
  description?: string | null;
  member_count?: number;
}

export interface ShiftSlot {
  weekday: number; // 1..7 (Mon..Sun)
  start_min: number; // minutes from 00:00
  end_min: number;
  enabled: boolean;
}

export interface MemberShifts {
  member_id: string;
  slots: ShiftSlot[];
}

/* ------------------------------------------------------------------ auth */

export interface User {
  id: string;
  email: string;
  name: string;
  avatar_url?: string | null;
}

export interface WorkspaceBrief {
  id: string;
  name: string;
  plan_code?: string;
  role_name?: string;
  member_id?: string;
}

/** Raw backend shape from /auth/{login,register,refresh}. */
export interface AuthOut {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user_id: string;
  email: string;
  display_name: string;
  workspaces: WorkspaceBrief[];
}

/** Normalized shape consumed by the auth store. */
export interface AuthResponse {
  token: string;
  refreshToken: string;
  user: User;
  workspaces: WorkspaceBrief[];
}

/* -------------------------------------------------------------- settings */

export interface ConversationSettings {
  auto_assign_mode: "off" | "round_robin" | "least_busy";
  ai_first: boolean;
  bot_first: boolean;
  keep_managed: boolean;
  auto_close_days: number;
  auto_close_hours: number;
  auto_close_minutes: number;
  offline_reply_mode: "email" | "widget";
}

export interface ApiTokenInfo {
  has_token: boolean;
  token_prefix?: string | null;
  created_at?: string | null;
}

export interface ApiTokenCreated {
  token: string;
  token_prefix: string;
  created_at: string;
}

export interface WebhookConfig {
  url: string;
  token?: string | null;
  channel_message_events: ChannelType[];
  customer_message_only: boolean;
  contact_created: boolean;
  contact_updated: boolean;
  channel_status: boolean;
  enabled: boolean;
}

/* --------------------------------------------------------------- widgets */

/** Widget.config JSONB canonical schema — 後端 bootstrap 原樣透傳，
 *  管理後台讀寫巢狀 config，訪客 widget 消費。 */
export interface WidgetBannerItem {
  image_url: string;
  link_url?: string;
}

export interface WidgetPrechatField {
  key: string;
  type: "text" | "email" | "phone" | "textarea";
  label: string;
  placeholder?: string;
  required?: boolean;
  options?: string[];
}

export interface WidgetConfigJson {
  brand?: {
    name?: string;
    avatar_url?: string;
    welcome_text?: string;
  };
  appearance?: {
    position?: "right" | "left";
    primary_color?: string;
    launcher_text?: string;
    show_branding?: boolean;
  };
  home?: {
    enabled?: boolean;
    banners?: WidgetBannerItem[];
    reply_hint?: string;
  };
  pre_chat?: {
    enabled?: boolean;
    required_before_chat?: boolean;
    fields?: WidgetPrechatField[];
  };
  offline?: {
    email_fallback?: boolean;
    notice?: string;
  };
  routing?: {
    member_ids?: string[];
    strategy?: string;
    ai_agent_id?: string | null;
  };
  features?: {
    file_upload?: boolean;
    emoji?: boolean;
  };
}

/** Flat REST serializer for GET/POST/PATCH /widgets* — settings live in the
 *  nested `config` (never top-level appearance/prechat). */
export interface WidgetConfig {
  id: string;
  widget_key: string;
  name: string;
  config: WidgetConfigJson;
  allowed_domains: string[];
  brand_removed: boolean;
  enabled: boolean;
  channel_account_id?: string | null;
  embed_script_url: string;
  created_at?: string | null;
}

/* ------------------------------------------------------------ pagination */

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor?: string | null;
}

export interface FileRef {
  id: string;
  url: string;
  mime: string;
  size: number;
  file_name: string;
}

/* --------------------------------------------------------------- realtime */

export interface WsEnvelope {
  seq?: number;
  /** normalized by the WS client from `event_id` when absent */
  id?: string;
  event_id?: string;
  type: string;
  ts?: string;
  workspace_id?: string;
  conversation_id?: string | null;
  contact_id?: string | null;
  /** normalized by the WS client from `data` when absent */
  payload?: Record<string, unknown>;
  data?: Record<string, unknown>;
}

export interface FilterPredicate {
  field: string;
  op: "contains" | "eq" | "neq" | "empty" | "not_empty";
  value?: string | null;
}

/* ============================================================ P2: automation
 * Flow-engine graph schema mirrors plan 附錄 B.1: nodes/edges + port protocol
 * (out / button:<id> / branch:<idx> / else / answered / timeout / invalid /
 * success / failed). `kind` is a dotted "<category>.<name>" key resolved
 * against the node catalog (pages/automation/nodes.ts). Field names are
 * snake_case so the graph round-trips to the backend verbatim. */

export type FlowChannelScope = ChannelType | "all";
export type NodeCategory = "trigger" | "condition" | "action";

export interface FlowNode {
  id: string;
  kind: string;
  category: NodeCategory;
  position: { x: number; y: number };
  title?: string | null;
  config: Record<string, unknown>;
}

export interface FlowEdge {
  id: string;
  source: string;
  /** Handle id on the source node = the plan's port protocol value. */
  source_port: string;
  target: string;
}

export interface FlowGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

/** 7-day funnel columns replicated from SaleSmartly's flow list. */
export interface FlowStats7d {
  triggers: number; // 觸發次數 = sessions created
  users: number; // 觸發人數 = distinct contacts
  engagement: number; // 參與度 0..1
  completion: number; // 完成度 0..1
}

export interface FlowSummary {
  id: string;
  name: string;
  enabled: boolean;
  channel_type: FlowChannelScope;
  category_id?: string | null;
  priority: number;
  stats_7d?: FlowStats7d | null;
  published_version_id?: string | null;
  has_draft?: boolean;
  updated_at: string;
  created_at: string;
}

export interface Flow extends FlowSummary {
  draft_graph: FlowGraph;
}

export interface FlowCategory {
  id: string;
  name: string;
  flow_count?: number;
}

export interface FlowTemplate {
  id: string;
  name: string;
  description: string;
  category: string;
  channel_type: FlowChannelScope;
  node_count?: number;
  tags?: string[];
  preview_graph?: FlowGraph | null;
}

export interface FlowValidationError {
  node_id?: string | null;
  code: string;
  message: string;
}

export interface FlowValidationResult {
  ok: boolean;
  errors: FlowValidationError[];
}

export interface FlowTestStep {
  node_id: string;
  kind: string;
  title: string;
  detail?: string | null;
  status: "ok" | "waiting" | "skipped" | "error";
}

export interface FlowTestResult {
  steps: FlowTestStep[];
  preview_messages?: MessageContent[];
}

/* --------------------------------------------------------------- keyword dicts */

export interface KeywordDict {
  id: string;
  name: string;
  item_count?: number;
  created_at?: string;
}

export interface KeywordDictItem {
  id: string;
  dict_id: string;
  keyword: string;
}

/* ================================================================= P2: AI */

export type AiMode = "builtin" | "external";

export interface AiPersona {
  role_prompt: string;
  tone: string;
  languages: string[];
  greeting: string;
  refuse_topics: string[];
}

export interface AiSkills {
  kb_answer: boolean;
  product_card: boolean;
  lead_capture: boolean;
  handoff: boolean;
}

export interface AiEscalationRules {
  keywords: string[];
  intent_ids: string[];
  max_kb_misses: number;
  outside_hours: boolean;
}

export interface AiAgent {
  id: string;
  member_id?: string | null;
  name: string;
  avatar_url?: string | null;
  enabled: boolean;
  model_tier: "fast" | "smart";
  mode: AiMode;
  persona: AiPersona;
  skills: AiSkills;
  kb_collection_ids: string[];
  monthly_quota: number;
  used_this_month?: number;
  escalation: AiEscalationRules;
  webhook_url?: string | null;
  webhook_secret?: string | null;
  created_at?: string;
}

/* ---------------------------------------------------------------- knowledge base */

export type KbDocType = "file" | "faq" | "product" | "url" | "text";
export type KbIngestStatus = "pending" | "processing" | "ready" | "failed";

export interface KbCollection {
  id: string;
  name: string;
  description?: string | null;
  document_count?: number;
  chunk_count?: number;
  created_at?: string;
}

export interface KbDocument {
  id: string;
  collection_id: string;
  title: string;
  doc_type: KbDocType;
  source?: string | null;
  status: KbIngestStatus;
  chunk_count?: number;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface KbFaqPair {
  question: string;
  answer: string;
}

/* -------------------------------------------------------------------- intents */

export interface Intent {
  id: string;
  name: string;
  description?: string | null;
  examples: string[];
  enabled: boolean;
  created_at?: string;
}

/* ------------------------------------------------------- translate / composer */

export type ComposerAssistMode =
  | "rewrite"
  | "expand"
  | "shorten"
  | "tone"
  | "fix_grammar"
  | "translate_draft";

export interface TranslateResult {
  text: string;
  detected_lang?: string | null;
}

/* ============================================================ P3: marketing
 * Broadcast/segment/template/split-link/EDM contracts — mirror the API ROUTE
 * CONTRACT. snake_case round-trips to the backend verbatim. */

/* ------------------------------------------------------- segments (受眾) */

export type SegmentMode = "dynamic" | "static";

/** AND/OR predicate tree — same predicate grammar as contacts/query, wrapped
 *  in nestable logic groups. */
export interface SegmentGroup {
  logic: "and" | "or";
  predicates: FilterPredicate[];
  groups?: SegmentGroup[];
}
export type SegmentDefinition = SegmentGroup;

export interface Segment {
  id: string;
  name: string;
  mode: SegmentMode;
  definition: SegmentDefinition;
  count?: number | null;
  created_at: string;
}

/* -------------------------------------------------------- broadcasts (群發) */

export type BroadcastType = "one_time" | "recurring";
export type BroadcastStatus =
  | "draft"
  | "scheduled"
  | "running"
  | "paused"
  | "completed"
  | "cancelled"
  | "failed";

export interface BroadcastSchedule {
  mode: "immediate" | "scheduled" | "recurring";
  send_at?: string | null;
  rrule?: string | null;
  timezone?: string | null;
}

/** Send rules = 訊息間隔 (3–600s / 3–120s WhatsApp) + optional 發送窗 + 頻次上限. */
export interface BroadcastSendRules {
  interval_seconds: number;
  window?: { start_min: number; end_min: number } | null;
  per_contact_weekly_cap?: number | null;
}

export interface BroadcastListItem {
  id: string;
  name: string;
  type: BroadcastType;
  status: BroadcastStatus;
  channel_type: ChannelType;
  channel_account_id: string;
  send_rule_summary: string;
  planned_count: number;
  sent_count: number;
  delivered_count: number;
  success_rate: number; // 0..1
  created_at: string;
}

export interface BroadcastRun {
  id: string;
  scheduled_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  planned: number;
  sent: number;
  delivered: number;
  read: number;
  failed: number;
  skipped: number;
}

export interface Broadcast extends BroadcastListItem {
  segment_id: string;
  template_id: string;
  variable_mapping: Record<string, string>;
  schedule: BroadcastSchedule;
  send_rules: BroadcastSendRules;
  runs?: BroadcastRun[];
}

export type RecipientState =
  | "planned"
  | "queued"
  | "sent"
  | "delivered"
  | "read"
  | "failed"
  | "skipped";

export interface BroadcastRecipient {
  contact_id: string;
  display_name: string | null;
  state: RecipientState;
  skip_reason?: string | null;
  sent_at?: string | null;
  delivered_at?: string | null;
  read_at?: string | null;
  error?: string | null;
}

export interface RecipientPage {
  items: BroadcastRecipient[];
  next_cursor?: string | null;
}

/* --------------------------------------------------------- message templates */

export type TemplateChannel = "whatsapp" | "email" | "messenger" | "sms";
export type WaTemplateCategory = "marketing" | "utility" | "authentication";
export type WaApprovalStatus =
  | "draft"
  | "pending"
  | "approved"
  | "rejected"
  | "paused"
  | "disabled";

export interface WaTemplateHeader {
  type: "text";
  text: string;
}

export type WaButtonKind = "none" | "call_to_action" | "quick_reply";

export interface WaButtonItem {
  type?: "url" | "phone_number" | "quick_reply";
  text: string;
  value?: string | null;
}

export interface WaTemplateButtons {
  type: WaButtonKind;
  items: WaButtonItem[];
}

export interface WhatsAppTemplate {
  id: string;
  channel: "whatsapp";
  name: string;
  label?: string | null;
  waba_account_id: string;
  category: WaTemplateCategory;
  language: string;
  header?: WaTemplateHeader | null;
  body: { text: string };
  footer?: { text: string } | null;
  buttons?: WaTemplateButtons | null;
  approval_status: WaApprovalStatus;
  meta_template_id?: string | null;
  rejected_reason?: string | null;
  created_at: string;
}

export interface EmailTemplate {
  id: string;
  channel: "email";
  name: string;
  subject: string;
  mjml_source: string;
  variables: string[];
  created_at: string;
}

export interface MessengerTemplate {
  id: string;
  channel: "messenger";
  name: string;
  payload: string;
  message_tag?: string | null;
  created_at: string;
}

export interface SmsTemplate {
  id: string;
  channel: "sms";
  name: string;
  text: string;
  signature_id?: string | null;
  created_at: string;
}

export type MsgTemplate = WhatsAppTemplate | EmailTemplate | MessengerTemplate | SmsTemplate;

export interface SmsSignature {
  id: string;
  name: string;
  text: string;
  created_at?: string;
}

/* ----------------------------------------------------- split links (分流連結) */

export type SplitStrategy = "random" | "time_period" | "sequential";

export interface SplitLinkTimeWindow {
  start_min: number;
  end_min: number;
  weekdays?: number[];
}

export interface SplitLinkTarget {
  channel_account_id: string;
  weight?: number | null;
  enabled?: boolean;
  daily_cap?: number | null;
  time_windows?: SplitLinkTimeWindow[] | null;
}

export interface SplitLink {
  id: string;
  name: string;
  channel_type: ChannelType;
  strategy: SplitStrategy;
  status: "active" | "disabled";
  short_url: string;
  qr_url: string;
  target_count: number;
  click_count: number;
  created_at: string;
  targets?: SplitLinkTarget[];
  prefill_text?: string | null;
}

export interface SplitLinkClickSeriesPoint {
  date: string;
  clicks: number;
  target_idx: number;
}

export interface SplitLinkClickStats {
  series: SplitLinkClickSeriesPoint[];
  total: number;
}

/* --------------------------------------------------------------- EDM 代發 */

export type EdmProvider = "smtp" | "ses" | "sendgrid" | "edm_provider";

export interface EdmCampaign {
  id: string;
  name: string;
  provider: EdmProvider;
  segment_id: string;
  template_id: string;
  schedule: BroadcastSchedule;
  status: BroadcastStatus;
  planned_count?: number;
  sent_count?: number;
  delivered_count?: number;
  created_at: string;
}

/* ============================================================ P3: reports */

export type ReportInterval = "hour" | "day" | "week" | "month";

export interface ReportFilters {
  from?: string;
  to?: string;
  interval?: ReportInterval;
  channel_type?: ChannelType | null;
  channel_account_id?: string | null;
  member_id?: string | null;
}

export interface ServiceOverviewReport {
  kpis: {
    new_conversations_today: number;
    in_progress: number;
    online_members: number;
  };
  trend: { ts: string; conversations: number }[];
}

export type CustomerDimension =
  | "member"
  | "channel"
  | "account"
  | "day"
  | "week"
  | "month"
  | "hour";

export interface CustomersReport {
  kpis: { new: number; new_deduped: number; repeat: number };
  trend: { date: string; new: number; new_deduped: number; repeat: number }[];
  detail: { dimension: CustomerDimension; rows: Record<string, unknown>[] };
}

export interface OnlineTimeRow {
  member_id: string;
  display_name: string;
  online_seconds: number;
  [k: string]: unknown;
}
export interface OnlineTimeReport {
  rows: OnlineTimeRow[];
}

export interface SummaryAgentRow {
  member_id: string;
  display_name?: string | null;
  msgs: number;
  convs: number;
  frt_avg_ms: number;
  csat_avg: number;
  resolution_avg_ms: number;
  online_seconds: number;
}
export interface SummaryReport {
  agents: SummaryAgentRow[];
}

export interface ChannelsReportRow {
  channel_type: ChannelType;
  conversations: number;
  messages_in: number;
  messages_out: number;
  [k: string]: unknown;
}
export interface ChannelsReport {
  rows: ChannelsReportRow[];
}

export interface AdsReport {
  rows: Record<string, unknown>[];
}

export interface AiSummaryReport {
  day: string;
  text: string;
}

export interface ReportExportJob {
  job_id: string;
}
export interface ReportExportStatus {
  status: "pending" | "processing" | "ready" | "failed";
  url?: string | null;
}
export interface ReportShare {
  token: string;
  url: string;
}

/* ============================================================ P3: billing */

export interface PlanLimits {
  seats?: number;
  official_channels?: number;
  hosted_devices?: number;
  monthly_active_contacts?: number;
  monthly_replies?: number;
  ai_points?: number;
  history_days?: number;
  brand_removal?: boolean;
  api_access?: boolean;
  [k: string]: unknown;
}

export interface Plan {
  code: string; // free / pro / max / custom
  name: string;
  price_monthly: number;
  limits: PlanLimits;
  is_public: boolean;
}

export interface SubscriptionAddons {
  seats: number;
  official_channels: number;
  hosted_devices: number;
}

export interface Subscription {
  plan_code: string;
  status: string; // active / trialing / past_due / canceled …
  current_period_end?: string | null;
  limits_effective: PlanLimits;
  balance: number;
  ai_points_balance: number;
  addons: SubscriptionAddons;
}

export type CheckoutDuration = 7 | 30 | 90 | 180 | 360 | 720;

export interface OrderPreview {
  base_price: number;
  discount: number;
  handling_fee: number;
  balance_applied: number;
  amount_due: number;
  currency: string;
}

export interface StripeIntent {
  client_secret?: string | null;
  checkout_url?: string | null;
}

export interface CheckoutResult {
  order: OrderPreview;
  stripe: StripeIntent;
}

/* --------------------------------------------------- platform stripe config
 * Super-admin platform payment settings. GET never echoes secrets — it returns
 * the publishable key (public by nature) plus booleans marking whether each
 * secret is stored. PUT accepts the raw keys, encrypted server-side at rest;
 * omitted/blank secret fields leave the stored value unchanged. */
export interface StripeConfig {
  publishable_key: string | null;
  secret_key_set: boolean;
  webhook_secret_set: boolean;
  configured: boolean;
}

export interface StripeConfigUpdate {
  secret_key?: string;
  publishable_key?: string;
  webhook_secret?: string;
}

export interface PointsTopupResult {
  stripe: StripeIntent;
  price: number;
}

export interface PointsLedgerEntry {
  delta: number;
  reason: string;
  ref?: string | null;
  balance_after: number;
  created_at: string;
}
export interface PointsLedgerPage {
  items: PointsLedgerEntry[];
  next_cursor?: string | null;
}

export interface Invoice {
  id: string;
  number?: string | null;
  amount: number;
  currency: string;
  status: string;
  created_at: string;
  url?: string | null;
  description?: string | null;
}
