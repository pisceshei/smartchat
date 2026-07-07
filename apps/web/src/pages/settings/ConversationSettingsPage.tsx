/** 會話設定 — auto-assign / 託管 / auto-close / offline reply forms. */
import { SaveOutlined } from "@ant-design/icons";
import { App, Button, Card, Form, InputNumber, Radio, Skeleton, Switch } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { settingsApi } from "@/api/endpoints";
import type { ConversationSettings } from "@/api/types";
import { t } from "@/i18n";

const DEFAULTS: ConversationSettings = {
  auto_assign_mode: "round_robin",
  ai_first: false,
  bot_first: true,
  keep_managed: false,
  auto_close_days: 1,
  auto_close_hours: 0,
  auto_close_minutes: 0,
  offline_reply_mode: "email",
};

export function ConversationSettingsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<ConversationSettings>();

  const query = useQuery({
    queryKey: ["conversation-settings"],
    queryFn: () => settingsApi.getConversation(),
    retry: 1,
  });

  const save = useMutation({
    mutationFn: (values: ConversationSettings) => settingsApi.saveConversation(values),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["conversation-settings"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  if (query.isLoading) {
    return (
      <div className="sc-page">
        <div className="sc-page-body">
          <Skeleton active paragraph={{ rows: 8 }} />
        </div>
      </div>
    );
  }

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("set.nav.conversation")}</h1>
        <Button
          type="primary"
          icon={<SaveOutlined />}
          loading={save.isPending}
          onClick={() => form.validateFields().then((v) => save.mutate(v))}
        >
          {t("common.save")}
        </Button>
      </div>
      <div className="sc-page-body">
        <Form form={form} layout="vertical" initialValues={query.data ?? DEFAULTS} style={{ maxWidth: 680 }}>
          <Card size="small" title={t("set.conv.autoAssign")} style={{ marginBottom: 16 }}>
            <Form.Item name="auto_assign_mode" style={{ marginBottom: 12 }}>
              <Radio.Group
                options={[
                  { label: t("set.conv.autoAssign.off"), value: "off" },
                  { label: t("set.conv.autoAssign.roundRobin"), value: "round_robin" },
                  { label: t("set.conv.autoAssign.leastBusy"), value: "least_busy" },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="ai_first"
              label={t("set.conv.aiFirst")}
              extra={t("set.conv.aiFirstHint")}
              valuePropName="checked"
              style={{ marginBottom: 12 }}
            >
              <Switch />
            </Form.Item>
            <Form.Item
              name="bot_first"
              label={t("set.conv.botFirst")}
              extra={t("set.conv.botFirstHint")}
              valuePropName="checked"
              style={{ marginBottom: 0 }}
            >
              <Switch />
            </Form.Item>
          </Card>

          <Card size="small" title={t("set.conv.keepManaged")} style={{ marginBottom: 16 }}>
            <Form.Item
              name="keep_managed"
              extra={t("set.conv.keepManagedHint")}
              valuePropName="checked"
              style={{ marginBottom: 0 }}
            >
              <Switch />
            </Form.Item>
          </Card>

          <Card size="small" title={t("set.conv.autoClose")} style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <Form.Item name="auto_close_days" noStyle>
                <InputNumber min={0} max={30} />
              </Form.Item>
              <span>{t("set.conv.autoCloseDays")}</span>
              <Form.Item name="auto_close_hours" noStyle>
                <InputNumber min={0} max={23} />
              </Form.Item>
              <span>{t("set.conv.autoCloseHours")}</span>
              <Form.Item name="auto_close_minutes" noStyle>
                <InputNumber min={0} max={59} />
              </Form.Item>
              <span>{t("set.conv.autoCloseMinutes")}</span>
            </div>
            <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", marginTop: 8 }}>
              {t("set.conv.autoCloseHint")}
            </div>
          </Card>

          <Card size="small" title={t("set.conv.offlineReply")}>
            <Form.Item name="offline_reply_mode" style={{ marginBottom: 0 }}>
              <Radio.Group
                options={[
                  { label: t("set.conv.offlineReply.email"), value: "email" },
                  { label: t("set.conv.offlineReply.widget"), value: "widget" },
                ]}
              />
            </Form.Item>
          </Card>
        </Form>
      </div>
    </div>
  );
}
