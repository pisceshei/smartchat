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
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { membersApi, widgetsApi } from "@/api/endpoints";
import { t } from "@/i18n";
import {
  describeValidationError,
  type ErrorFieldInfo,
  type FormShape,
  previewHtml,
  pruneEmptyRows,
  toConfig,
  toForm,
} from "./widgetConfigForm";
import { InstallModal } from "./WidgetsPage";

const FIELD_TYPE_OPTIONS = [
  { value: "text", label: t("widget.config.fieldType.text") },
  { value: "email", label: t("widget.config.fieldType.email") },
  { value: "phone", label: t("widget.config.fieldType.phone") },
  { value: "textarea", label: t("widget.config.fieldType.textarea") },
];

export function WidgetConfigPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<FormShape>();
  const values = Form.useWatch([], form);
  const [installOpen, setInstallOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("brand");

  const widget = useQuery({
    queryKey: ["widget", id],
    queryFn: () => widgetsApi.get(id!),
    enabled: !!id,
    retry: 1,
  });

  const members = useQuery({ queryKey: ["members"], queryFn: () => membersApi.list(), retry: 0 });
  const aiMembers = (members.data ?? []).filter((m) => m.member_type === "ai_agent");
  const humanMembers = (members.data ?? []).filter((m) => m.member_type === "human");

  const savedSnapshot = useRef<FormShape | null>(null);
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
    onSuccess: (_res, submitted) => {
      // remember exactly what we sent — the post-save refetch may re-hydrate
      // the form, but ONLY if the user hasn't typed something new since
      savedSnapshot.current = submitted;
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["widgets"] });
      void qc.invalidateQueries({ queryKey: ["widget", id] });
    },
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
  });

  // Preview seed: Form.useWatch([]) is undefined until the FIRST user edit, so
  // without a seed the preview rendered "SmartChat" defaults over a fully
  // saved config — which read as "儲存沒生效" in production.
  const seed = useMemo<Partial<FormShape>>(
    () =>
      widget.data
        ? toForm(
            widget.data.name,
            widget.data.config ?? {},
            widget.data.brand_removed,
            widget.data.allowed_domains ?? [],
          )
        : {},
    [widget.data],
  );
  const srcDoc = useMemo(() => previewHtml(values ?? seed), [values, seed]);

  // Re-hydrate the form when fresh data lands (antd initialValues only apply
  // on first mount). Skip while the user has unsaved edits — EXCEPT right
  // after their own save, and even then only when the live form still equals
  // what was submitted (edits made during the refetch window are preserved).
  useEffect(() => {
    const d = widget.data;
    if (!d) return;
    const snap = savedSnapshot.current;
    if (form.isFieldsTouched()) {
      const live = form.getFieldsValue(true) as FormShape;
      if (!(snap && JSON.stringify(live) === JSON.stringify(snap))) return;
    }
    savedSnapshot.current = null;
    form.setFieldsValue(toForm(d.name, d.config ?? {}, d.brand_removed, d.allowed_domains ?? []));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [widget.dataUpdatedAt]);

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
            onClick={() => {
              // fully-empty banner/prechat rows would fail required-field
              // validation invisibly — drop them first (toConfig discards
              // them at save time anyway)
              const pruned = pruneEmptyRows(form.getFieldsValue(true) as FormShape);
              form.setFieldsValue({
                banners: pruned.banners,
                prechat_fields: pruned.prechat_fields,
              });
              form
                .validateFields()
                .then((v) => save.mutate(v))
                .catch((info: { errorFields?: ErrorFieldInfo[] }) => {
                  // a rejected validateFields used to be swallowed — the save
                  // silently did nothing with the failed field hidden on
                  // another tab. Name the tab/row/field and jump there.
                  const d = describeValidationError(info?.errorFields);
                  if (d.tab) setActiveTab(d.tab);
                  if (d.name) form.scrollToField(d.name);
                  message.error(d.text);
                });
            }}
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
              activeKey={activeTab}
              onChange={setActiveTab}
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
