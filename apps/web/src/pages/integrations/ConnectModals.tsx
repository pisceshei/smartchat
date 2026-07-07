/** Channel connect modals — telegram token / meta OAuth placeholder /
 *  LINE credentials / email IMAP+SMTP / WhatsApp Cloud API / widget create. */
import { InfoCircleOutlined } from "@ant-design/icons";
import { Alert, App, Form, Input, InputNumber, Modal, Switch } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { channelsApi, widgetsApi } from "@/api/endpoints";
import type { ChannelType } from "@/api/types";
import { t } from "@/i18n";

function useConnect(channelType: ChannelType, onDone: () => void) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => channelsApi.connect(channelType, body),
    onSuccess: () => {
      message.success(t("common.createSuccess"));
      void qc.invalidateQueries({ queryKey: ["channel-accounts"] });
      onDone();
    },
    onError: () => message.error(t("common.operationFailed")),
  });
}

interface ModalProps {
  open: boolean;
  onClose: () => void;
}

export function TelegramConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("telegram_bot", onClose);
  return (
    <Modal
      title="Telegram Bot"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={connect.isPending}
      destroyOnHidden
    >
      <Alert type="info" showIcon icon={<InfoCircleOutlined />} message={t("int.tg.hint")} style={{ marginBottom: 16 }} />
      <Form form={form} layout="vertical" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input maxLength={30} />
        </Form.Item>
        <Form.Item
          name="bot_token"
          label={t("int.tg.token")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input.Password placeholder="123456789:AAF..." />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function MetaConnectModal({
  open,
  onClose,
  channelType,
}: ModalProps & { channelType: "messenger" | "instagram" }) {
  const [form] = Form.useForm();
  const connect = useConnect(channelType, onClose);
  return (
    <Modal
      title={t("int.meta.title")}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={connect.isPending}
      destroyOnHidden
    >
      <Alert type="info" showIcon message={t("int.meta.hint")} style={{ marginBottom: 16 }} />
      <Form form={form} layout="vertical" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input maxLength={30} />
        </Form.Item>
        <Form.Item
          name="page_id"
          label={t("int.meta.pageId")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="page_access_token"
          label={t("int.meta.pageToken")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input.Password />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function LineConnectModal({
  open,
  onClose,
  channelType,
}: ModalProps & { channelType: "line_oa" | "line_app" }) {
  const [form] = Form.useForm();
  const connect = useConnect(channelType, onClose);
  return (
    <Modal
      title={channelType === "line_oa" ? "LINE 官方帳號" : "LINE App"}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={connect.isPending}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input maxLength={30} />
        </Form.Item>
        <Form.Item
          name="channel_id"
          label={t("int.line.channelId")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="channel_secret"
          label={t("int.line.channelSecret")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input.Password />
        </Form.Item>
        <Form.Item
          name="channel_access_token"
          label={t("int.line.accessToken")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input.Password />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function EmailConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("email", onClose);
  return (
    <Modal
      title="Email"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={connect.isPending}
      width={560}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(v) => connect.mutate(v)}
        initialValues={{ imap_port: 993, smtp_port: 465, imap_ssl: true, smtp_ssl: true }}
      >
        <Form.Item
          name="address"
          label={t("int.email.address")}
          rules={[
            { required: true, message: t("common.required") },
            { type: "email", message: t("auth.emailInvalid") },
          ]}
        >
          <Input placeholder="support@example.com" />
        </Form.Item>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 90px", gap: 12 }}>
          <Form.Item
            name="imap_host"
            label={t("int.email.imapHost")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input placeholder="imap.example.com" />
          </Form.Item>
          <Form.Item name="imap_port" label={t("int.email.imapPort")}>
            <InputNumber min={1} max={65535} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="imap_ssl" label="SSL" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 90px", gap: 12 }}>
          <Form.Item
            name="smtp_host"
            label={t("int.email.smtpHost")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input placeholder="smtp.example.com" />
          </Form.Item>
          <Form.Item name="smtp_port" label={t("int.email.smtpPort")}>
            <InputNumber min={1} max={65535} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="smtp_ssl" label="SSL" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <Form.Item
            name="username"
            label={t("int.email.username")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="password"
            label={t("int.email.password")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input.Password />
          </Form.Item>
        </div>
      </Form>
    </Modal>
  );
}

export function WhatsAppApiConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("whatsapp_api", onClose);
  return (
    <Modal
      title="WhatsApp Business Cloud API"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={connect.isPending}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input maxLength={30} />
        </Form.Item>
        <Form.Item
          name="phone_number_id"
          label={t("int.wa.phoneNumberId")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="waba_id"
          label={t("int.wa.wabaId")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="access_token"
          label={t("int.wa.token")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input.Password />
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function WidgetCreateModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const create = useMutation({
    mutationFn: (values: { name: string; domain?: string }) => widgetsApi.create(values),
    onSuccess: (w) => {
      message.success(t("common.createSuccess"));
      void qc.invalidateQueries({ queryKey: ["widgets"] });
      onClose();
      navigate(`/integrations/widgets/${w.id}`);
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  return (
    <Modal
      title={t("widget.add")}
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText={t("common.create")}
      cancelText={t("common.cancel")}
      confirmLoading={create.isPending}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={(v) => create.mutate(v)}>
        <Form.Item
          name="name"
          label={t("widget.name")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input maxLength={30} />
        </Form.Item>
        <Form.Item name="domain" label={t("widget.domain")}>
          <Input placeholder="www.example.com" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
