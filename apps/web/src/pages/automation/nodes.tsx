/** Flow-engine node catalog — the 9 triggers / 7 conditions / 15 actions from
 *  the实測 SaleSmartly editor (plan 附錄 B.1). Each entry carries a distinct
 *  icon + accent color, a default config factory, and a short zh-Hant label /
 *  description (kept inline as data, mirroring constants/channels.ts). Port
 *  layout lives in graph.ts (`portsOf`) because it depends on live config. */
import {
  AppstoreOutlined,
  ApiOutlined,
  BulbOutlined,
  CalendarOutlined,
  CheckCircleOutlined,
  CloudServerOutlined,
  CustomerServiceOutlined,
  EditOutlined,
  EnvironmentOutlined,
  ExpandAltOutlined,
  FieldTimeOutlined,
  ForkOutlined,
  GiftOutlined,
  GlobalOutlined,
  HourglassOutlined,
  IdcardOutlined,
  InboxOutlined,
  MailOutlined,
  MessageOutlined,
  MobileOutlined,
  QuestionCircleOutlined,
  SendOutlined,
  SolutionOutlined,
  StarOutlined,
  StopOutlined,
  TagOutlined,
  TagsOutlined,
  TranslationOutlined,
  UserAddOutlined,
  UserOutlined,
  UserSwitchOutlined,
} from "@ant-design/icons";
import type { ReactNode } from "react";
import type { NodeCategory } from "@/api/types";

export interface NodeMeta {
  kind: string;
  category: NodeCategory;
  label: string;
  desc: string;
  icon: ReactNode;
  accent: string;
  /** No outgoing ports — the flow ends here. */
  terminal?: boolean;
  defaultConfig: () => Record<string, unknown>;
}

let seq = 0;
/** Short unique id for buttons / branches / extract rows inside a config. */
export function subId(prefix = "b"): string {
  seq += 1;
  return `${prefix}_${Date.now().toString(36)}${seq.toString(36)}`;
}

/* ------------------------------------------------------------- triggers (9) */

const TRIGGERS: NodeMeta[] = [
  {
    kind: "trigger.new_visitor",
    category: "trigger",
    label: "新訪客",
    desc: "首次到訪的訪客",
    icon: <UserAddOutlined />,
    accent: "#16A34A",
    defaultConfig: () => ({ once_per_contact: true, cooldown_minutes: 0 }),
  },
  {
    kind: "trigger.returning_visitor",
    category: "trigger",
    label: "舊訪客",
    desc: "曾到訪過的回訪客",
    icon: <UserSwitchOutlined />,
    accent: "#0D9488",
    defaultConfig: () => ({ once_per_contact: false, cooldown_minutes: 0 }),
  },
  {
    kind: "trigger.visitor_message",
    category: "trigger",
    label: "訪客發消息",
    desc: "命中關鍵詞或任意消息",
    icon: <MessageOutlined />,
    accent: "#059669",
    defaultConfig: () => ({
      match_mode: "keyword",
      match_type: "fuzzy",
      keyword_groups: [{ id: subId("g"), keywords: [] as string[] }],
      dict_ids: [] as string[],
      once_per_contact: false,
      cooldown_minutes: 0,
    }),
  },
  {
    kind: "trigger.visitor_intent",
    category: "trigger",
    label: "訪客意圖識別",
    desc: "由 AI 判斷訪客意圖",
    icon: <BulbOutlined />,
    accent: "#7C3AED",
    defaultConfig: () => ({ intent_ids: [] as string[], cooldown_minutes: 0 }),
  },
  {
    kind: "trigger.widget_opened",
    category: "trigger",
    label: "聊天窗口展開",
    desc: "訪客打開聊天外掛",
    icon: <ExpandAltOutlined />,
    accent: "#0891B2",
    defaultConfig: () => ({ once_per_contact: false, cooldown_minutes: 0 }),
  },
  {
    kind: "trigger.page_visited",
    category: "trigger",
    label: "訪問特定頁面",
    desc: "訪客瀏覽指定網址",
    icon: <GlobalOutlined />,
    accent: "#0284C7",
    defaultConfig: () => ({ url_match: "contains", url: "", once_per_contact: false }),
  },
  {
    kind: "trigger.lead_submitted",
    category: "trigger",
    label: "訪客留資",
    desc: "訪客提交留資表單",
    icon: <SolutionOutlined />,
    accent: "#65A30D",
    defaultConfig: () => ({ cooldown_minutes: 0 }),
  },
  {
    kind: "trigger.agent_timeout",
    category: "trigger",
    label: "客服超時未回復",
    desc: "客服逾時未回覆訪客",
    icon: <CustomerServiceOutlined />,
    accent: "#D97706",
    defaultConfig: () => ({ minutes: 5 }),
  },
  {
    kind: "trigger.visitor_timeout",
    category: "trigger",
    label: "訪客超時未回復",
    desc: "訪客逾時未回覆",
    icon: <HourglassOutlined />,
    accent: "#EA580C",
    defaultConfig: () => ({ minutes: 10 }),
  },
];

/* ----------------------------------------------------------- conditions (7) */

const CONDITIONS: NodeMeta[] = [
  {
    kind: "cond.language",
    category: "condition",
    label: "訪客語言",
    desc: "依訪客語言分流",
    icon: <TranslationOutlined />,
    accent: "#7C3AED",
    defaultConfig: () => ({ branches: [{ id: subId(), lang: "" }] }),
  },
  {
    kind: "cond.country",
    category: "condition",
    label: "國家/地區",
    desc: "依訪客所在地分流",
    icon: <EnvironmentOutlined />,
    accent: "#C026D3",
    defaultConfig: () => ({ branches: [{ id: subId(), countries: [] as string[] }] }),
  },
  {
    kind: "cond.time_window",
    category: "condition",
    label: "自動執行時段",
    desc: "依當前時段分流",
    icon: <CalendarOutlined />,
    accent: "#D97706",
    defaultConfig: () => ({
      tz: "",
      branches: [{ id: subId(), days: [1, 2, 3, 4, 5], start: "09:00", end: "18:00" }],
    }),
  },
  {
    kind: "cond.random",
    category: "condition",
    label: "隨機分支",
    desc: "A/B 加權隨機分流",
    icon: <ForkOutlined />,
    accent: "#DB2777",
    defaultConfig: () => ({
      branches: [
        { id: subId(), weight: 50 },
        { id: subId(), weight: 50 },
      ],
    }),
  },
  {
    kind: "cond.device",
    category: "condition",
    label: "訪問設備",
    desc: "依訪客裝置分流",
    icon: <MobileOutlined />,
    accent: "#4F46E5",
    defaultConfig: () => ({ branches: [{ id: subId(), device: "desktop" }] }),
  },
  {
    kind: "cond.contact_attribute",
    category: "condition",
    label: "客戶屬性/行為",
    desc: "依客戶欄位條件分流",
    icon: <IdcardOutlined />,
    accent: "#9333EA",
    defaultConfig: () => ({ branches: [{ id: subId(), field: "", op: "eq", value: "" }] }),
  },
  {
    kind: "cond.external_variable",
    category: "condition",
    label: "外部請求變數",
    desc: "依外部請求回傳值分流",
    icon: <ApiOutlined />,
    accent: "#0D9488",
    defaultConfig: () => ({ var: "", branches: [{ id: subId(), op: "eq", value: "" }] }),
  },
];

/* -------------------------------------------------------------- actions (15) */

const ACTIONS: NodeMeta[] = [
  {
    kind: "action.send_message",
    category: "action",
    label: "發送消息",
    desc: "發送文字或商品卡片",
    icon: <SendOutlined />,
    accent: "#2C5CE6",
    defaultConfig: () => ({ blocks: [{ kind: "text", text: "" }] }),
  },
  {
    kind: "action.ask_question",
    category: "action",
    label: "問詢",
    desc: "提問並將答案存入變數",
    icon: <QuestionCircleOutlined />,
    accent: "#2563EB",
    defaultConfig: () => ({ question: "", save_to: "vars.answer", timeout_minutes: 10 }),
  },
  {
    kind: "action.send_email",
    category: "action",
    label: "發送郵件",
    desc: "向訪客發送郵件",
    icon: <MailOutlined />,
    accent: "#0EA5E9",
    defaultConfig: () => ({ subject: "", body: "" }),
  },
  {
    kind: "action.quick_buttons",
    category: "action",
    label: "快捷按鈕",
    desc: "每個按鈕一個分支出口",
    icon: <AppstoreOutlined />,
    accent: "#3D6CEB",
    defaultConfig: () => ({
      text: "",
      buttons: [{ id: subId("btn"), text: "" }],
      timeout_minutes: 10,
    }),
  },
  {
    kind: "action.add_tag",
    category: "action",
    label: "添加訪客標籤",
    desc: "為訪客加上標籤",
    icon: <TagOutlined />,
    accent: "#6366F1",
    defaultConfig: () => ({ tag_ids: [] as string[] }),
  },
  {
    kind: "action.add_conversation_tag",
    category: "action",
    label: "新增會話標籤",
    desc: "為當前會話加上標籤",
    icon: <TagsOutlined />,
    accent: "#818CF8",
    defaultConfig: () => ({ tag_ids: [] as string[] }),
  },
  {
    kind: "action.delay",
    category: "action",
    label: "延時等候",
    desc: "等待一段時間再繼續",
    icon: <FieldTimeOutlined />,
    accent: "#64748B",
    defaultConfig: () => ({ value: 1, unit: "minutes" }),
  },
  {
    kind: "action.request_rating",
    category: "action",
    label: "邀請評價",
    desc: "邀請訪客為服務評分",
    icon: <StarOutlined />,
    accent: "#F59E0B",
    defaultConfig: () => ({ prompt: "" }),
  },
  {
    kind: "action.promo_card",
    category: "action",
    label: "推廣卡片",
    desc: "發送推廣圖文卡片",
    icon: <GiftOutlined />,
    accent: "#DB2777",
    defaultConfig: () => ({
      card: { title: "", subtitle: "", image_url: "", url: "", buttons: [] as unknown[] },
    }),
  },
  {
    kind: "action.transfer_unassigned",
    category: "action",
    label: "轉未分配會話",
    desc: "將會話轉入待分配池",
    icon: <InboxOutlined />,
    accent: "#7C3AED",
    defaultConfig: () => ({}),
  },
  {
    kind: "action.assign_agent",
    category: "action",
    label: "分配客服",
    desc: "分配給指定成員或分組",
    icon: <UserOutlined />,
    accent: "#2C5CE6",
    defaultConfig: () => ({ target: "member", member_id: null, group_id: null }),
  },
  {
    kind: "action.close_conversation",
    category: "action",
    label: "結束會話",
    desc: "結束當前會話（終點）",
    icon: <CheckCircleOutlined />,
    accent: "#DC2626",
    terminal: true,
    defaultConfig: () => ({}),
  },
  {
    kind: "action.external_request",
    category: "action",
    label: "外部請求",
    desc: "呼叫 HTTP API 並提取變數",
    icon: <CloudServerOutlined />,
    accent: "#0D9488",
    defaultConfig: () => ({
      method: "GET",
      url: "",
      headers: "",
      body: "",
      extract: [] as { id: string; path: string; var: string }[],
    }),
  },
  {
    kind: "action.update_contact",
    category: "action",
    label: "更新客戶資料",
    desc: "寫入客戶欄位",
    icon: <EditOutlined />,
    accent: "#0891B2",
    defaultConfig: () => ({ field: "", value: "" }),
  },
  {
    kind: "action.blacklist",
    category: "action",
    label: "加入黑名單",
    desc: "封鎖訪客並停止接待（終點）",
    icon: <StopOutlined />,
    accent: "#B91C1C",
    terminal: true,
    defaultConfig: () => ({}),
  },
];

export const NODE_GROUPS: { category: NodeCategory; items: NodeMeta[] }[] = [
  { category: "trigger", items: TRIGGERS },
  { category: "condition", items: CONDITIONS },
  { category: "action", items: ACTIONS },
];

export const NODE_CATALOG: NodeMeta[] = [...TRIGGERS, ...CONDITIONS, ...ACTIONS];

export const NODE_MAP: Record<string, NodeMeta> = Object.fromEntries(
  NODE_CATALOG.map((n) => [n.kind, n]),
);

export function metaFor(kind: string): NodeMeta | undefined {
  return NODE_MAP[kind];
}
