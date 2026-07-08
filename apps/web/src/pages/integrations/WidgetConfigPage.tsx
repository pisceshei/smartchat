/** Widget config editor — 品牌 / 外觀 / 首頁模式 / 留資表單 / 接待 / 網域 tabs
 *  with a live preview iframe (srcDoc mock of the visitor widget).
 *  Settings round-trip through the widget's nested `config` JSONB (contract
 *  schema: brand/appearance/home/pre_chat/offline/routing/features); the REST
 *  shape stays flat {name, config, allowed_domains, ...}. */
import {
  ArrowLeftOutlined,
  CodeOutlined,
  DeleteOutlined,
  PlusOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Checkbox,
  ColorPicker,
  Form,
  Input,
  Radio,
  Select,
  Skeleton,
  Switch,
  Tabs,
} from "antd";
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { membersApi, widgetsApi } from "@/api/endpoints";
import type { WidgetBannerItem, WidgetConfigJson, WidgetPrechatField } from "@/api/types";
import { t } from "@/i18n";
import { InstallModal } from "./WidgetsPage";

interface FormShape {
  name: string;
  brand_name: string;
  welcome_text: string;
  avatar_url: string;
  position: "right" | "left";
  primary_color: string;
  launcher_text: string;
  remove_branding: boolean;
  home_enabled: boolean;
  banners: WidgetBannerItem[];
  reply_hint: string;
  prechat_enabled: boolean;
  prechat_required: boolean;
  prechat_fields: WidgetPrechatField[];
  ai_agent_id: string | null;
  member_ids: string[];
  domains_text: string;
}

const FIELD_TYPE_OPTIONS = [
  { value: "text", label: t("widget.config.fieldType.text") },
  { value: "email", label: t("widget.config.fieldType.email") },
  { value: "phone", label: t("widget.config.fieldType.phone") },
  { value: "textarea", label: t("widget.config.fieldType.textarea") },
];

function toForm(name: string, cfg: WidgetConfigJson, brandRemoved: boolean, domains: string[]): FormShape {
  return {
    name,
    brand_name: cfg.brand?.name ?? "",
    welcome_text: cfg.brand?.welcome_text ?? "",
    avatar_url: cfg.brand?.avatar_url ?? "",
    position: cfg.appearance?.position === "left" ? "left" : "right",
    primary_color: cfg.appearance?.primary_color ?? "#2C5CE6",
    launcher_text: cfg.appearance?.launcher_text ?? "",
    remove_branding: brandRemoved,
    home_enabled: cfg.home?.enabled ?? false,
    banners: cfg.home?.banners ?? [],
    reply_hint: cfg.home?.reply_hint ?? "",
    prechat_enabled: cfg.pre_chat?.enabled ?? false,
    prechat_required: cfg.pre_chat?.required_before_chat ?? false,
    prechat_fields: cfg.pre_chat?.fields ?? [],
    ai_agent_id: cfg.routing?.ai_agent_id ?? null,
    member_ids: cfg.routing?.member_ids ?? [],
    domains_text: domains.join("\n"),
  };
}

/** Full merged nested config for PATCH — preserves keys the editor doesn't
 *  surface (offline/features/strategy). */
function toConfig(cfg: WidgetConfigJson, v: FormShape): WidgetConfigJson {
  return {
    ...cfg,
    brand: {
      ...cfg.brand,
      name: v.brand_name || undefined,
      welcome_text: v.welcome_text || undefined,
      avatar_url: v.avatar_url || undefined,
    },
    appearance: {
      ...cfg.appearance,
      position: v.position,
      primary_color: typeof v.primary_color === "string" ? v.primary_color : "#2C5CE6",
      launcher_text: v.launcher_text || undefined,
    },
    home: {
      ...cfg.home,
      enabled: v.home_enabled,
      banners: (v.banners ?? []).filter((b) => b?.image_url),
      reply_hint: v.reply_hint || undefined,
    },
    pre_chat: {
      ...cfg.pre_chat,
      enabled: v.prechat_enabled,
      required_before_chat: v.prechat_required,
      fields: (v.prechat_fields ?? []).filter((f) => f?.key && f?.label),
    },
    offline: cfg.offline,
    routing: {
      ...cfg.routing,
      ai_agent_id: v.ai_agent_id ?? null,
      member_ids: v.member_ids ?? [],
    },
    features: cfg.features,
  };
}

function previewHtml(v: Partial<FormShape>): string {
  const esc = (s: string) => s.replace(/</g, "&lt;");
  const color = typeof v.primary_color === "string" ? v.primary_color : "#2C5CE6";
  const pos = v.position === "left" ? "left" : "right";
  const greeting = esc(v.welcome_text || "您好！有什麼可以幫到您？");
  const brand = esc(v.brand_name || "SmartChat");
  const banners = v.home_enabled
    ? (v.banners ?? [])
        .filter((b) => b?.image_url)
        .slice(0, 3)
        .map((b) => `<div class="bn"><img src="${esc(b.image_url)}" alt="" /></div>`)
        .join("")
    : "";
  const replyHint = v.home_enabled && v.reply_hint ? `<div class="hint">${esc(v.reply_hint)}</div>` : "";
  const prechat = v.prechat_enabled
    ? `<div class="pre">${(v.prechat_fields ?? [])
        .filter((f) => f?.label)
        .map((f) =>
          f.type === "textarea"
            ? `<textarea placeholder="${esc(f.label)}"></textarea>`
            : `<input placeholder="${esc(f.label)}" />`,
        )
        .join("")}<button>開始對話</button></div>`
    : "";
  return `<!doctype html><html><head><meta charset="utf-8"><style>
  body{margin:0;font-family:-apple-system,'Segoe UI','PingFang TC','Microsoft JhengHei',sans-serif;background:#e9edf3;height:100vh;overflow:hidden}
  .page{padding:14px;color:#9aa4b5;font-size:12px}
  .panel{position:absolute;bottom:84px;${pos}:16px;width:280px;height:340px;background:#fff;border-radius:14px;box-shadow:0 12px 32px rgba(15,23,42,.18);display:flex;flex-direction:column;overflow:hidden}
  .hd{background:${color};color:#fff;padding:12px 14px;font-weight:600;font-size:14px}
  .hd small{display:block;font-weight:400;opacity:.85;font-size:11px}
  .body{flex:1;padding:12px;background:#f6f8fb;overflow:auto}
  .msg{background:#fff;border-radius:10px;border-top-left-radius:3px;padding:8px 10px;font-size:12.5px;max-width:80%;box-shadow:0 1px 2px rgba(15,23,42,.08)}
  .bn{margin-top:8px;border-radius:10px;overflow:hidden;background:#dfe4ec}
  .bn img{display:block;width:100%;height:64px;object-fit:cover}
  .hint{margin-top:8px;font-size:11px;color:#8a94a6;text-align:center}
  .in{border:none;border-top:1px solid #e5e9f0;padding:10px 12px;font-size:12px;color:#9aa4b5;background:#fff}
  .fab{position:absolute;bottom:20px;${pos}:16px;width:52px;height:52px;border-radius:50%;background:${color};box-shadow:0 8px 20px rgba(15,23,42,.25);display:flex;align-items:center;justify-content:center}
  .fab svg{width:26px;height:26px;fill:#fff}
  .brand{font-size:10px;color:#b3bcc9;text-align:center;padding:4px}
  .pre{padding:10px 0 0;display:flex;flex-direction:column;gap:6px}
  .pre input,.pre textarea{border:1px solid #dfe4ec;border-radius:8px;padding:7px 9px;font-size:12px;font-family:inherit;resize:none}
  .pre button{background:${color};color:#fff;border:none;border-radius:8px;padding:8px;font-size:12.5px;margin-top:2px}
  </style></head><body>
  <div class="page">www.example.com</div>
  <div class="panel">
    <div class="hd">${brand}<small>線上客服</small></div>
    <div class="body"><div class="msg">${greeting}</div>${banners}${replyHint}${prechat}</div>
    <div class="in">輸入訊息…</div>
    ${v.remove_branding ? "" : '<div class="brand">Powered by SmartChat</div>'}
  </div>
  <div class="fab"><svg viewBox="0 0 24 24"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.6 3.8c-.5.4-1.4.1-1.4-.6V5.5Z"/></svg></div>
  </body></html>`;
}

export function WidgetConfigPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<FormShape>();
  const values = Form.useWatch([], form);
  const [installOpen, setInstallOpen] = useState(false);

  const widget = useQuery({
    queryKey: ["widget", id],
    queryFn: () => widgetsApi.get(id!),
    enabled: !!id,
    retry: 1,
  });

  const members = useQuery({ queryKey: ["members"], queryFn: () => membersApi.list(), retry: 0 });
  const aiMembers = (members.data ?? []).filter((m) => m.member_type === "ai_agent");
  const humanMembers = (members.data ?? []).filter((m) => m.member_type === "human");

  const save = useMutation({
    mutationFn: (v: FormShape) =>
      widgetsApi.update(id!, {
        name: v.name,
        brand_removed: v.remove_branding,
        allowed_domains: v.domains_text
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        config: toConfig(widget.data?.config ?? {}, v),
      }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["widgets"] });
      void qc.invalidateQueries({ queryKey: ["widget", id] });
    },
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
  });

  const srcDoc = useMemo(() => previewHtml(values ?? {}), [values]);

  if (widget.isLoading || !widget.data) {
    return (
      <div className="sc-page">
        <div className="sc-page-body">
          <Skeleton active paragraph={{ rows: 10 }} />
        </div>
      </div>
    );
  }

  const w = widget.data;

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate("/integrations/widgets")} />
          {w.name}
        </h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<CodeOutlined />} onClick={() => setInstallOpen(true)}>
            {t("widget.install")}
          </Button>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={save.isPending}
            onClick={() => form.validateFields().then((v) => save.mutate(v))}
          >
            {t("common.save")}
          </Button>
        </div>
      </div>

      <div className="sc-page-body" style={{ display: "flex", gap: 20, alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0, background: "var(--sc-bg-container)", borderRadius: 10, padding: "8px 20px 20px", border: "1px solid var(--sc-border)" }}>
          <Form
            form={form}
            layout="vertical"
            initialValues={toForm(w.name, w.config ?? {}, w.brand_removed, w.allowed_domains ?? [])}
          >
            <Tabs
              items={[
                {
                  key: "brand",
                  label: t("widget.config.brand"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item name="name" label={t("widget.name")} rules={[{ required: true, message: t("common.required") }]}>
                        <Input maxLength={128} />
                      </Form.Item>
                      <Form.Item name="brand_name" label={t("widget.config.brandName")}>
                        <Input maxLength={30} />
                      </Form.Item>
                      <Form.Item name="welcome_text" label={t("widget.config.greeting")}>
                        <Input.TextArea rows={2} maxLength={200} showCount />
                      </Form.Item>
                      <Form.Item name="avatar_url" label={t("widget.config.avatarUrl")}>
                        <Input placeholder="https://…" />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "appearance",
                  label: t("widget.config.appearance"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item
                        name="primary_color"
                        label={t("widget.config.color")}
                        getValueFromEvent={(c) => (typeof c === "string" ? c : c.toHexString())}
                      >
                        <ColorPicker showText />
                      </Form.Item>
                      <Form.Item name="position" label={t("widget.config.position")}>
                        <Radio.Group
                          options={[
                            { label: t("widget.config.posRight"), value: "right" },
                            { label: t("widget.config.posLeft"), value: "left" },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item name="launcher_text" label={t("widget.config.launcherText")}>
                        <Input maxLength={30} />
                      </Form.Item>
                      <Form.Item
                        name="remove_branding"
                        label={t("widget.config.removeBranding")}
                        valuePropName="checked"
                        extra={t("widget.config.removeBrandingHint")}
                      >
                        <Switch />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "home",
                  label: t("widget.config.home"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item name="home_enabled" label={t("widget.config.homeEnabled")} valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item label={t("widget.config.banners")} style={{ marginBottom: 0 }}>
                        <Form.List name="banners">
                          {(fields, { add, remove }) => (
                            <>
                              {fields.map(({ key, name }) => (
                                <div key={key} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                                  <Form.Item
                                    name={[name, "image_url"]}
                                    rules={[{ required: true, message: t("common.required") }]}
                                    style={{ flex: 1, marginBottom: 10 }}
                                  >
                                    <Input placeholder={t("widget.config.bannerImage")} />
                                  </Form.Item>
                                  <Form.Item name={[name, "link_url"]} style={{ flex: 1, marginBottom: 10 }}>
                                    <Input placeholder={t("widget.config.bannerLink")} />
                                  </Form.Item>
                                  <Button type="text" danger icon={<DeleteOutlined />} onClick={() => remove(name)} />
                                </div>
                              ))}
                              <Button block type="dashed" icon={<PlusOutlined />} onClick={() => add({ image_url: "" })}>
                                {t("widget.config.addBanner")}
                              </Button>
                            </>
                          )}
                        </Form.List>
                      </Form.Item>
                      <Form.Item name="reply_hint" label={t("widget.config.replyHint")} style={{ marginTop: 20 }}>
                        <Input maxLength={100} />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "prechat",
                  label: t("widget.config.prechat"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item name="prechat_enabled" label={t("widget.config.prechatEnabled")} valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item name="prechat_required" label={t("widget.config.prechatRequired")} valuePropName="checked">
                        <Switch />
                      </Form.Item>
                      <Form.Item label={t("widget.config.prechatFields")} style={{ marginBottom: 0 }}>
                        <Form.List name="prechat_fields">
                          {(fields, { add, remove }) => (
                            <>
                              {fields.map(({ key, name }) => (
                                <div key={key} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                                  <Form.Item
                                    name={[name, "key"]}
                                    rules={[{ required: true, message: t("common.required") }]}
                                    style={{ width: 110, marginBottom: 10 }}
                                  >
                                    <Input placeholder={t("widget.config.fieldKey")} />
                                  </Form.Item>
                                  <Form.Item name={[name, "type"]} style={{ width: 110, marginBottom: 10 }}>
                                    <Select options={FIELD_TYPE_OPTIONS} />
                                  </Form.Item>
                                  <Form.Item
                                    name={[name, "label"]}
                                    rules={[{ required: true, message: t("common.required") }]}
                                    style={{ flex: 1, marginBottom: 10 }}
                                  >
                                    <Input placeholder={t("widget.config.fieldLabel")} />
                                  </Form.Item>
                                  <Form.Item name={[name, "required"]} valuePropName="checked" style={{ marginBottom: 10 }}>
                                    <Checkbox>{t("widget.config.fieldRequired")}</Checkbox>
                                  </Form.Item>
                                  <Button type="text" danger icon={<DeleteOutlined />} onClick={() => remove(name)} />
                                </div>
                              ))}
                              <Button
                                block
                                type="dashed"
                                icon={<PlusOutlined />}
                                onClick={() => add({ key: "", type: "text", label: "" })}
                              >
                                {t("widget.config.addPrechatField")}
                              </Button>
                            </>
                          )}
                        </Form.List>
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "routing",
                  label: t("widget.config.routing"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item name="ai_agent_id" label={t("widget.config.aiAgent")}>
                        <Select
                          allowClear
                          placeholder={t("widget.config.aiAgentAuto")}
                          options={aiMembers.map((m) => ({ value: m.id, label: m.display_name }))}
                        />
                      </Form.Item>
                      <Form.Item name="member_ids" label={t("widget.config.assignMembers")}>
                        <Select
                          mode="multiple"
                          allowClear
                          options={humanMembers.map((m) => ({ value: m.id, label: m.display_name }))}
                        />
                      </Form.Item>
                    </>
                  ),
                },
                {
                  key: "domains",
                  label: t("widget.config.domains"),
                  forceRender: true,
                  children: (
                    <Form.Item
                      name="domains_text"
                      label={t("widget.config.allowedDomains")}
                      extra={t("widget.config.allowedDomainsHint")}
                    >
                      <Input.TextArea rows={5} placeholder={"www.example.com\nshop.example.com"} />
                    </Form.Item>
                  ),
                },
              ]}
            />
          </Form>
        </div>

        <div style={{ width: 360, flex: "none" }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: "var(--sc-text-secondary)" }}>
            {t("widget.config.previewTitle")}
          </div>
          <iframe
            title={t("widget.config.previewTitle")}
            srcDoc={srcDoc}
            style={{
              width: "100%",
              height: 480,
              border: "1px solid var(--sc-border)",
              borderRadius: 12,
              background: "#fff",
            }}
            sandbox=""
          />
        </div>
      </div>

      <InstallModal widget={installOpen ? w : null} onClose={() => setInstallOpen(false)} />
    </div>
  );
}
