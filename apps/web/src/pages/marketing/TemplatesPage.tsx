/** 行銷 › 訊息範本 — 4 channel tabs. WhatsApp API has the full builder with a
 *  live phone preview + Meta approval-status sync; Email / Messenger / SMS have
 *  their own compact editors. Contract: /msg-templates/{channel}. */
import {
  DeleteOutlined,
  LinkOutlined,
  PlusOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Drawer,
  Input,
  Modal,
  Radio,
  Select,
  Skeleton,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { channelsApi, msgTemplatesApi } from "@/api/endpoints";
import { galleryType } from "@/constants/channels";
import type {
  EmailTemplate,
  MessengerTemplate,
  SmsTemplate,
  TemplateChannel,
  WaApprovalStatus,
  WaButtonItem,
  WaButtonKind,
  WaTemplateCategory,
  WhatsAppTemplate,
} from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { SmsSignaturesModal, smsSegments } from "./SmsSignatures";
import "./marketing.css";

const WA_LANGS = [
  { value: "en", label: "English (en)" },
  { value: "en_US", label: "English US (en_US)" },
  { value: "zh_TW", label: "繁體中文 (zh_TW)" },
  { value: "zh_CN", label: "简体中文 (zh_CN)" },
  { value: "zh_HK", label: "繁體中文 香港 (zh_HK)" },
  { value: "ja", label: "日本語 (ja)" },
  { value: "ko", label: "한국어 (ko)" },
  { value: "th", label: "ไทย (th)" },
  { value: "vi", label: "Tiếng Việt (vi)" },
  { value: "id", label: "Bahasa Indonesia (id)" },
  { value: "es", label: "Español (es)" },
  { value: "pt_BR", label: "Português BR (pt_BR)" },
];

const APPROVAL_COLOR: Record<WaApprovalStatus, string> = {
  draft: "default",
  pending: "processing",
  approved: "success",
  rejected: "error",
  paused: "warning",
  disabled: "default",
};

function approvalTag(s: WaApprovalStatus) {
  return <Tag color={APPROVAL_COLOR[s]}>{t(`tpl.wa.status.${s}` as Parameters<typeof t>[0])}</Tag>;
}

export function TemplatesPage() {
  const [channel, setChannel] = useState<TemplateChannel>("whatsapp");
  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("mkt.nav.templates")}</h1>
      </div>
      <div className="sc-page-body">
        <Tabs
          activeKey={channel}
          onChange={(k) => setChannel(k as TemplateChannel)}
          items={[
            { key: "whatsapp", label: t("tpl.tab.whatsapp") },
            { key: "email", label: t("tpl.tab.email") },
            { key: "messenger", label: t("tpl.tab.messenger") },
            { key: "sms", label: t("tpl.tab.sms") },
          ]}
        />
        {channel === "whatsapp" && <WhatsAppTab />}
        {channel === "email" && <EmailTab />}
        {channel === "messenger" && <MessengerTab />}
        {channel === "sms" && <SmsTab />}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- WhatsApp */
function WhatsAppTab() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [builderOpen, setBuilderOpen] = useState(false);
  const [editing, setEditing] = useState<WhatsAppTemplate | null>(null);

  const list = useQuery({
    queryKey: ["msg-templates", "whatsapp"],
    queryFn: () => msgTemplatesApi.list("whatsapp"),
    retry: 1,
  });
  const accounts = useQuery({
    queryKey: ["channel-accounts"],
    queryFn: () => channelsApi.listAccounts(),
    retry: 1,
  });
  // whatsapp_cloud (direct Meta) AND whatsapp_bsp (YCloud proxy) both count
  // as WABA-backed accounts — the stored channel_type is backend-canonical
  const wabaAccounts = (accounts.data ?? []).filter(
    (a) => galleryType(a.channel_type) === "whatsapp_api",
  );

  const sync = useMutation({
    mutationFn: async () => {
      if (!wabaAccounts.length) throw new Error("no-account");
      const results = await Promise.allSettled(
        wabaAccounts.map((a) => msgTemplatesApi.syncWhatsapp(a.id)),
      );
      const synced = results.reduce(
        (n, r) => n + (r.status === "fulfilled" ? r.value.synced : 0),
        0,
      );
      if (results.every((r) => r.status === "rejected")) throw new Error("all-failed");
      return { synced };
    },
    onSuccess: (r) => {
      message.success(t("tpl.synced", { count: r.synced }));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "whatsapp"] });
    },
    onError: () => message.error(t("tpl.syncFailed")),
  });

  const submit = useMutation({
    mutationFn: (id: string) => msgTemplatesApi.submitWhatsapp(id),
    onSuccess: () => {
      message.success(t("tpl.submitted"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "whatsapp"] });
    },
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => msgTemplatesApi.remove("whatsapp", id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "whatsapp"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const rows = (list.data ?? []) as WhatsAppTemplate[];

  const columns: ColumnsType<WhatsAppTemplate> = [
    {
      title: t("tpl.col.name"),
      dataIndex: "name",
      render: (v: string, r) => (
        <a className="sc-mono" onClick={() => { setEditing(r); setBuilderOpen(true); }}>{v}</a>
      ),
    },
    { title: t("tpl.col.label"), dataIndex: "label", width: 120, render: (v) => v || "—" },
    { title: t("tpl.col.waba"), dataIndex: "waba_account_id", width: 130, render: (v: string) => v?.slice(0, 8) || "—" },
    {
      title: t("tpl.col.contentType"),
      dataIndex: "category",
      width: 100,
      render: (v: WaTemplateCategory) => t(`tpl.wa.cat.${v}` as Parameters<typeof t>[0]),
    },
    {
      title: t("tpl.col.content"),
      dataIndex: "body",
      render: (_, r) => (
        <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)", display: "block", maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {r.body?.text}
        </span>
      ),
    },
    { title: t("tpl.col.language"), dataIndex: "language", width: 90 },
    {
      title: t("tpl.col.status"),
      dataIndex: "approval_status",
      width: 90,
      render: (s: WaApprovalStatus, r) =>
        s === "rejected" && r.rejected_reason ? (
          <Tooltip title={r.rejected_reason}>{approvalTag(s)}</Tooltip>
        ) : (
          approvalTag(s)
        ),
    },
    {
      title: t("common.actions"),
      width: 140,
      render: (_, r) => {
        // submit-for-review only works on BSP (YCloud) accounts — Cloud API
        // templates are created in Meta Business Manager (backend 422s them)
        const acct = (accounts.data ?? []).find((a) => a.id === r.waba_account_id);
        const canSubmit =
          acct?.channel_type === "whatsapp_bsp" &&
          (r.approval_status === "draft" || r.approval_status === "rejected");
        return (
          <>
            {canSubmit && (
              <Button
                type="link"
                size="small"
                loading={submit.isPending && submit.variables === r.id}
                onClick={() => submit.mutate(r.id)}
              >
                {t("tpl.submit")}
              </Button>
            )}
            <Button type="text" size="small" danger icon={<DeleteOutlined />} onClick={() => remove.mutate(r.id)} />
          </>
        );
      },
    },
  ];

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 12 }}>
        <a href="https://developers.facebook.com/docs/whatsapp/message-templates" target="_blank" rel="noreferrer">
          <Button icon={<LinkOutlined />}>{t("tpl.officialExample")}</Button>
        </a>
        <Button icon={<SyncOutlined />} loading={sync.isPending} disabled={wabaAccounts.length === 0} onClick={() => sync.mutate()}>
          {t("tpl.sync")}
        </Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => { setEditing(null); setBuilderOpen(true); }}>
          {t("tpl.create")}
        </Button>
      </div>
      {list.isLoading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : rows.length === 0 ? (
        <EmptyState icon={<PlusOutlined />} title={t("tpl.empty")} hint={t("tpl.emptyHint")} />
      ) : (
        <Table<WhatsAppTemplate> rowKey="id" size="small" columns={columns} dataSource={rows} pagination={false} scroll={{ x: 900 }} />
      )}
      <WhatsAppBuilder
        open={builderOpen}
        editing={editing}
        wabaOptions={wabaAccounts.map((a) => ({ value: a.id, label: a.display_name }))}
        onClose={() => setBuilderOpen(false)}
      />
    </div>
  );
}

function WhatsAppBuilder({
  open,
  editing,
  wabaOptions,
  onClose,
}: {
  open: boolean;
  editing: WhatsAppTemplate | null;
  wabaOptions: { value: string; label: string }[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { message } = App.useApp();

  const [name, setName] = useState("");
  const [label, setLabel] = useState("");
  const [waba, setWaba] = useState<string | undefined>(undefined);
  const [category, setCategory] = useState<WaTemplateCategory>("marketing");
  const [language, setLanguage] = useState("en");
  const [header, setHeader] = useState("");
  const [body, setBody] = useState("");
  const [footer, setFooter] = useState("");
  const [btnKind, setBtnKind] = useState<WaButtonKind>("none");
  const [btnItems, setBtnItems] = useState<WaButtonItem[]>([]);

  // hydrate on open / editing change
  useMemo(() => {
    if (open) {
      setName(editing?.name ?? "");
      setLabel(editing?.label ?? "");
      setWaba(editing?.waba_account_id ?? wabaOptions[0]?.value);
      setCategory(editing?.category ?? "marketing");
      setLanguage(editing?.language ?? "en");
      setHeader(editing?.header?.text ?? "");
      setBody(editing?.body?.text ?? "");
      setFooter(editing?.footer?.text ?? "");
      setBtnKind(editing?.buttons?.type ?? "none");
      setBtnItems(editing?.buttons?.items ?? []);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, editing]);

  const save = useMutation({
    mutationFn: () => {
      const payload = {
        name: name.trim(),
        label: label.trim() || null,
        waba_account_id: waba,
        category,
        language,
        header: header.trim() ? { type: "text", text: header.trim() } : null,
        body: { text: body },
        footer: footer.trim() ? { text: footer.trim() } : null,
        buttons: btnKind === "none" ? { type: "none", items: [] } : { type: btnKind, items: btnItems },
      };
      return editing
        ? msgTemplatesApi.update("whatsapp", editing.id, payload)
        : msgTemplatesApi.create("whatsapp", payload);
    },
    onSuccess: () => {
      message.success(t("tpl.saved"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "whatsapp"] });
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const insertVar = () => {
    const nextIdx = (body.match(/\{\{\d+\}\}/g)?.length ?? 0) + 1;
    setBody((b) => `${b}{{${nextIdx}}}`);
  };

  const previewText = (s: string) => s.replace(/\{\{(\w+)\}\}/g, "[$1]");

  return (
    <Drawer
      title={editing ? editing.name : t("tpl.create")}
      open={open}
      onClose={onClose}
      width={880}
      destroyOnHidden
      extra={
        <Button type="primary" loading={save.isPending} disabled={!name.trim() || !body.trim() || !waba} onClick={() => save.mutate()}>
          {t("common.save")}
        </Button>
      }
    >
      <div style={{ display: "flex", gap: 24 }}>
        {/* editor */}
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 14 }}>
          {editing && <div>{approvalTag(editing.approval_status)}</div>}
          <Field label={t("tpl.wa.name")} hint={t("tpl.wa.nameHint")}>
            <Input
              value={name}
              maxLength={50}
              onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
              placeholder="order_update"
              disabled={!!editing}
            />
          </Field>
          <Field label={t("tpl.wa.label")} hint={t("tpl.wa.labelHint")}>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} />
          </Field>
          <Field label={t("tpl.wa.waba")} hint={t("tpl.wa.wabaHint")}>
            <Select style={{ width: "100%" }} value={waba} onChange={setWaba} options={wabaOptions} placeholder={t("common.select")} />
          </Field>
          <Field label={t("tpl.wa.category")}>
            <Radio.Group
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              options={[
                { value: "marketing", label: t("tpl.wa.cat.marketing") },
                { value: "utility", label: t("tpl.wa.cat.utility") },
                { value: "authentication", label: t("tpl.wa.cat.authentication") },
              ]}
              optionType="button"
            />
          </Field>
          <Field label={t("tpl.wa.language")}>
            <Select style={{ width: "100%" }} value={language} onChange={setLanguage} options={WA_LANGS} showSearch />
          </Field>
          <Field label={t("tpl.wa.header")}>
            <Input value={header} maxLength={60} onChange={(e) => setHeader(e.target.value)} placeholder={t("tpl.wa.headerPlaceholder")} showCount />
          </Field>
          <Field label={t("tpl.wa.body")} hint={t("tpl.wa.bodyHint")}>
            <Input.TextArea
              value={body}
              maxLength={1024}
              rows={4}
              onChange={(e) => setBody(e.target.value)}
              placeholder={t("tpl.wa.bodyPlaceholder")}
              showCount
            />
            <div style={{ marginTop: 6, display: "flex", gap: 6 }}>
              <Button size="small" onClick={insertVar}>{t("tpl.wa.insertVar")}</Button>
              {["😀", "🎉", "👍", "❤️"].map((e) => (
                <Button size="small" key={e} onClick={() => setBody((b) => b + e)}>{e}</Button>
              ))}
            </div>
          </Field>
          <Field label={t("tpl.wa.footer")}>
            <Input value={footer} maxLength={60} onChange={(e) => setFooter(e.target.value)} placeholder={t("tpl.wa.footerPlaceholder")} showCount />
          </Field>
          <Field label={t("tpl.wa.buttons")}>
            <Radio.Group
              value={btnKind}
              onChange={(e) => {
                setBtnKind(e.target.value);
                setBtnItems([]);
              }}
              options={[
                { value: "none", label: t("tpl.wa.btn.none") },
                { value: "call_to_action", label: t("tpl.wa.btn.call_to_action") },
                { value: "quick_reply", label: t("tpl.wa.btn.quick_reply") },
              ]}
              optionType="button"
            />
            {btnKind !== "none" && (
              <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
                {btnItems.map((it, i) => (
                  <div key={i} style={{ display: "flex", gap: 8 }}>
                    {btnKind === "call_to_action" && (
                      <Select
                        style={{ width: 120 }}
                        value={it.type ?? "url"}
                        onChange={(v) => setBtnItems((arr) => arr.map((x, idx) => (idx === i ? { ...x, type: v } : x)))}
                        options={[
                          { value: "url", label: "URL" },
                          { value: "phone_number", label: t("tpl.wa.btnValue") },
                        ]}
                      />
                    )}
                    <Input
                      style={{ flex: 1 }}
                      placeholder={t("tpl.wa.btnText")}
                      value={it.text}
                      onChange={(e) => setBtnItems((arr) => arr.map((x, idx) => (idx === i ? { ...x, text: e.target.value } : x)))}
                    />
                    {btnKind === "call_to_action" && (
                      <Input
                        style={{ flex: 1 }}
                        placeholder={t("tpl.wa.btnValue")}
                        value={it.value ?? ""}
                        onChange={(e) => setBtnItems((arr) => arr.map((x, idx) => (idx === i ? { ...x, value: e.target.value } : x)))}
                      />
                    )}
                    <Button type="text" danger icon={<DeleteOutlined />} onClick={() => setBtnItems((arr) => arr.filter((_, idx) => idx !== i))} />
                  </div>
                ))}
                <Button
                  size="small"
                  type="dashed"
                  icon={<PlusOutlined />}
                  disabled={btnItems.length >= 3}
                  onClick={() =>
                    setBtnItems((arr) => [...arr, btnKind === "call_to_action" ? { type: "url", text: "", value: "" } : { type: "quick_reply", text: "" }])
                  }
                >
                  {t("tpl.wa.addButton")}
                </Button>
              </div>
            )}
          </Field>
        </div>

        {/* live phone preview */}
        <div style={{ flex: "none" }}>
          <div className="sc-mkt-hint" style={{ marginBottom: 8 }}>{t("tpl.wa.preview")}</div>
          <div className="sc-wa-preview">
            <div className="sc-wa-bubble">
              {header.trim() && <div className="sc-wa-header">{previewText(header)}</div>}
              <div>{previewText(body) || t("tpl.wa.bodyPlaceholder")}</div>
              {footer.trim() && <div className="sc-wa-footer">{previewText(footer)}</div>}
            </div>
            {btnKind !== "none" && btnItems.length > 0 && (
              <div className="sc-wa-btns">
                {btnItems.map((it, i) => (
                  <div className="sc-wa-btn" key={i}>{it.text || t("tpl.wa.btnText")}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </Drawer>
  );
}

/* ------------------------------------------------------------------- Email */
function EmailTab() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [mjml, setMjml] = useState("");
  const [variables, setVariables] = useState<string[]>([]);

  const list = useQuery({ queryKey: ["msg-templates", "email"], queryFn: () => msgTemplatesApi.list("email"), retry: 1 });
  const save = useMutation({
    mutationFn: () => msgTemplatesApi.create("email", { name: name.trim(), subject, mjml_source: mjml, variables }),
    onSuccess: () => {
      message.success(t("tpl.saved"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "email"] });
      setOpen(false);
      setName(""); setSubject(""); setMjml(""); setVariables([]);
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const rows = (list.data ?? []) as EmailTemplate[];

  return (
    <ChannelTemplateList
      loading={list.isLoading}
      empty={rows.length === 0}
      onCreate={() => setOpen(true)}
      columns={[
        { title: t("tpl.col.name"), dataIndex: "name" },
        { title: t("tpl.col.subject"), dataIndex: "subject" },
      ]}
      rows={rows}
    >
      <Modal title={t("tpl.create")} open={open} onCancel={() => setOpen(false)} onOk={() => save.mutate()} confirmLoading={save.isPending} okText={t("common.save")} cancelText={t("common.cancel")} width={640}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 6 }}>
          <Field label={t("tpl.email.name")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label={t("tpl.email.subject")}><Input value={subject} onChange={(e) => setSubject(e.target.value)} /></Field>
          <Field label={t("tpl.email.mjml")} hint={t("tpl.email.mjmlHint")}>
            <Input.TextArea value={mjml} rows={7} onChange={(e) => setMjml(e.target.value)} style={{ fontFamily: "var(--sc-font-mono)", fontSize: 12.5 }} />
          </Field>
          <Field label={t("tpl.email.variables")}>
            <Select mode="tags" style={{ width: "100%" }} value={variables} onChange={setVariables} tokenSeparators={[","]} />
          </Field>
        </div>
      </Modal>
    </ChannelTemplateList>
  );
}

/* --------------------------------------------------------------- Messenger */
function MessengerTab() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [payload, setPayload] = useState("");
  const [tag, setTag] = useState("");

  const list = useQuery({ queryKey: ["msg-templates", "messenger"], queryFn: () => msgTemplatesApi.list("messenger"), retry: 1 });
  const save = useMutation({
    mutationFn: () => msgTemplatesApi.create("messenger", { name: name.trim(), payload, message_tag: tag || null }),
    onSuccess: () => {
      message.success(t("tpl.saved"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "messenger"] });
      setOpen(false); setName(""); setPayload(""); setTag("");
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const rows = (list.data ?? []) as MessengerTemplate[];

  return (
    <ChannelTemplateList
      loading={list.isLoading}
      empty={rows.length === 0}
      onCreate={() => setOpen(true)}
      columns={[
        { title: t("tpl.col.name"), dataIndex: "name" },
        { title: t("tpl.msgr.messageTag"), dataIndex: "message_tag", render: (v) => v || "—" },
      ]}
      rows={rows}
    >
      <Modal title={t("tpl.create")} open={open} onCancel={() => setOpen(false)} onOk={() => save.mutate()} confirmLoading={save.isPending} okText={t("common.save")} cancelText={t("common.cancel")}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 6 }}>
          <Field label={t("tpl.msgr.name")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label={t("tpl.msgr.payload")}><Input.TextArea value={payload} rows={4} onChange={(e) => setPayload(e.target.value)} /></Field>
          <Field label={t("tpl.msgr.messageTag")} hint={t("tpl.msgr.tagHint")}>
            <Select
              allowClear
              style={{ width: "100%" }}
              value={tag || undefined}
              onChange={(v) => setTag(v ?? "")}
              options={[
                { value: "CONFIRMED_EVENT_UPDATE", label: "CONFIRMED_EVENT_UPDATE" },
                { value: "POST_PURCHASE_UPDATE", label: "POST_PURCHASE_UPDATE" },
                { value: "ACCOUNT_UPDATE", label: "ACCOUNT_UPDATE" },
              ]}
            />
          </Field>
        </div>
      </Modal>
    </ChannelTemplateList>
  );
}

/* -------------------------------------------------------------------- SMS */
function SmsTab() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [open, setOpen] = useState(false);
  const [sigOpen, setSigOpen] = useState(false);
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [signatureId, setSignatureId] = useState<string | undefined>(undefined);

  const list = useQuery({ queryKey: ["msg-templates", "sms"], queryFn: () => msgTemplatesApi.list("sms"), retry: 1 });
  const signatures = useQuery({ queryKey: ["sms-signatures"], queryFn: () => msgTemplatesApi.signatures(), retry: 1 });
  const save = useMutation({
    mutationFn: () => msgTemplatesApi.create("sms", { name: name.trim(), text, signature_id: signatureId ?? null }),
    onSuccess: () => {
      message.success(t("tpl.saved"));
      void qc.invalidateQueries({ queryKey: ["msg-templates", "sms"] });
      setOpen(false); setName(""); setText(""); setSignatureId(undefined);
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const rows = (list.data ?? []) as SmsTemplate[];
  const seg = smsSegments(text);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 12 }}>
        <Button onClick={() => setSigOpen(true)}>{t("tpl.sms.addSignature")}</Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>{t("tpl.create")}</Button>
      </div>
      {list.isLoading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : rows.length === 0 ? (
        <EmptyState icon={<PlusOutlined />} title={t("tpl.empty")} hint={t("tpl.emptyHint")} />
      ) : (
        <Table<SmsTemplate>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={rows}
          columns={[
            { title: t("tpl.col.name"), dataIndex: "name" },
            { title: t("tpl.sms.text"), dataIndex: "text", render: (v: string) => <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>{v}</span> },
          ]}
        />
      )}
      <Modal title={t("tpl.create")} open={open} onCancel={() => setOpen(false)} onOk={() => save.mutate()} confirmLoading={save.isPending} okText={t("common.save")} cancelText={t("common.cancel")}>
        <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 6 }}>
          <Field label={t("tpl.sms.name")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label={t("tpl.sms.text")} hint={t("tpl.sms.segHint", { enc: seg.encoding, chars: seg.chars, segments: seg.segments })}>
            <Input.TextArea value={text} rows={4} onChange={(e) => setText(e.target.value)} />
          </Field>
          <Field label={t("tpl.sms.signature")}>
            <Select
              allowClear
              style={{ width: "100%" }}
              value={signatureId}
              onChange={(v) => setSignatureId(v)}
              placeholder={t("tpl.sms.noSignature")}
              options={(signatures.data ?? []).map((s) => ({ value: s.id, label: s.name }))}
            />
          </Field>
        </div>
      </Modal>
      <SmsSignaturesModal open={sigOpen} onClose={() => setSigOpen(false)} />
    </div>
  );
}

/* ------------------------------------------------------------------ shared */
function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12.5, fontWeight: 500, color: "var(--sc-text-secondary)", marginBottom: 6 }}>{label}</div>
      {children}
      {hint && <div className="sc-mkt-hint">{hint}</div>}
    </div>
  );
}

function ChannelTemplateList<T extends { id: string }>({
  loading,
  empty,
  onCreate,
  columns,
  rows,
  children,
}: {
  loading: boolean;
  empty: boolean;
  onCreate: () => void;
  columns: ColumnsType<T>;
  rows: T[];
  children: React.ReactNode;
}) {
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <Button type="primary" icon={<PlusOutlined />} onClick={onCreate}>{t("tpl.create")}</Button>
      </div>
      {loading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : empty ? (
        <EmptyState icon={<PlusOutlined />} title={t("tpl.empty")} hint={t("tpl.emptyHint")} />
      ) : (
        <Table<T> rowKey="id" size="small" pagination={false} dataSource={rows} columns={columns} />
      )}
      {children}
    </div>
  );
}
