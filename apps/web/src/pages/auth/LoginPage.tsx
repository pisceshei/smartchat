import { LockOutlined, MailOutlined } from "@ant-design/icons";
import { App, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { authApi } from "@/api/endpoints";
import { LogoMark } from "@/components/Logo";
import { t } from "@/i18n";
import { useAuthStore } from "@/stores/auth";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const setAuth = useAuthStore((s) => s.setAuth);
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);

  const onFinish = async (values: { email: string; password: string }) => {
    setLoading(true);
    try {
      const res = await authApi.login(values);
      setAuth(res.token, res.user, res.workspaces);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/inbox", { replace: true });
    } catch {
      message.error(t("auth.loginFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="sc-auth-bg">
      <Card style={{ width: 400, boxShadow: "var(--sc-shadow-lg)" }} styles={{ body: { padding: 32 } }}>
        <div style={{ textAlign: "center", marginBottom: 26 }}>
          <LogoMark size={44} />
          <Typography.Title level={3} style={{ marginTop: 14, marginBottom: 4 }}>
            {t("auth.login.title")}
          </Typography.Title>
          <Typography.Text type="secondary">{t("auth.login.subtitle")}</Typography.Text>
        </div>
        <Form layout="vertical" onFinish={onFinish} requiredMark={false} size="large">
          <Form.Item
            name="email"
            label={t("auth.email")}
            rules={[
              { required: true, message: t("common.required") },
              { type: "email", message: t("auth.emailInvalid") },
            ]}
          >
            <Input
              prefix={<MailOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
              placeholder="you@example.com"
              autoComplete="email"
            />
          </Form.Item>
          <Form.Item
            name="password"
            label={t("auth.password")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
              placeholder="••••••••"
              autoComplete="current-password"
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading} style={{ marginTop: 4 }}>
            {t("auth.login")}
          </Button>
        </Form>
        <div style={{ textAlign: "center", marginTop: 18 }}>
          <Link to="/register">{t("auth.toRegister")}</Link>
        </div>
      </Card>
    </div>
  );
}
