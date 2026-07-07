/** Widget config editor — appearance / pre-chat / routing / domains tabs
 *  with a live preview iframe (srcDoc mock of the visitor widget). */
import { ArrowLeftOutlined, CodeOutlined, SaveOutlined } from "@ant-design/icons";
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
  Tooltip,
} from "antd";
import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { groupsApi, membersApi, widgetsApi } from "@/api/endpoints";
import type { WidgetConfig } from "@/api/types";
import { t } from "@/i18n";
import { InstallModal } from "./WidgetsPage";

interface FormShape {
  color: string;
  position: "right" | "left";
  greeting: string;
  brand_name: string;
  remove_branding: boolean;
  prechat_enabled: boolean;
  prechat_fields: string[];
  prechat_message: string;
  group_id?: string;
  member_id?: string;
  offline_lead: boolean;
  domains_text: string;
}

function toForm(w: WidgetConfig): FormShape {
  return {
    color: w.appearance.color,
    position: w.appearance.position,
    greeting: w.appearance.greeting ?? "",
    brand_name: w.appearance.brand_name ?? "",
    remove_branding: w.appearance.remove_branding,
    prechat_enabled: w.prechat.enabled,
    prechat_fields: [
      ...(w.prechat.require_name ? ["name"] : []),
      ...(w.prechat.require_email ? ["email"] : []),
      ...(w.prechat.require_phone ? ["phone"] : []),
    ],
    prechat_message: w.prechat.message ?? "",
    group_id: w.routing.group_id ?? undefined,
    member_id: w.routing.member_id ?? undefined,
    offline_lead: w.routing.offline_lead,
    domains_text: w.allowed_domains.join("\n"),
  };
}

function previewHtml(v: Partial<FormShape>): string {
  const color = typeof v.color === "string" ? v.color : "#2C5CE6";
  const pos = v.position === "left" ? "left" : "right";
  const greeting = (v.greeting || "您好！有什麼可以幫到您？").replace(/</g, "&lt;");
  const brand = (v.brand_name || "SmartChat").replace(/</g, "&lt;");
  const prechat = v.prechat_enabled
    ? `<div class="pre"><div class="pre-t">${(v.prechat_message || "請留下您的聯絡方式").replace(/</g, "&lt;")}</div>${(v.prechat_fields ?? [])
        .map((f) => `<input placeholder="${f === "name" ? "姓名" : f === "email" ? "郵箱" : "電話"}" />`)
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
  .in{border:none;border-top:1px solid #e5e9f0;padding:10px 12px;font-size:12px;color:#9aa4b5;background:#fff}
  .fab{position:absolute;bottom:20px;${pos}:16px;width:52px;height:52px;border-radius:50%;background:${color};box-shadow:0 8px 20px rgba(15,23,42,.25);display:flex;align-items:center;justify-content:center}
  .fab svg{width:26px;height:26px;fill:#fff}
  .brand{font-size:10px;color:#b3bcc9;text-align:center;padding:4px}
  .pre{padding:10px;display:flex;flex-direction:column;gap:6px}
  .pre-t{font-size:12px;color:#5a6472}
  .pre input{border:1px solid #dfe4ec;border-radius:8px;padding:7px 9px;font-size:12px}
  .pre button{background:${color};color:#fff;border:none;border-radius:8px;padding:8px;font-size:12.5px;margin-top:2px}
  </style></head><body>
  <div class="page">www.example.com</div>
  <div class="panel">
    <div class="hd">${brand}<small>線上客服</small></div>
    <div class="body"><div class="msg">${greeting}</div>${prechat}</div>
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
  const groups = useQuery({ queryKey: ["member-groups"], queryFn: () => groupsApi.list(), retry: 0 });

  const save = useMutation({
    mutationFn: (v: FormShape) =>
      widgetsApi.update(id!, {
        appearance: {
          color: typeof v.color === "string" ? v.color : "#2C5CE6",
          position: v.position,
          greeting: v.greeting || null,
          brand_name: v.brand_name || null,
          remove_branding: v.remove_branding,
        },
        prechat: {
          enabled: v.prechat_enabled,
          require_name: v.prechat_fields.includes("name"),
          require_email: v.prechat_fields.includes("email"),
          require_phone: v.prechat_fields.includes("phone"),
          message: v.prechat_message || null,
        },
        routing: {
          group_id: v.group_id ?? null,
          member_id: v.member_id ?? null,
          offline_lead: v.offline_lead,
          flow_id: null,
        },
        allowed_domains: v.domains_text
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["widgets"] });
      void qc.invalidateQueries({ queryKey: ["widget", id] });
    },
    onError: () => message.error(t("common.operationFailed")),
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
          <Form form={form} layout="vertical" initialValues={toForm(w)}>
            <Tabs
              items={[
                {
                  key: "appearance",
                  label: t("widget.config.appearance"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item
                        name="color"
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
                      <Form.Item name="brand_name" label={t("widget.config.brandName")}>
                        <Input maxLength={30} />
                      </Form.Item>
                      <Form.Item name="greeting" label={t("widget.config.greeting")}>
                        <Input.TextArea rows={2} maxLength={200} showCount />
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
                  key: "prechat",
                  label: t("widget.config.prechat"),
                  forceRender: true,
                  children: (
                    <>
                      <Form.Item
                        name="prechat_enabled"
                        label={t("widget.config.prechatEnabled")}
                        valuePropName="checked"
                      >
                        <Switch />
                      </Form.Item>
                      <Form.Item name="prechat_fields" label={t("cust.filter.field")}>
                        <Checkbox.Group
                          options={[
                            { label: t("widget.config.prechatField.name"), value: "name" },
                            { label: t("widget.config.prechatField.email"), value: "email" },
                            { label: t("widget.config.prechatField.phone"), value: "phone" },
                          ]}
                        />
                      </Form.Item>
                      <Form.Item name="prechat_message" label={t("widget.config.prechatMessage")}>
                        <Input.TextArea rows={2} maxLength={200} showCount />
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
                      <Form.Item label={t("widget.config.bindFlow")}>
                        <Tooltip title={t("widget.config.bindFlowHint")}>
                          <Select disabled placeholder={t("common.comingSoon")} />
                        </Tooltip>
                      </Form.Item>
                      <Form.Item name="group_id" label={t("widget.config.assignGroup")}>
                        <Select
                          allowClear
                          options={(groups.data ?? []).map((g) => ({ value: g.id, label: g.name }))}
                        />
                      </Form.Item>
                      <Form.Item name="member_id" label={t("widget.config.assignMember")}>
                        <Select
                          allowClear
                          options={(members.data ?? []).map((m) => ({
                            value: m.id,
                            label: m.display_name,
                          }))}
                        />
                      </Form.Item>
                      <Form.Item
                        name="offline_lead"
                        label={t("widget.config.offlineLead")}
                        valuePropName="checked"
                      >
                        <Switch />
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
