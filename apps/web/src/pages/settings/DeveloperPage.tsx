/** 開發者 — OpenAPI token (reveal-once) + webhook config (9-channel message
 *  events, customer-only switch, contact & channel-status events). */
import { ApiOutlined, CopyOutlined, KeyOutlined, SendOutlined } from "@ant-design/icons";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Popconfirm,
  Skeleton,
  Switch,
  Tag,
} from "antd";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsApi } from "@/api/endpoints";
import type { ChannelType, WebhookConfig } from "@/api/types";
import { CHANNEL_CATALOG } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";

const WEBHOOK_CHANNELS: ChannelType[] = [
  "widget",
  "whatsapp_app",
  "whatsapp_api",
  "messenger",
  "instagram",
  "telegram_bot",
  "email",
  "line_oa",
  "line_app",
];

const DEFAULT_WEBHOOK: WebhookConfig = {
  url: "",
  token: null,
  channel_message_events: [],
  customer_message_only: false,
  contact_created: false,
  contact_updated: false,
  channel_status: false,
  enabled: false,
};

function ApiTokenCard() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [revealed, setRevealed] = useState<string | null>(null);

  const info = useQuery({
    queryKey: ["api-token"],
    queryFn: () => settingsApi.getApiToken(),
    retry: 1,
  });

  const create = useMutation({
    mutationFn: () => settingsApi.createApiToken(),
    onSuccess: (res) => {
      setRevealed(res.token);
      void qc.invalidateQueries({ queryKey: ["api-token"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Card
      size="small"
      title={
        <span>
          <KeyOutlined style={{ marginRight: 8 }} />
          {t("dev.apiToken")}
        </span>
      }
      style={{ marginBottom: 16 }}
    >
      <p style={{ color: "var(--sc-text-secondary)", fontSize: 13, marginTop: 0 }}>
        {t("dev.apiTokenHint")}
      </p>
      {info.isLoading ? (
        <Skeleton active paragraph={{ rows: 1 }} title={false} />
      ) : info.data?.has_token ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <Tag className="sc-mono" style={{ fontSize: 13, padding: "3px 10px" }}>
            {info.data.token_prefix}••••••••••••••••
          </Tag>
          <span style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>
            {t("dev.lastRotated")}: {fullTime(info.data.created_at) || "-"}
          </span>
          <Popconfirm
            title={t("dev.regenerateConfirm")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => create.mutate()}
          >
            <Button loading={create.isPending}>{t("dev.regenerate")}</Button>
          </Popconfirm>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ color: "var(--sc-text-tertiary)" }}>{t("dev.noToken")}</span>
          <Button type="primary" onClick={() => create.mutate()} loading={create.isPending}>
            {t("dev.generate")}
          </Button>
        </div>
      )}

      <Modal
        title={t("dev.tokenCreatedTitle")}
        open={!!revealed}
        onCancel={() => setRevealed(null)}
        footer={[
          <Button
            key="copy"
            type="primary"
            icon={<CopyOutlined />}
            onClick={() => {
              if (revealed) {
                void navigator.clipboard.writeText(revealed).then(() => message.success(t("common.copied")));
              }
            }}
          >
            {t("common.copy")}
          </Button>,
        ]}
      >
        <Alert type="warning" showIcon message={t("dev.tokenCreatedHint")} style={{ marginBottom: 14 }} />
        <pre className="sc-code-block" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
          {revealed}
        </pre>
      </Modal>
    </Card>
  );
}

function WebhookCard() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<WebhookConfig>();

  const query = useQuery({
    queryKey: ["webhook-config"],
    queryFn: () => settingsApi.getWebhook(),
    retry: 1,
  });

  useEffect(() => {
    if (query.data) form.setFieldsValue(query.data);
  }, [query.data, form]);

  const save = useMutation({
    mutationFn: (values: WebhookConfig) => settingsApi.saveWebhook(values),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["webhook-config"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const test = useMutation({
    mutationFn: () => settingsApi.testWebhook(),
    onSuccess: () => message.success(t("dev.webhookTestSent")),
    onError: () => message.error(t("common.operationFailed")),
  });

  const channelName = (ct: ChannelType) => CHANNEL_CATALOG.find((c) => c.type === ct)?.name ?? ct;

  return (
    <Card
      size="small"
      title={
        <span>
          <ApiOutlined style={{ marginRight: 8 }} />
          {t("dev.webhook")}
        </span>
      }
      extra={
        <span style={{ display: "inline-flex", gap: 8 }}>
          <Button size="small" icon={<SendOutlined />} onClick={() => test.mutate()} loading={test.isPending}>
            {t("dev.webhookTest")}
          </Button>
          <Button
            size="small"
            type="primary"
            onClick={() => form.validateFields().then((v) => save.mutate(v))}
            loading={save.isPending}
          >
            {t("common.save")}
          </Button>
        </span>
      }
    >
      {query.isLoading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : (
        <Form form={form} layout="vertical" initialValues={query.data ?? DEFAULT_WEBHOOK}>
          <Form.Item name="enabled" label={t("common.enabled")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            name="url"
            label={t("dev.webhookUrl")}
            rules={[{ type: "url", message: t("common.required") }]}
          >
            <Input placeholder="https://example.com/webhooks/smartchat" />
          </Form.Item>
          <Form.Item name="token" label={t("dev.webhookToken")}>
            <Input.Password placeholder="whsec_..." />
          </Form.Item>

          <Form.Item name="channel_message_events" label={t("dev.webhookChannelMsg")}>
            <Checkbox.Group
              options={WEBHOOK_CHANNELS.map((ct) => ({ value: ct, label: channelName(ct) }))}
              style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 4 }}
            />
          </Form.Item>
          <Form.Item name="customer_message_only" label={t("dev.webhookCustomerOnly")} valuePropName="checked">
            <Switch size="small" />
          </Form.Item>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, auto)", gap: 8, justifyContent: "start" }}>
            <Form.Item name="contact_created" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Checkbox>{t("dev.webhookContactCreated")}</Checkbox>
            </Form.Item>
            <Form.Item name="contact_updated" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Checkbox>{t("dev.webhookContactUpdated")}</Checkbox>
            </Form.Item>
            <Form.Item name="channel_status" valuePropName="checked" style={{ marginBottom: 0 }}>
              <Checkbox>{t("dev.webhookChannelStatus")}</Checkbox>
            </Form.Item>
          </div>
        </Form>
      )}
    </Card>
  );
}

export function DeveloperPage() {
  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("dev.title")}</h1>
      </div>
      <div className="sc-page-body" style={{ maxWidth: 760 }}>
        <ApiTokenCard />
        <WebhookCard />
      </div>
    </div>
  );
}
