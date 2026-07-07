/** 自動化 › AI 成員 — list + create/edit drawer. An AI member is a
 *  workspace_member(type=ai_agent) with a persona, KB picker, skill toggles,
 *  monthly quota, builtin/external mode and escalation rules (plan 附錄 B.2).
 *  It appears in team assignment like a human member. */
import { DeleteOutlined, EditOutlined, PlusOutlined, RobotOutlined } from "@ant-design/icons";
import {
  App,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Segmented,
  Select,
  Skeleton,
  Switch,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { aiApi, kbApi, intentsApi } from "@/api/endpoints";
import type { AiAgent, AiMode } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { TRANSLATE_LANGS } from "@/constants/channels";
import { t } from "@/i18n";

interface FormShape {
  name: string;
  mode: AiMode;
  model_tier: "fast" | "smart";
  enabled: boolean;
  monthly_quota: number;
  persona: { role_prompt: string; tone: string; languages: string[]; greeting: string; refuse_topics: string[] };
  skills: { kb_answer: boolean; product_card: boolean; lead_capture: boolean; handoff: boolean };
  kb_collection_ids: string[];
  escalation: { keywords: string[]; intent_ids: string[]; max_kb_misses: number; outside_hours: boolean };
  webhook_url?: string;
  webhook_secret?: string;
}

const DEFAULTS: FormShape = {
  name: "",
  mode: "builtin",
  model_tier: "fast",
  enabled: true,
  monthly_quota: 1000,
  persona: { role_prompt: "", tone: "friendly", languages: [], greeting: "", refuse_topics: [] },
  skills: { kb_answer: true, product_card: true, lead_capture: false, handoff: true },
  kb_collection_ids: [],
  escalation: { keywords: [], intent_ids: [], max_kb_misses: 3, outside_hours: false },
  webhook_url: "",
  webhook_secret: "",
};

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 13, fontWeight: 600, color: "var(--sc-text-heading)", margin: "6px 0 12px" }}>
      {children}
    </div>
  );
}

function AiMemberDrawer({
  open,
  editing,
  onClose,
}: {
  open: boolean;
  editing: AiAgent | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<FormShape>();
  const mode = Form.useWatch("mode", form);

  const collections = useQuery({
    queryKey: ["kb-collections"],
    queryFn: () => kbApi.collections(),
    enabled: open,
    retry: 1,
  });
  const intents = useQuery({
    queryKey: ["intents"],
    queryFn: () => intentsApi.list(),
    enabled: open,
    retry: 1,
  });

  useEffect(() => {
    if (open) {
      form.setFieldsValue(
        editing
          ? {
              ...DEFAULTS,
              ...editing,
              persona: { ...DEFAULTS.persona, ...editing.persona },
              skills: { ...DEFAULTS.skills, ...editing.skills },
              escalation: { ...DEFAULTS.escalation, ...editing.escalation },
              webhook_url: editing.webhook_url ?? "",
              webhook_secret: editing.webhook_secret ?? "",
            }
          : DEFAULTS,
      );
    }
  }, [open, editing, form]);

  const save = useMutation({
    mutationFn: (v: FormShape) =>
      editing ? aiApi.updateAgent(editing.id, v) : aiApi.createAgent(v),
    onSuccess: () => {
      message.success(t("ai.saved"));
      void qc.invalidateQueries({ queryKey: ["ai-agents"] });
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Drawer
      title={editing ? t("ai.drawer.editTitle") : t("ai.drawer.createTitle")}
      open={open}
      onClose={onClose}
      width={480}
      extra={
        <Button type="primary" loading={save.isPending} onClick={() => form.submit()}>
          {t("common.save")}
        </Button>
      }
    >
      <Form<FormShape> form={form} layout="vertical" onFinish={(v) => save.mutate(v)} initialValues={DEFAULTS}>
        <SectionTitle>{t("ai.section.basic")}</SectionTitle>
        <Form.Item name="name" label={t("ai.field.name")} rules={[{ required: true, message: t("common.required") }]}>
          <Input placeholder="Chilling AI" />
        </Form.Item>
        <Form.Item name="mode" label={t("ai.field.mode")}>
          <Segmented
            block
            options={[
              { value: "builtin", label: t("ai.members.mode.builtin") },
              { value: "external", label: t("ai.members.mode.external") },
            ]}
          />
        </Form.Item>
        <Form.Item name="model_tier" label={t("ai.field.modelTier")}>
          <Select
            options={[
              { value: "fast", label: t("ai.model.fast") },
              { value: "smart", label: t("ai.model.smart") },
            ]}
          />
        </Form.Item>
        <Form.Item name="enabled" label={t("ai.members.enabled")} valuePropName="checked">
          <Switch />
        </Form.Item>

        {mode === "external" ? (
          <>
            <SectionTitle>{t("ai.section.webhook")}</SectionTitle>
            <Form.Item name="webhook_url" label={t("ai.webhook.url")}>
              <Input placeholder="https://" />
            </Form.Item>
            <Form.Item name="webhook_secret" label={t("ai.webhook.secret")}>
              <Input.Password />
            </Form.Item>
            <div className="sc-fe-hint" style={{ marginTop: -8, marginBottom: 12 }}>
              {t("ai.webhook.hint")}
            </div>
          </>
        ) : (
          <>
            <SectionTitle>{t("ai.section.persona")}</SectionTitle>
            <Form.Item name={["persona", "role_prompt"]} label={t("ai.field.rolePrompt")}>
              <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} placeholder={t("ai.field.rolePromptPh")} />
            </Form.Item>
            <Form.Item name={["persona", "tone"]} label={t("ai.field.tone")}>
              <Select
                options={[
                  { value: "friendly", label: t("ai.tone.friendly") },
                  { value: "professional", label: t("ai.tone.professional") },
                  { value: "concise", label: t("ai.tone.concise") },
                  { value: "warm", label: t("ai.tone.warm") },
                ]}
              />
            </Form.Item>
            <Form.Item name={["persona", "languages"]} label={t("ai.field.languages")} extra={t("ai.field.languagesHint")}>
              <Select mode="multiple" options={TRANSLATE_LANGS} placeholder={t("ai.field.languagesHint")} />
            </Form.Item>
            <Form.Item name={["persona", "greeting"]} label={t("ai.field.greeting")}>
              <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} />
            </Form.Item>
            <Form.Item name={["persona", "refuse_topics"]} label={t("ai.field.refuseTopics")}>
              <Select mode="tags" tokenSeparators={[",", "，"]} placeholder={t("ai.field.refuseTopicsPh")} open={false} suffixIcon={null} />
            </Form.Item>

            <SectionTitle>{t("ai.section.kb")}</SectionTitle>
            <Form.Item name="kb_collection_ids" label={t("ai.field.kbPick")} extra={t("ai.field.kbPickHint")}>
              <Select
                mode="multiple"
                loading={collections.isLoading}
                options={(collections.data ?? []).map((c) => ({ value: c.id, label: c.name }))}
                placeholder={t("ai.field.kbPick")}
              />
            </Form.Item>

            <SectionTitle>{t("ai.section.skills")}</SectionTitle>
            {(
              [
                ["kb_answer", t("ai.skill.kbAnswer")],
                ["product_card", t("ai.skill.productCard")],
                ["lead_capture", t("ai.skill.leadCapture")],
                ["handoff", t("ai.skill.handoff")],
              ] as const
            ).map(([key, label]) => (
              <Form.Item
                key={key}
                name={["skills", key]}
                label={label}
                valuePropName="checked"
                style={{ marginBottom: 8 }}
              >
                <Switch />
              </Form.Item>
            ))}

            <SectionTitle>{t("ai.section.escalation")}</SectionTitle>
            <Form.Item name={["escalation", "keywords"]} label={t("ai.esc.keywords")}>
              <Select mode="tags" tokenSeparators={[",", "，"]} placeholder={t("ai.esc.keywordsPh")} open={false} suffixIcon={null} />
            </Form.Item>
            <Form.Item name={["escalation", "intent_ids"]} label={t("ai.esc.intents")}>
              <Select
                mode="multiple"
                loading={intents.isLoading}
                options={(intents.data ?? []).map((it) => ({ value: it.id, label: it.name }))}
              />
            </Form.Item>
            <Form.Item name={["escalation", "max_kb_misses"]} label={t("ai.esc.maxKbMiss")}>
              <InputNumber min={0} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name={["escalation", "outside_hours"]} label={t("ai.esc.outsideHours")} valuePropName="checked">
              <Switch />
            </Form.Item>
          </>
        )}

        <SectionTitle>{t("ai.section.quota")}</SectionTitle>
        <Form.Item name="monthly_quota" label={t("ai.members.quota")}>
          <InputNumber min={0} style={{ width: "100%" }} />
        </Form.Item>
      </Form>
    </Drawer>
  );
}

export function AiMembersPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<AiAgent | null>(null);

  const agents = useQuery({
    queryKey: ["ai-agents"],
    queryFn: () => aiApi.listAgents(),
    retry: 1,
  });

  const remove = useMutation({
    mutationFn: (id: string) => aiApi.removeAgent(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["ai-agents"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const openCreate = () => {
    setEditing(null);
    setDrawerOpen(true);
  };
  const openEdit = (a: AiAgent) => {
    setEditing(a);
    setDrawerOpen(true);
  };

  const columns: ColumnsType<AiAgent> = [
    {
      title: t("ai.col.name"),
      render: (_, r) => (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              background: "var(--sc-primary-bg)",
              color: "var(--sc-primary)",
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 16,
            }}
          >
            <RobotOutlined />
          </span>
          <div>
            <div style={{ fontWeight: 600 }}>{r.name}</div>
            <Tag color={r.enabled ? "green" : "default"} style={{ marginTop: 2 }}>
              {r.enabled ? t("common.enabled") : t("common.disabled")}
            </Tag>
          </div>
        </div>
      ),
    },
    {
      title: t("ai.col.mode"),
      width: 120,
      render: (_, r) => (
        <Tag color={r.mode === "external" ? "purple" : "blue"}>
          {r.mode === "external" ? t("ai.members.mode.external") : t("ai.members.mode.builtin")}
        </Tag>
      ),
    },
    {
      title: t("ai.col.model"),
      width: 90,
      render: (_, r) => (r.model_tier === "smart" ? t("ai.model.smart") : t("ai.model.fast")),
    },
    {
      title: t("ai.col.kb"),
      width: 80,
      align: "right",
      render: (_, r) => r.kb_collection_ids?.length ?? 0,
    },
    {
      title: t("ai.col.quota"),
      width: 140,
      render: (_, r) => (
        <span style={{ fontVariantNumeric: "tabular-nums" }}>
          {r.used_this_month ?? 0} / {r.monthly_quota}
        </span>
      ),
    },
    {
      title: t("common.actions"),
      width: 110,
      render: (_, r) => (
        <div style={{ display: "flex", gap: 2 }}>
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(r)} />
          <Button
            type="text"
            size="small"
            danger
            icon={<DeleteOutlined />}
            onClick={() =>
              modal.confirm({
                title: t("common.confirmDeleteTitle"),
                content: r.name,
                okText: t("common.delete"),
                okButtonProps: { danger: true },
                cancelText: t("common.cancel"),
                onOk: () => remove.mutate(r.id),
              })
            }
          />
        </div>
      ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("ai.members.title")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          {t("ai.members.add")}
        </Button>
      </div>
      <div className="sc-page-body">
        {agents.isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : (agents.data ?? []).length === 0 ? (
          <EmptyState
            icon={<RobotOutlined />}
            title={t("ai.members.empty")}
            hint={t("ai.members.emptyHint")}
            action={
              <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                {t("ai.members.add")}
              </Button>
            }
          />
        ) : (
          <Table<AiAgent> rowKey="id" size="middle" columns={columns} dataSource={agents.data} pagination={false} />
        )}
      </div>

      <AiMemberDrawer open={drawerOpen} editing={editing} onClose={() => setDrawerOpen(false)} />
    </div>
  );
}
