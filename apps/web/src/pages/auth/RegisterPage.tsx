import { HomeOutlined, LockOutlined, MailOutlined, UserOutlined } from "@ant-design/icons";
import { App, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { authApi } from "@/api/endpoints";
import { LogoMark } from "@/components/Logo";
import { t } from "@/i18n";
import { useAuthStore } from "@/stores/auth";

interface RegisterForm {
  name: string;
  email: string;
  password: string;
  password_confirm: string;
  workspace_name: string;
}

export function RegisterPage() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((s) => s.setAuth);
  const { message } = App.useApp();
  const [loading, setLoading] = useState(false);

  const onFinish = async (values: RegisterForm) => {
    setLoading(true);
    try {
      const res = await authApi.register({
        name: values.name,
        email: values.email.trim().toLowerCase(),
        password: values.password,
        workspace_name: values.workspace_name,
      });
      setAuth(res.token, res.user, res.workspaces, res.refreshToken);
      navigate("/inbox", { replace: true });
    } catch {
      message.error(t("auth.registerFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="sc-auth-bg">
      <Card style={{ width: 420, boxShadow: "var(--sc-shadow-lg)" }} styles={{ body: { padding: 32 } }}>
        <div style={{ textAlign: "center", marginBottom: 24 }}>
          <LogoMark size={44} />
          <Typography.Title level={3} style={{ marginTop: 14, marginBottom: 4 }}>
            {t("auth.register.title")}
          </Typography.Title>
          <Typography.Text type="secondary">{t("auth.register.subtitle")}</Typography.Text>
        </div>
        <Form layout="vertical" onFinish={onFinish} requiredMark={false}>
          <Form.Item
            name="name"
            label={t("auth.name")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input prefix={<UserOutlined style={{ color: "var(--sc-text-tertiary)" }} />} />
          </Form.Item>
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
              autoComplete="email"
            />
          </Form.Item>
          <Form.Item
            name="workspace_name"
            label={t("auth.workspaceName")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input prefix={<HomeOutlined style={{ color: "var(--sc-text-tertiary)" }} />} />
          </Form.Item>
          <Form.Item
            name="password"
            label={t("auth.password")}
            rules={[
              { required: true, message: t("common.required") },
              { min: 8, message: t("auth.passwordMin") },
            ]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
              autoComplete="new-password"
            />
          </Form.Item>
          <Form.Item
            name="password_confirm"
            label={t("auth.passwordConfirm")}
            dependencies={["password"]}
            rules={[
              { required: true, message: t("common.required") },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("password") === value) return Promise.resolve();
                  return Promise.reject(new Error(t("auth.passwordMismatch")));
                },
              }),
            ]}
          >
            <Input.Password
              prefix={<LockOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
              autoComplete="new-password"
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading} size="large">
            {t("auth.register")}
          </Button>
        </Form>
        <div style={{ textAlign: "center", marginTop: 18 }}>
          <Link to="/login">{t("auth.toLogin")}</Link>
        </div>
      </Card>
    </div>
  );
}
