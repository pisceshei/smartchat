/** Channel connect modals — one per connectable channel type. Each modal
 *  collects the flat credential/config fields the backend connect route splits
 *  into envelope-encrypted credentials + config, then POSTs
 *  /channels/{channel_type}/accounts via channelsApi.connect.
 *
 *  Conventions (mirror telegram/line/email/meta P1 modals):
 *   - useConnect(channelType) → mutation with success/err toast + cache invalidate
 *   - flat snake_case field names round-trip to the backend verbatim
 *   - secrets use Input.Password; conditional credential fields use
 *     preserve={false} so a hidden branch's stale value is never submitted. */
import { InfoCircleOutlined } from "@ant-design/icons";
import { Alert, App, Button, Form, Input, InputNumber, Modal, Result, Segmented, Select, Spin, Switch } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiError } from "@/api/client";
import { channelsApi, devicesApi, widgetsApi } from "@/api/endpoints";
import type { BridgeDeviceStatus, ChannelType } from "@/api/types";
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
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
  });
}

interface ModalProps {
  open: boolean;
  onClose: () => void;
}

const REQUIRED = [{ required: true, message: t("common.required") }];

/** Official-doc URLs for the 「如何取得？」 helper link per channel. */
const DOC_LINKS: Partial<Record<ChannelType, string>> = {
  slack: "https://api.slack.com/apps",
  vk: "https://dev.vk.com/en/api/callback/getting-started",
  wechat_kf: "https://kf.weixin.qq.com/",
  wecom: "https://work.weixin.qq.com/api/doc",
  zalo_app: "https://developers.zalo.me/",
  youtube: "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
  tiktok_business: "https://business-api.tiktok.com/portal",
};

/** Info banner with the channel hint + an optional 官方文檔 link. */
function ConnectHint({ hint, doc }: { hint: string; doc?: string }) {
  return (
    <Alert
      type="info"
      showIcon
      icon={<InfoCircleOutlined />}
      message={hint}
      description={
        doc ? (
          <a href={doc} target="_blank" rel="noreferrer">
            {t("int.howto")}
          </a>
        ) : undefined
      }
      style={{ marginBottom: 16 }}
    />
  );
}

/** Shared Modal chrome so every connect modal renders identically. */
function ConnectModalShell({
  title,
  open,
  onClose,
  onOk,
  loading,
  width,
  children,
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  onOk: () => void;
  loading: boolean;
  width?: number;
  children: React.ReactNode;
}) {
  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      onOk={onOk}
      okText={t("int.connect")}
      cancelText={t("common.cancel")}
      confirmLoading={loading}
      width={width}
      destroyOnHidden
    >
      {/* 隱藏誘餌欄位 — 吸收瀏覽器密碼管理器的自動填充 */}
      <div style={{ display: "none" }} aria-hidden>
        <input type="text" autoComplete="username" />
        <input type="password" autoComplete="current-password" />
      </div>
      {children}
    </Modal>
  );
}

export function TelegramConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("telegram_bot", onClose);
  return (
    <ConnectModalShell
      title="Telegram Bot"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.tg.hint")} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="bot_token" label={t("int.tg.token")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" placeholder="123456789:AAF..." />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function MetaConnectModal({
  open,
  onClose,
  channelType,
}: ModalProps & { channelType: "messenger" | "instagram" }) {
  const [form] = Form.useForm();
  const connect = useConnect(channelType, onClose);
  const isIg = channelType === "instagram";
  const loginType = (Form.useWatch("login_type", form) as string) ?? "page";
  const igDirect = isIg && loginType === "ig";
  return (
    <ConnectModalShell
      title={t("int.meta.title")}
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={igDirect ? t("int.ig.hint") : t("int.meta.hint")} />
      <Form
        form={form}
        layout="vertical"
        autoComplete="off"
        initialValues={{ login_type: "page" }}
        onFinish={(v) => connect.mutate(v)}
      >
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        {isIg && (
          <Form.Item name="login_type" label={t("int.ig.loginType")}>
            <Segmented
              block
              options={[
                { label: t("int.ig.login.page"), value: "page" },
                { label: t("int.ig.login.ig"), value: "ig" },
              ]}
            />
          </Form.Item>
        )}
        {igDirect ? (
          <>
            <Form.Item name="ig_user_id" label={t("int.ig.igUserId")} rules={REQUIRED} preserve={false}>
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="access_token"
              label={t("int.ig.accessToken")}
              rules={REQUIRED}
              preserve={false}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </>
        ) : (
          <>
            <Form.Item name="page_id" label={t("int.meta.pageId")} rules={REQUIRED} preserve={false}>
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="page_access_token"
              label={t("int.meta.pageToken")}
              rules={REQUIRED}
              preserve={false}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </>
        )}
      </Form>
    </ConnectModalShell>
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
    <ConnectModalShell
      title={channelType === "line_oa" ? "LINE 官方帳號" : "LINE App"}
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="channel_id" label={t("int.line.channelId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="channel_secret" label={t("int.line.channelSecret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="channel_access_token" label={t("int.line.accessToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function EmailConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("email", onClose);
  const authType = (Form.useWatch("auth_type", form) as string) ?? "password";
  return (
    <ConnectModalShell
      title="Email"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
      width={560}
    >
      <Form
        form={form}
        layout="vertical"
        autoComplete="off"
        onFinish={(v) => connect.mutate(v)}
        initialValues={{
          imap_port: 993,
          smtp_port: 465,
          imap_ssl: true,
          smtp_ssl: true,
          auth_type: "password",
          oauth_provider: "gmail",
        }}
      >
        <Form.Item
          name="address"
          label={t("int.email.address")}
          rules={[
            { required: true, message: t("common.required") },
            { type: "email", message: t("auth.emailInvalid") },
          ]}
        >
          <Input autoComplete="off" placeholder="support@example.com" />
        </Form.Item>
        <Form.Item name="auth_type" label={t("int.email.authType")}>
          <Segmented
            block
            options={[
              { label: t("int.email.auth.password"), value: "password" },
              { label: t("int.email.auth.oauth2"), value: "oauth2" },
            ]}
          />
        </Form.Item>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 90px", gap: 12 }}>
          <Form.Item name="imap_host" label={t("int.email.imapHost")} rules={REQUIRED}>
            <Input autoComplete="off" placeholder="imap.example.com" />
          </Form.Item>
          <Form.Item name="imap_port" label={t("int.email.imapPort")}>
            <InputNumber min={1} max={65535} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="imap_ssl" label="SSL" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 120px 90px", gap: 12 }}>
          <Form.Item name="smtp_host" label={t("int.email.smtpHost")} rules={REQUIRED}>
            <Input autoComplete="off" placeholder="smtp.example.com" />
          </Form.Item>
          <Form.Item name="smtp_port" label={t("int.email.smtpPort")}>
            <InputNumber min={1} max={65535} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="smtp_ssl" label="SSL" valuePropName="checked">
            <Switch />
          </Form.Item>
        </div>
        {authType === "password" ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Form.Item name="username" label={t("int.email.username")} rules={REQUIRED}>
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item name="password" label={t("int.email.password")} rules={REQUIRED} preserve={false}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </div>
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              icon={<InfoCircleOutlined />}
              message={t("int.email.oauthHint")}
              style={{ marginBottom: 16 }}
            />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Form.Item name="username" label={t("int.email.username")} rules={REQUIRED}>
                <Input autoComplete="off" placeholder="support@gmail.com" />
              </Form.Item>
              <Form.Item name="oauth_provider" label={t("int.email.oauthProvider")}>
                <Select
                  options={[
                    { label: "Gmail", value: "gmail" },
                    { label: "Outlook", value: "outlook" },
                    { label: t("common.more"), value: "custom" },
                  ]}
                />
              </Form.Item>
            </div>
            <Form.Item
              name="oauth_access_token"
              label={t("int.email.oauthAccessToken")}
              rules={REQUIRED}
              preserve={false}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
            <Form.Item
              name="oauth_refresh_token"
              label={t("int.email.oauthRefreshToken")}
              preserve={false}
            >
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </>
        )}
      </Form>
    </ConnectModalShell>
  );
}

export function WhatsAppApiConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("whatsapp_api", onClose);
  const bsp = (Form.useWatch("bsp", form) as string) ?? "cloud";
  const isCloud = bsp === "cloud";
  return (
    <ConnectModalShell
      title="WhatsApp Business Cloud API"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <Form
        form={form}
        layout="vertical"
        autoComplete="off"
        initialValues={{ bsp: "cloud" }}
        onFinish={(v) => connect.mutate(v)}
      >
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="bsp" label={t("int.wa.bsp")}>
          <Select
            options={[
              { label: t("int.wa.bsp.cloud"), value: "cloud" },
              { label: "YCloud", value: "ycloud" },
              { label: "ChatApp", value: "chatapp" },
              { label: "NxCloud", value: "nxcloud" },
              { label: "ITNIO", value: "itnio" },
            ]}
          />
        </Form.Item>
        <Form.Item name="phone_number_id" label={t("int.wa.phoneNumberId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        {isCloud ? (
          <>
            <Form.Item name="waba_id" label={t("int.wa.wabaId")} rules={REQUIRED} preserve={false}>
              <Input autoComplete="off" />
            </Form.Item>
            <Form.Item name="access_token" label={t("int.wa.token")} rules={REQUIRED} preserve={false}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </>
        ) : (
          <>
            <Alert
              type="info"
              showIcon
              icon={<InfoCircleOutlined />}
              message={t("int.wa.bspHint")}
              style={{ marginBottom: 16 }}
            />
            <Form.Item name="api_key" label={t("int.wa.apiKey")} rules={REQUIRED} preserve={false}>
              <Input.Password autoComplete="new-password" />
            </Form.Item>
          </>
        )}
      </Form>
    </ConnectModalShell>
  );
}

/* ------------------------------------------- WhatsApp/LINE App — QR bridge */

/** Terminal statuses that stop the poll loop. `online` = paired successfully;
 *  `logged_out`/`banned` require operator action (the bridge never auto-re-pairs). */
const BRIDGE_TERMINAL: readonly BridgeDeviceStatus[] = ["online", "logged_out", "banned"];

/** Small under-QR hint per transient status. */
const BRIDGE_STATUS_HINT: Partial<Record<BridgeDeviceStatus, string>> = {
  provisioning: t("int.waApp.st.provisioning"),
  awaiting_qr: t("int.waApp.st.awaitingQr"),
  connecting: t("int.waApp.st.connecting"),
  pairing: t("int.waApp.st.pairing"),
};

/** QR-scan connect flow for personal-number channels (whatsapp_app / line_app).
 *  On open it provisions a bridge device then polls qr()+status() every 2s,
 *  rendering the QR string as an image until the device reports "online".
 *  Handles awaiting_qr/connecting/logged_out/banned with clear messages and a
 *  「重新產生 QR」 action that restarts the whole flow (safe after logout — it
 *  provisions a fresh session rather than auto-re-pairing). */
function QrBridgeConnectModal({
  open,
  onClose,
  channelType,
  title,
}: ModalProps & { channelType: ChannelType; title: string }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [accountId, setAccountId] = useState<string | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [status, setStatus] = useState<BridgeDeviceStatus | null>(null);
  const [info, setInfo] = useState<{ phone?: string | null; pushname?: string | null }>({});
  const [err, setErr] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const doneRef = useRef(false);
  // Keep the latest onClose without re-subscribing the poll effect (the parent
  // passes a fresh closure each render).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const stop = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    stop();
    doneRef.current = false;
    setAccountId(null);
    setQr(null);
    setStatus(null);
    setInfo({});
    setErr(null);
    setStarting(true);
    try {
      const acct = await devicesApi.connect(channelType, {});
      if (!acct.account_id) throw new Error("missing account id");
      setStatus(acct.status);
      setAccountId(acct.account_id);
    } catch (e) {
      const detail = e instanceof ApiError && e.message ? e.message : null;
      setErr(detail ?? t("int.waApp.startFailed"));
    } finally {
      setStarting(false);
    }
  }, [channelType, stop]);

  // (re)start when the modal opens; tear down the loop when it closes.
  useEffect(() => {
    if (open) void start();
    return () => stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Poll qr + status in parallel once we have an account id.
  useEffect(() => {
    if (!accountId || !open) return;
    let cancelled = false;
    const tick = async () => {
      const [qrRes, stRes] = await Promise.allSettled([
        devicesApi.qr(channelType, accountId),
        devicesApi.status(channelType, accountId),
      ]);
      if (cancelled) return;
      let next: BridgeDeviceStatus | null = null;
      if (qrRes.status === "fulfilled") {
        setQr(qrRes.value.qr);
        next = qrRes.value.status ?? next;
      }
      if (stRes.status === "fulfilled") {
        next = stRes.value.status ?? next;
        setInfo({ phone: stRes.value.phone, pushname: stRes.value.pushname });
      }
      if (next) setStatus(next);
      if (next && BRIDGE_TERMINAL.includes(next) && !doneRef.current) {
        doneRef.current = true;
        stop();
        if (next === "online") {
          setQr(null);
          void qc.invalidateQueries({ queryKey: ["channel-accounts"] });
          message.success(t("int.waApp.onlineToast"));
          window.setTimeout(() => {
            if (!cancelled) onCloseRef.current();
          }, 1800);
        }
      }
    };
    void tick();
    timerRef.current = setInterval(tick, 2000);
    return () => {
      cancelled = true;
      stop();
    };
  }, [accountId, open, channelType, qc, message, stop]);

  const online = status === "online";
  const banned = status === "banned";
  const loggedOut = status === "logged_out";
  const statusHint = status ? BRIDGE_STATUS_HINT[status] : undefined;

  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      destroyOnHidden
      width={420}
      footer={
        online ? (
          <Button type="primary" onClick={onClose}>
            {t("common.close")}
          </Button>
        ) : (
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <Button onClick={onClose}>{t("common.cancel")}</Button>
            <Button type="primary" loading={starting} onClick={() => void start()}>
              {t("int.waApp.regen")}
            </Button>
          </div>
        )
      }
    >
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 14, padding: "4px 0" }}>
        {!online && (
          <Alert
            type="warning"
            showIcon
            message={t("int.waApp.banRisk")}
            style={{ width: "100%" }}
          />
        )}
        {online ? (
          <Result
            style={{ padding: "16px 0" }}
            status="success"
            title={t("int.waApp.online")}
            subTitle={
              [info.pushname, info.phone].filter(Boolean).join(" · ") || t("int.waApp.onlineSub")
            }
          />
        ) : err ? (
          <Alert type="error" showIcon message={t("int.waApp.startFailed")} description={err} style={{ width: "100%" }} />
        ) : banned ? (
          <Alert type="error" showIcon message={t("int.waApp.banned")} style={{ width: "100%" }} />
        ) : loggedOut ? (
          <Alert type="warning" showIcon message={t("int.waApp.loggedOut")} style={{ width: "100%" }} />
        ) : qr ? (
          <>
            <div style={{ background: "#fff", padding: 12, borderRadius: 10, lineHeight: 0 }}>
              <QRCodeSVG value={qr} size={220} level="M" marginSize={2} />
            </div>
            <div style={{ textAlign: "center", color: "var(--sc-text-secondary)", fontSize: 13, lineHeight: 1.7 }}>
              {t("int.waApp.scanHint")}
            </div>
            {statusHint && (
              <div style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>{statusHint}</div>
            )}
          </>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12, padding: "28px 0" }}>
            <Spin />
            <div style={{ color: "var(--sc-text-secondary)", fontSize: 13 }}>
              {starting ? t("int.waApp.starting") : statusHint ?? t("int.waApp.waitingQr")}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

export function WhatsAppAppConnectModal({ open, onClose }: ModalProps) {
  return (
    <QrBridgeConnectModal open={open} onClose={onClose} channelType="whatsapp_app" title="WhatsApp App" />
  );
}

export function SlackConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("slack", onClose);
  return (
    <ConnectModalShell
      title="Slack"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.slack.hint")} doc={DOC_LINKS.slack} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="bot_token" label={t("int.slack.botToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" placeholder="xoxb-..." />
        </Form.Item>
        <Form.Item name="signing_secret" label={t("int.slack.signingSecret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function VkConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("vk", onClose);
  return (
    <ConnectModalShell
      title="VKontakte"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.vk.hint")} doc={DOC_LINKS.vk} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="access_token" label={t("int.vk.token")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="group_id" label={t("int.vk.groupId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="confirmation" label={t("int.vk.confirmation")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="secret" label={t("int.vk.secret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function WeChatKfConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("wechat_kf", onClose);
  return (
    <ConnectModalShell
      title="微信客服"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.wxkf.hint")} doc={DOC_LINKS.wechat_kf} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="corp_id" label={t("int.wxkf.corpId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="secret" label={t("int.wxkf.secret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="token" label={t("int.wxkf.token")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="encoding_aes_key" label={t("int.wxkf.aesKey")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function WeComConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("wecom", onClose);
  return (
    <ConnectModalShell
      title="企業微信"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.wecom.hint")} doc={DOC_LINKS.wecom} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="corp_id" label={t("int.wecom.corpId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="agent_id" label={t("int.wecom.agentId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="secret" label={t("int.wecom.secret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="token" label={t("int.wecom.token")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="encoding_aes_key" label={t("int.wecom.aesKey")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function ZaloConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("zalo_app", onClose);
  return (
    <ConnectModalShell
      title="Zalo Official Account"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.zalo.hint")} doc={DOC_LINKS.zalo_app} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="oa_id" label={t("int.zalo.oaId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="app_id" label={t("int.zalo.appId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="app_secret" label={t("int.zalo.appSecret")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="access_token" label={t("int.zalo.accessToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="refresh_token" label={t("int.zalo.refreshToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function YouTubeConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("youtube", onClose);
  const { message } = App.useApp();
  return (
    <ConnectModalShell
      title="YouTube 評論"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.youtube.hint")} doc={DOC_LINKS.youtube} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Button block onClick={() => message.info(t("int.oauthPlaceholder"))} style={{ marginBottom: 16 }}>
          {t("int.youtube.goOauth")}
        </Button>
        <Form.Item name="channel_id" label={t("int.youtube.channelId")} rules={REQUIRED}>
          <Input autoComplete="off" placeholder="UC..." />
        </Form.Item>
        <Form.Item name="access_token" label={t("int.youtube.accessToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item name="refresh_token" label={t("int.youtube.refreshToken")}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
  );
}

export function TikTokBusinessConnectModal({ open, onClose }: ModalProps) {
  const [form] = Form.useForm();
  const connect = useConnect("tiktok_business", onClose);
  return (
    <ConnectModalShell
      title="TikTok 商業號"
      open={open}
      onClose={onClose}
      onOk={() => form.submit()}
      loading={connect.isPending}
    >
      <ConnectHint hint={t("int.tiktok.hint")} doc={DOC_LINKS.tiktok_business} />
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => connect.mutate(v)}>
        <Form.Item name="name" label={t("int.name")}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="business_id" label={t("int.tiktok.businessId")} rules={REQUIRED}>
          <Input autoComplete="off" />
        </Form.Item>
        <Form.Item name="access_token" label={t("int.tiktok.accessToken")} rules={REQUIRED}>
          <Input.Password autoComplete="new-password" />
        </Form.Item>
      </Form>
    </ConnectModalShell>
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
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
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
      <Form form={form} layout="vertical" autoComplete="off" onFinish={(v) => create.mutate(v)}>
        <Form.Item name="name" label={t("widget.name")} rules={REQUIRED}>
          <Input autoComplete="off" maxLength={30} />
        </Form.Item>
        <Form.Item name="domain" label={t("widget.domain")}>
          <Input autoComplete="off" placeholder="www.example.com" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
