/** Super-admin platform Stripe key settings (訂閱總覽 ▸ 付款設定).
 *  Inputs Secret Key / Publishable Key / Webhook Secret → PUT
 *  /billing/stripe-config; the secrets are stored encrypted server-side and
 *  never echoed back — GET returns only the publishable key + booleans marking
 *  whether each secret is set. Blank secret fields on save leave the stored
 *  value unchanged. Degrades cleanly while the endpoint is not yet live. */
import { LockOutlined } from "@ant-design/icons";
import { App, Button, Card, Form, Input, Skeleton, Tag } from "antd";
import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { billingApi } from "@/api/endpoints";
import type { StripeConfigUpdate } from "@/api/types";
import { t } from "@/i18n";

interface StripeFormValues {
  publishable_key?: string;
  secret_key?: string;
  webhook_secret?: string;
}

export function StripeConfigCard() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<StripeFormValues>();

  const cfg = useQuery({
    queryKey: ["billing-stripe-config"],
    queryFn: () => billingApi.stripeConfig.get(),
    retry: 0,
  });

  // Prefill only the (public) publishable key once loaded; secrets stay blank.
  useEffect(() => {
    if (cfg.data) form.setFieldsValue({ publishable_key: cfg.data.publishable_key ?? "" });
  }, [cfg.data, form]);

  const save = useMutation({
    mutationFn: (body: StripeConfigUpdate) => billingApi.stripeConfig.save(body),
    onSuccess: () => {
      message.success(t("sub.stripe.saved"));
      form.setFieldsValue({ secret_key: "", webhook_secret: "" });
      void qc.invalidateQueries({ queryKey: ["billing-stripe-config"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const onFinish = (v: StripeFormValues) => {
    const body: StripeConfigUpdate = {};
    // Publishable key is public — always send (allows clearing). Secrets are
    // only sent when non-blank so a blank field keeps the stored value.
    if (v.publishable_key !== undefined) body.publishable_key = (v.publishable_key ?? "").trim();
    if (v.secret_key?.trim()) body.secret_key = v.secret_key.trim();
    if (v.webhook_secret?.trim()) body.webhook_secret = v.webhook_secret.trim();
    save.mutate(body);
  };

  // 404/501 while the backend endpoint is not yet deployed — show the form
  // anyway (saving works once it goes live) with a soft notice.
  const endpointMissing =
    cfg.isError && cfg.error instanceof ApiError && (cfg.error.status === 404 || cfg.error.status === 501);

  return (
    <Card
      size="small"
      style={{ marginTop: 16 }}
      title={
        <span>
          <LockOutlined /> {t("sub.stripe.title")}
        </span>
      }
      extra={
        cfg.data ? (
          <Tag color={cfg.data.configured ? "success" : "default"}>
            {cfg.data.configured ? t("sub.stripe.configured") : t("sub.stripe.notConfigured")}
          </Tag>
        ) : null
      }
    >
      <div className="sc-mkt-hint" style={{ marginBottom: 12 }}>{t("sub.stripe.hint")}</div>
      {cfg.isLoading ? (
        <Skeleton active paragraph={{ rows: 3 }} />
      ) : (
        <>
          {endpointMissing && (
            <div style={{ color: "var(--sc-warning)", fontSize: 12.5, marginBottom: 12 }}>
              {t("sub.stripe.loadFailed")}
            </div>
          )}
          <Form form={form} layout="vertical" onFinish={onFinish}>
            <Form.Item
              name="publishable_key"
              label={t("sub.stripe.publishableKey")}
            >
              <Input placeholder="pk_live_…" autoComplete="off" />
            </Form.Item>
            <Form.Item
              name="secret_key"
              label={t("sub.stripe.secretKey")}
              extra={cfg.data?.secret_key_set ? t("sub.stripe.secretSet") : undefined}
            >
              <Input.Password
                placeholder={cfg.data?.secret_key_set ? t("sub.stripe.secretPlaceholder") : "sk_live_…"}
                autoComplete="new-password"
              />
            </Form.Item>
            <Form.Item
              name="webhook_secret"
              label={t("sub.stripe.webhookSecret")}
              extra={cfg.data?.webhook_secret_set ? t("sub.stripe.secretSet") : undefined}
            >
              <Input.Password
                placeholder={cfg.data?.webhook_secret_set ? t("sub.stripe.secretPlaceholder") : "whsec_…"}
                autoComplete="new-password"
              />
            </Form.Item>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <span className="sc-mkt-hint" style={{ fontSize: 12 }}>{t("sub.stripe.encryptedNote")}</span>
              <Button type="primary" htmlType="submit" loading={save.isPending}>
                {t("sub.stripe.save")}
              </Button>
            </div>
          </Form>
        </>
      )}
    </Card>
  );
}
