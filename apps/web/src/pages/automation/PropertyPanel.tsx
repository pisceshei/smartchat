/** Right-hand property panel — edits the selected node's config with a
 *  type-specific editor: keyword chips + OR groups + 詞庫 import for
 *  visitor_message, a card builder for send_message, branch configs for
 *  conditions, HTTP config for external_request, etc. Emits a full replacement
 *  config object so the editor state stays immutable. */
import { CloseOutlined, DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { App, Button, Checkbox, Divider, Input, InputNumber, Select, Space, Switch } from "antd";
import { useQuery } from "@tanstack/react-query";
import type { FlowNode } from "@/api/types";
import { groupsApi, intentsApi, keywordDictsApi, membersApi, tagsApi } from "@/api/endpoints";
import { TRANSLATE_LANGS } from "@/constants/channels";
import { t } from "@/i18n";
import { metaFor, subId } from "./nodes";

type Cfg = Record<string, unknown>;

interface Props {
  node: FlowNode;
  onConfig: (cfg: Cfg) => void;
  onTitle: (title: string) => void;
  onDelete: () => void;
}

function Field({
  label,
  hint,
  children,
}: {
  label?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="sc-fe-field">
      {label && <label className="sc-fe-label">{label}</label>}
      {children}
      {hint && <div className="sc-fe-hint">{hint}</div>}
    </div>
  );
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 7] as const;

export function PropertyPanel({ node, onConfig, onTitle, onDelete }: Props) {
  const { message } = App.useApp();
  const meta = metaFor(node.kind);
  const c = node.config ?? {};
  const set = (patch: Cfg) => onConfig({ ...c, ...patch });

  const visitorTags = useQuery({
    queryKey: ["tags", "visitor"],
    queryFn: () => tagsApi.list("visitor"),
    staleTime: 60_000,
    retry: 1,
  });
  const convTags = useQuery({
    queryKey: ["tags", "conversation"],
    queryFn: () => tagsApi.list("conversation"),
    staleTime: 60_000,
    retry: 1,
  });
  const members = useQuery({
    queryKey: ["members"],
    queryFn: () => membersApi.list(),
    staleTime: 60_000,
    retry: 1,
  });
  const groups = useQuery({
    queryKey: ["member-groups"],
    queryFn: () => groupsApi.list(),
    staleTime: 60_000,
    retry: 1,
  });
  const intents = useQuery({
    queryKey: ["intents"],
    queryFn: () => intentsApi.list(),
    staleTime: 60_000,
    retry: 1,
  });
  const dicts = useQuery({
    queryKey: ["keyword-dicts"],
    queryFn: () => keywordDictsApi.list(),
    staleTime: 60_000,
    retry: 1,
  });

  /* ---- keyword groups (visitor_message) ---- */
  const renderKeywordGroups = () => {
    const groupsCfg = (c.keyword_groups as { id: string; keywords: string[] }[]) ?? [];
    const patchGroup = (id: string, keywords: string[]) =>
      set({ keyword_groups: groupsCfg.map((g) => (g.id === id ? { ...g, keywords } : g)) });
    return (
      <Field label={t("nc.keywordGroups")}>
        {groupsCfg.map((g, i) => (
          <div key={g.id}>
            {i > 0 && (
              <div className="sc-fe-hint" style={{ textAlign: "center", margin: "2px 0" }}>
                — 或 —
              </div>
            )}
            <div className="sc-kw-group">
              <Select
                mode="tags"
                size="small"
                style={{ width: "100%" }}
                value={g.keywords}
                onChange={(v) => patchGroup(g.id, v)}
                placeholder={t("nc.addKeyword")}
                tokenSeparators={[",", "，", " "]}
                open={false}
                suffixIcon={null}
              />
              {groupsCfg.length > 1 && (
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<CloseOutlined />}
                  style={{ marginTop: 4 }}
                  onClick={() => set({ keyword_groups: groupsCfg.filter((x) => x.id !== g.id) })}
                />
              )}
            </div>
          </div>
        ))}
        <Button
          type="dashed"
          size="small"
          icon={<PlusOutlined />}
          block
          onClick={() => set({ keyword_groups: [...groupsCfg, { id: subId("g"), keywords: [] }] })}
        >
          {t("nc.addGroup")}
        </Button>
        <div style={{ marginTop: 8 }}>
          <label className="sc-fe-label">{t("nc.importDict")}</label>
          <Select
            mode="multiple"
            size="small"
            style={{ width: "100%" }}
            value={(c.dict_ids as string[]) ?? []}
            onChange={(v) => set({ dict_ids: v })}
            placeholder={t("nc.importDict")}
            loading={dicts.isLoading}
            options={(dicts.data ?? []).map((d) => ({
              value: d.id,
              label: `${d.name}${d.item_count != null ? `（${d.item_count}）` : ""}`,
            }))}
          />
        </div>
      </Field>
    );
  };

  /* ---- generic branch editor (conditions) ---- */
  const renderBranches = () => {
    const branches = (c.branches as Record<string, unknown>[]) ?? [];
    const patchBranch = (id: string, patch: Cfg) =>
      set({ branches: branches.map((b) => (b.id === id ? { ...b, ...patch } : b)) });
    const removeBranch = (id: string) =>
      set({ branches: branches.filter((b) => b.id !== id) });
    const addBranch = () => {
      const base = meta?.defaultConfig() as { branches?: Record<string, unknown>[] };
      const template = base?.branches?.[0] ?? {};
      set({ branches: [...branches, { ...template, id: subId() }] });
    };

    return (
      <Field label={t("nc.branches")} hint={t("fe.port.else") + "：未命中任一分支時走此出口"}>
        {branches.map((b, i) => {
          const id = b.id as string;
          return (
            <div key={id} className="sc-branch-row">
              <div className="sc-branch-row-head">
                <span>
                  {t("nc.branches")} {i + 1}
                </span>
                {branches.length > 1 && (
                  <Button
                    type="text"
                    size="small"
                    danger
                    icon={<CloseOutlined />}
                    onClick={() => removeBranch(id)}
                  />
                )}
              </div>
              {renderBranchFields(b, (patch) => patchBranch(id, patch))}
            </div>
          );
        })}
        <Button type="dashed" size="small" icon={<PlusOutlined />} block onClick={addBranch}>
          {t("nc.addBranch")}
        </Button>
      </Field>
    );
  };

  const renderBranchFields = (b: Record<string, unknown>, patch: (p: Cfg) => void) => {
    switch (node.kind) {
      case "cond.language":
        return (
          <Select
            size="small"
            style={{ width: "100%" }}
            value={(b.lang as string) || undefined}
            onChange={(v) => patch({ lang: v })}
            placeholder={t("nc.language")}
            options={TRANSLATE_LANGS}
            showSearch
            optionFilterProp="label"
          />
        );
      case "cond.country":
        return (
          <Select
            mode="tags"
            size="small"
            style={{ width: "100%" }}
            value={(b.countries as string[]) ?? []}
            onChange={(v) => patch({ countries: v })}
            placeholder={t("nc.countries")}
            tokenSeparators={[",", " "]}
          />
        );
      case "cond.device":
        return (
          <Select
            size="small"
            style={{ width: "100%" }}
            value={(b.device as string) || "desktop"}
            onChange={(v) => patch({ device: v })}
            options={[
              { value: "desktop", label: t("nc.device.desktop") },
              { value: "mobile", label: t("nc.device.mobile") },
              { value: "tablet", label: t("nc.device.tablet") },
            ]}
          />
        );
      case "cond.random":
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className="sc-fe-hint" style={{ margin: 0 }}>
              {t("nc.weight")}
            </span>
            <InputNumber
              size="small"
              min={0}
              max={100}
              value={b.weight as number}
              onChange={(v) => patch({ weight: v ?? 0 })}
              addonAfter="%"
              style={{ width: 110 }}
            />
          </div>
        );
      case "cond.time_window":
        return (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Checkbox.Group
              value={(b.days as number[]) ?? []}
              onChange={(v) => patch({ days: v })}
              options={WEEKDAYS.map((d) => ({
                value: d,
                label: t(`shifts.weekday.${d}` as never),
              }))}
            />
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <Input
                size="small"
                style={{ width: 90 }}
                value={(b.start as string) ?? ""}
                onChange={(e) => patch({ start: e.target.value })}
                placeholder="09:00"
              />
              <span>–</span>
              <Input
                size="small"
                style={{ width: 90 }}
                value={(b.end as string) ?? ""}
                onChange={(e) => patch({ end: e.target.value })}
                placeholder="18:00"
              />
            </div>
          </Space>
        );
      case "cond.contact_attribute":
        return (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Input
              size="small"
              value={(b.field as string) ?? ""}
              onChange={(e) => patch({ field: e.target.value })}
              placeholder={t("nc.attrField")}
            />
            <div style={{ display: "flex", gap: 6 }}>
              <Select
                size="small"
                style={{ width: 100 }}
                value={(b.op as string) || "eq"}
                onChange={(v) => patch({ op: v })}
                options={OP_OPTIONS}
              />
              <Input
                size="small"
                style={{ flex: 1 }}
                value={(b.value as string) ?? ""}
                onChange={(e) => patch({ value: e.target.value })}
                placeholder={t("nc.attrValue")}
              />
            </div>
          </Space>
        );
      case "cond.external_variable":
        return (
          <div style={{ display: "flex", gap: 6 }}>
            <Select
              size="small"
              style={{ width: 100 }}
              value={(b.op as string) || "eq"}
              onChange={(v) => patch({ op: v })}
              options={OP_OPTIONS}
            />
            <Input
              size="small"
              style={{ flex: 1 }}
              value={(b.value as string) ?? ""}
              onChange={(e) => patch({ value: e.target.value })}
              placeholder={t("nc.attrValue")}
            />
          </div>
        );
      default:
        return null;
    }
  };

  /* ---- send_message blocks ---- */
  const renderMessageBlocks = () => {
    const blocks = (c.blocks as Record<string, unknown>[]) ?? [];
    const patchBlock = (i: number, patch: Cfg) =>
      set({ blocks: blocks.map((b, j) => (j === i ? { ...b, ...patch } : b)) });
    const removeBlock = (i: number) => set({ blocks: blocks.filter((_, j) => j !== i) });
    return (
      <Field label={t("nc.message")} hint={t("nc.varHint")}>
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {blocks.map((b, i) =>
            b.kind === "text" ? (
              <div key={i} style={{ display: "flex", gap: 6 }}>
                <Input.TextArea
                  size="small"
                  autoSize={{ minRows: 2, maxRows: 6 }}
                  value={(b.text as string) ?? ""}
                  onChange={(e) => patchBlock(i, { text: e.target.value })}
                  placeholder={t("nc.message")}
                />
                <Button type="text" size="small" danger icon={<CloseOutlined />} onClick={() => removeBlock(i)} />
              </div>
            ) : (
              <div key={i} className="sc-branch-row">
                <div className="sc-branch-row-head">
                  <span>{t("nc.addCardBlock")}</span>
                  <Button type="text" size="small" danger icon={<CloseOutlined />} onClick={() => removeBlock(i)} />
                </div>
                {renderCardFields(b, (patch) => patchBlock(i, patch))}
              </div>
            ),
          )}
          <Space>
            <Button
              type="dashed"
              size="small"
              icon={<PlusOutlined />}
              onClick={() => set({ blocks: [...blocks, { kind: "text", text: "" }] })}
            >
              {t("nc.addTextBlock")}
            </Button>
            <Button
              type="dashed"
              size="small"
              icon={<PlusOutlined />}
              onClick={() =>
                set({
                  blocks: [
                    ...blocks,
                    { kind: "product_card", title: "", subtitle: "", image_url: "", price: "", url: "", buttons: [] },
                  ],
                })
              }
            >
              {t("nc.addCardBlock")}
            </Button>
          </Space>
        </Space>
      </Field>
    );
  };

  const renderCardFields = (b: Record<string, unknown>, patch: (p: Cfg) => void) => (
    <Space direction="vertical" size={6} style={{ width: "100%" }}>
      <Input size="small" value={(b.title as string) ?? ""} onChange={(e) => patch({ title: e.target.value })} placeholder={t("nc.card.title")} />
      <Input size="small" value={(b.subtitle as string) ?? ""} onChange={(e) => patch({ subtitle: e.target.value })} placeholder={t("nc.card.subtitle")} />
      <Input size="small" value={(b.image_url as string) ?? ""} onChange={(e) => patch({ image_url: e.target.value })} placeholder={t("nc.card.image")} />
      <div style={{ display: "flex", gap: 6 }}>
        <Input size="small" style={{ width: 110 }} value={(b.price as string) ?? ""} onChange={(e) => patch({ price: e.target.value })} placeholder={t("nc.card.price")} />
        <Input size="small" style={{ flex: 1 }} value={(b.url as string) ?? ""} onChange={(e) => patch({ url: e.target.value })} placeholder={t("nc.card.url")} />
      </div>
    </Space>
  );

  /* ---- quick_buttons ---- */
  const renderQuickButtons = () => {
    const buttons = (c.buttons as { id: string; text: string }[]) ?? [];
    return (
      <>
        <Field label={t("nc.message")}>
          <Input.TextArea
            size="small"
            autoSize={{ minRows: 2, maxRows: 4 }}
            value={(c.text as string) ?? ""}
            onChange={(e) => set({ text: e.target.value })}
          />
        </Field>
        <Field label={t("nc.buttons")}>
          {buttons.map((btn, i) => (
            <div key={btn.id} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
              <Input
                size="small"
                value={btn.text}
                onChange={(e) =>
                  set({ buttons: buttons.map((x) => (x.id === btn.id ? { ...x, text: e.target.value } : x)) })
                }
                placeholder={`${t("nc.buttonText")} ${i + 1}`}
              />
              {buttons.length > 1 && (
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<CloseOutlined />}
                  onClick={() => set({ buttons: buttons.filter((x) => x.id !== btn.id) })}
                />
              )}
            </div>
          ))}
          <Button
            type="dashed"
            size="small"
            icon={<PlusOutlined />}
            block
            onClick={() => set({ buttons: [...buttons, { id: subId("btn"), text: "" }] })}
          >
            {t("nc.addButton")}
          </Button>
        </Field>
        <Field label={t("nc.timeoutMinutes")}>
          <InputNumber
            size="small"
            min={1}
            value={c.timeout_minutes as number}
            onChange={(v) => set({ timeout_minutes: v ?? 10 })}
            style={{ width: "100%" }}
          />
        </Field>
      </>
    );
  };

  /* ---- external_request ---- */
  const renderHttp = () => {
    const extract = (c.extract as { id: string; path: string; var: string }[]) ?? [];
    return (
      <>
        <Field label={t("nc.http.method")}>
          <Select
            size="small"
            style={{ width: "100%" }}
            value={(c.method as string) || "GET"}
            onChange={(v) => set({ method: v })}
            options={["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => ({ value: m, label: m }))}
          />
        </Field>
        <Field label={t("nc.http.url")} hint={t("nc.http.ssrfHint")}>
          <Input size="small" value={(c.url as string) ?? ""} onChange={(e) => set({ url: e.target.value })} placeholder="https://" />
        </Field>
        <Field label={t("nc.http.headers")}>
          <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 5 }} value={(c.headers as string) ?? ""} onChange={(e) => set({ headers: e.target.value })} placeholder="Authorization: Bearer ..." />
        </Field>
        <Field label={t("nc.http.body")}>
          <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 6 }} value={(c.body as string) ?? ""} onChange={(e) => set({ body: e.target.value })} placeholder='{ "key": "{{contact.email}}" }' />
        </Field>
        <Field label={t("nc.http.extract")}>
          {extract.map((row) => (
            <div key={row.id} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
              <Input size="small" value={row.path} onChange={(e) => set({ extract: extract.map((x) => (x.id === row.id ? { ...x, path: e.target.value } : x)) })} placeholder="$.data.id" />
              <Input size="small" value={row.var} onChange={(e) => set({ extract: extract.map((x) => (x.id === row.id ? { ...x, var: e.target.value } : x)) })} placeholder="order_id" />
              <Button type="text" size="small" danger icon={<CloseOutlined />} onClick={() => set({ extract: extract.filter((x) => x.id !== row.id) })} />
            </div>
          ))}
          <Button type="dashed" size="small" icon={<PlusOutlined />} block onClick={() => set({ extract: [...extract, { id: subId("x"), path: "", var: "" }] })}>
            {t("nc.http.addExtract")}
          </Button>
        </Field>
      </>
    );
  };

  const tagPicker = (kind: "visitor" | "conversation") => {
    const q = kind === "visitor" ? visitorTags : convTags;
    return (
      <Field label={t("nc.tags")}>
        <Select
          mode="multiple"
          size="small"
          style={{ width: "100%" }}
          value={(c.tag_ids as string[]) ?? []}
          onChange={(v) => set({ tag_ids: v })}
          loading={q.isLoading}
          options={(q.data ?? []).map((tg) => ({ value: tg.id, label: tg.name }))}
          placeholder={t("nc.tags")}
        />
      </Field>
    );
  };

  /* --------------------------------------------------------------- body switch */
  const renderConfig = () => {
    // triggers
    if (node.kind === "trigger.visitor_message") {
      return (
        <>
          <Field label={t("nc.matchMode")}>
            <Select
              size="small"
              style={{ width: "100%" }}
              value={(c.match_mode as string) || "keyword"}
              onChange={(v) => set({ match_mode: v })}
              options={[
                { value: "keyword", label: t("nc.matchMode.keyword") },
                { value: "any", label: t("nc.matchMode.any") },
              ]}
            />
          </Field>
          {(c.match_mode ?? "keyword") === "keyword" && (
            <>
              <Field label={t("nc.matchType")}>
                <Select
                  size="small"
                  style={{ width: "100%" }}
                  value={(c.match_type as string) || "fuzzy"}
                  onChange={(v) => set({ match_type: v })}
                  options={[
                    { value: "fuzzy", label: t("nc.matchType.fuzzy") },
                    { value: "exact", label: t("nc.matchType.exact") },
                  ]}
                />
              </Field>
              {renderKeywordGroups()}
            </>
          )}
          {renderTriggerLimits()}
        </>
      );
    }
    if (node.kind === "trigger.visitor_intent") {
      return (
        <>
          <Field label={t("nc.intents")} hint={t("nc.intentHint")}>
            <Select
              mode="multiple"
              size="small"
              style={{ width: "100%" }}
              value={(c.intent_ids as string[]) ?? []}
              onChange={(v) => set({ intent_ids: v })}
              loading={intents.isLoading}
              options={(intents.data ?? []).map((it) => ({ value: it.id, label: it.name }))}
            />
          </Field>
          {renderTriggerLimits()}
        </>
      );
    }
    if (node.kind === "trigger.page_visited") {
      return (
        <>
          <Field label={t("nc.urlMatch")}>
            <Space.Compact style={{ width: "100%" }}>
              <Select
                size="small"
                style={{ width: 90 }}
                value={(c.url_match as string) || "contains"}
                onChange={(v) => set({ url_match: v })}
                options={[
                  { value: "contains", label: t("nc.urlMatch.contains") },
                  { value: "exact", label: t("nc.urlMatch.exact") },
                  { value: "regex", label: t("nc.urlMatch.regex") },
                ]}
              />
              <Input size="small" value={(c.url as string) ?? ""} onChange={(e) => set({ url: e.target.value })} placeholder="/pricing" />
            </Space.Compact>
          </Field>
          {renderTriggerLimits()}
        </>
      );
    }
    if (node.kind === "trigger.agent_timeout" || node.kind === "trigger.visitor_timeout") {
      return (
        <Field label={t("nc.timeoutMinutes")}>
          <InputNumber size="small" min={1} value={c.minutes as number} onChange={(v) => set({ minutes: v ?? 5 })} style={{ width: "100%" }} />
        </Field>
      );
    }
    if (node.category === "trigger") return renderTriggerLimits();

    // conditions
    if (node.category === "condition") {
      return (
        <>
          {node.kind === "cond.external_variable" && (
            <Field label={t("nc.http.extract")} hint={t("nc.varHint")}>
              <Input size="small" value={(c.var as string) ?? ""} onChange={(e) => set({ var: e.target.value })} placeholder="ext.node1.status" />
            </Field>
          )}
          {node.kind === "cond.time_window" && (
            <Field label="時區">
              <Input size="small" value={(c.tz as string) ?? ""} onChange={(e) => set({ tz: e.target.value })} placeholder="Asia/Hong_Kong（留空＝工作區時區）" />
            </Field>
          )}
          {renderBranches()}
        </>
      );
    }

    // actions
    switch (node.kind) {
      case "action.send_message":
        return renderMessageBlocks();
      case "action.ask_question":
        return (
          <>
            <Field label={t("nc.question")}>
              <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 5 }} value={(c.question as string) ?? ""} onChange={(e) => set({ question: e.target.value })} />
            </Field>
            <Field label={t("nc.saveTo")} hint={t("nc.saveToHint")}>
              <Input size="small" value={(c.save_to as string) ?? ""} onChange={(e) => set({ save_to: e.target.value })} placeholder="vars.answer" />
            </Field>
            <Field label={t("nc.timeoutMinutes")}>
              <InputNumber size="small" min={1} value={c.timeout_minutes as number} onChange={(v) => set({ timeout_minutes: v ?? 10 })} style={{ width: "100%" }} />
            </Field>
          </>
        );
      case "action.send_email":
        return (
          <>
            <Field label={t("nc.email.subject")}>
              <Input size="small" value={(c.subject as string) ?? ""} onChange={(e) => set({ subject: e.target.value })} />
            </Field>
            <Field label={t("nc.email.body")} hint={t("nc.varHint")}>
              <Input.TextArea size="small" autoSize={{ minRows: 3, maxRows: 8 }} value={(c.body as string) ?? ""} onChange={(e) => set({ body: e.target.value })} />
            </Field>
          </>
        );
      case "action.quick_buttons":
        return renderQuickButtons();
      case "action.add_tag":
        return tagPicker("visitor");
      case "action.add_conversation_tag":
        return tagPicker("conversation");
      case "action.delay":
        return (
          <Field label={t("nc.delay")}>
            <Space.Compact style={{ width: "100%" }}>
              <InputNumber size="small" min={1} value={c.value as number} onChange={(v) => set({ value: v ?? 1 })} style={{ width: "60%" }} />
              <Select
                size="small"
                style={{ width: "40%" }}
                value={(c.unit as string) || "minutes"}
                onChange={(v) => set({ unit: v })}
                options={[
                  { value: "minutes", label: t("nc.delayUnit.minutes") },
                  { value: "hours", label: t("nc.delayUnit.hours") },
                  { value: "days", label: t("nc.delayUnit.days") },
                ]}
              />
            </Space.Compact>
          </Field>
        );
      case "action.request_rating":
        return (
          <Field label={t("nc.rating.prompt")}>
            <Input.TextArea size="small" autoSize={{ minRows: 2, maxRows: 4 }} value={(c.prompt as string) ?? ""} onChange={(e) => set({ prompt: e.target.value })} />
          </Field>
        );
      case "action.promo_card": {
        const card = (c.card as Record<string, unknown>) ?? {};
        return (
          <Field label={t("nc.addCardBlock")}>
            {renderCardFields(card, (patch) => set({ card: { ...card, ...patch } }))}
          </Field>
        );
      }
      case "action.assign_agent":
        return (
          <>
            <Field label={t("nc.assign.target")}>
              <Select
                size="small"
                style={{ width: "100%" }}
                value={(c.target as string) || "member"}
                onChange={(v) => set({ target: v })}
                options={[
                  { value: "member", label: t("nc.assign.member") },
                  { value: "group", label: t("nc.assign.group") },
                ]}
              />
            </Field>
            {(c.target ?? "member") === "member" ? (
              <Field label={t("nc.assign.member")}>
                <Select
                  size="small"
                  style={{ width: "100%" }}
                  value={(c.member_id as string) || undefined}
                  onChange={(v) => set({ member_id: v })}
                  loading={members.isLoading}
                  options={(members.data ?? []).map((m) => ({ value: m.id, label: m.display_name }))}
                  showSearch
                  optionFilterProp="label"
                />
              </Field>
            ) : (
              <Field label={t("nc.assign.group")}>
                <Select
                  size="small"
                  style={{ width: "100%" }}
                  value={(c.group_id as string) || undefined}
                  onChange={(v) => set({ group_id: v })}
                  loading={groups.isLoading}
                  options={(groups.data ?? []).map((g) => ({ value: g.id, label: g.name }))}
                />
              </Field>
            )}
          </>
        );
      case "action.external_request":
        return renderHttp();
      case "action.update_contact":
        return (
          <>
            <Field label={t("nc.updateContact.field")}>
              <Input size="small" value={(c.field as string) ?? ""} onChange={(e) => set({ field: e.target.value })} placeholder="custom.vip_level" />
            </Field>
            <Field label={t("nc.updateContact.value")} hint={t("nc.varHint")}>
              <Input size="small" value={(c.value as string) ?? ""} onChange={(e) => set({ value: e.target.value })} />
            </Field>
          </>
        );
      case "action.close_conversation":
        return <div className="sc-fe-hint">{t("nc.terminalHint")}</div>;
      case "action.blacklist":
        return <div className="sc-fe-hint">{t("nc.blacklist.hint")}</div>;
      case "action.transfer_unassigned":
        return <div className="sc-fe-hint">將會話轉入待分配池，等待客服認領。</div>;
      default:
        return <div className="sc-fe-hint">此節點無需額外設定。</div>;
    }
  };

  const renderTriggerLimits = () => (
    <>
      <Divider style={{ margin: "8px 0 14px" }} plain>
        <span style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>{t("nc.trigger.limit")}</span>
      </Divider>
      {"once_per_contact" in (meta?.defaultConfig() ?? {}) && (
        <Field>
          <Space>
            <Switch size="small" checked={!!c.once_per_contact} onChange={(v) => set({ once_per_contact: v })} />
            <span style={{ fontSize: 13 }}>{t("nc.trigger.oncePerContact")}</span>
          </Space>
        </Field>
      )}
      <Field label={t("nc.trigger.cooldown")}>
        <InputNumber size="small" min={0} value={(c.cooldown_minutes as number) ?? 0} onChange={(v) => set({ cooldown_minutes: v ?? 0 })} style={{ width: "100%" }} />
      </Field>
    </>
  );

  return (
    <aside className="sc-fe-props">
      <div className="sc-fe-props-head">
        <span className="sc-fn-chip" style={{ background: meta?.accent ?? "var(--sc-primary)", width: 24, height: 24, fontSize: 13 }}>
          {meta?.icon}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 13.5 }}>{meta?.label ?? node.kind}</div>
          <div style={{ fontSize: 11.5, color: "var(--sc-text-tertiary)" }}>{meta?.desc}</div>
        </div>
        <Button
          type="text"
          size="small"
          danger
          icon={<DeleteOutlined />}
          onClick={() => {
            onDelete();
            message.success(t("common.deleteSuccess"));
          }}
        />
      </div>
      <div className="sc-fe-props-body">
        <Field label={t("fe.nodeTitle")}>
          <Input
            size="small"
            value={node.title ?? ""}
            onChange={(e) => onTitle(e.target.value)}
            placeholder={meta?.label}
          />
        </Field>
        <Divider style={{ margin: "4px 0 16px" }} />
        {renderConfig()}
      </div>
    </aside>
  );
}

const OP_OPTIONS = [
  { value: "eq", label: t("cust.filter.op.eq") },
  { value: "neq", label: t("cust.filter.op.neq") },
  { value: "contains", label: t("cust.filter.op.contains") },
  { value: "empty", label: t("cust.filter.op.empty") },
  { value: "not_empty", label: t("cust.filter.op.notEmpty") },
];
