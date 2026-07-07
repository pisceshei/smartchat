/** 訂閱 › 訂閱總覽 — current plan card (方案/狀態/到期/餘額/AI點數/加購) +
 *  effective limits + quick actions + super-admin 手動切換方案 (no-charge self
 *  path → /billing/admin/change-plan). Contract: /billing/*. */
import { CrownOutlined, SwapOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { App, Button, Card, InputNumber, Segmented, Select, Skeleton, Tag } from "antd";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { billingApi } from "@/api/endpoints";
import type { CheckoutDuration, PlanLimits, SubscriptionAddons } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";
import { useIsSuperAdmin } from "./plan";
import "./billing.css";

const LIMIT_KEYS: (keyof PlanLimits)[] = [
  "seats",
  "official_channels",
  "hosted_devices",
  "monthly_active_contacts",
  "monthly_replies",
  "ai_points",
  "history_days",
];

const DURATIONS: CheckoutDuration[] = [7, 30, 90, 180, 360, 720];
const ADDON_KEYS: (keyof SubscriptionAddons)[] = ["seats", "official_channels", "hosted_devices"];

function StatTile({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="sc-stat-tile">
      <div className="sc-stat-tile-label">{label}</div>
      <div className="sc-stat-tile-value">{value}</div>
    </div>
  );
}

export function SubscriptionPage() {
  const navigate = useNavigate();
  const isSuperAdmin = useIsSuperAdmin();
  const sub = useQuery({ queryKey: ["billing-subscription"], queryFn: () => billingApi.subscription(), retry: 1 });

  if (sub.isLoading) {
    return (
      <div className="sc-page">
        <div className="sc-page-body">
          <Skeleton active paragraph={{ rows: 6 }} />
        </div>
      </div>
    );
  }

  if (sub.isError || !sub.data) {
    return (
      <div className="sc-page">
        <div className="sc-page-header">
          <h1 className="sc-page-title">{t("sub.nav.overview")}</h1>
        </div>
        <div className="sc-page-body">
          <EmptyState icon={<CrownOutlined />} title={t("rpt.loadFailed")} />
        </div>
      </div>
    );
  }

  const s = sub.data;
  const planName = t(`sub.plan.${s.plan_code}` as Parameters<typeof t>[0]);
  const statusKey = `sub.status.${s.status}` as Parameters<typeof t>[0];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("sub.nav.overview")}</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<ThunderboltOutlined />} onClick={() => navigate("/subscription/points")}>
            {t("sub.topup")}
          </Button>
          <Button type="primary" icon={<CrownOutlined />} onClick={() => navigate("/subscription/change-plan")}>
            {t("sub.changePlan")}
          </Button>
        </div>
      </div>
      <div className="sc-page-body" style={{ maxWidth: 900 }}>
        <Card size="small" style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
            <div style={{ width: 44, height: 44, borderRadius: 12, background: "var(--sc-primary-bg)", color: "var(--sc-primary)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 22 }}>
              <CrownOutlined />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 18, fontWeight: 700, color: "var(--sc-text-heading)" }}>{planName}</div>
              <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>
                {t("sub.current.expires")}: {s.current_period_end ? fullTime(s.current_period_end).slice(0, 10) : "—"}
              </div>
            </div>
            <Tag color={s.status === "active" || s.status === "trialing" ? "success" : "warning"}>
              {t(statusKey)}
            </Tag>
          </div>
          <div className="sc-stat-tiles">
            <StatTile label={t("sub.current.balance")} value={`$${s.balance.toFixed(2)}`} />
            <StatTile label={t("sub.current.aiPoints")} value={s.ai_points_balance.toLocaleString()} />
            {ADDON_KEYS.map((k) => (
              <StatTile key={k} label={t(`sub.addon.${k}` as Parameters<typeof t>[0])} value={`+${s.addons[k] ?? 0}`} />
            ))}
          </div>
        </Card>

        <Card size="small" title={t("sub.current.limits")} style={{ marginBottom: 16 }}>
          <div className="sc-stat-tiles">
            {LIMIT_KEYS.map((k) =>
              s.limits_effective[k] != null ? (
                <StatTile
                  key={k}
                  label={t(`sub.limit.${k}` as Parameters<typeof t>[0])}
                  value={String(s.limits_effective[k])}
                />
              ) : null,
            )}
          </div>
        </Card>

        {isSuperAdmin && <AdminSwitch />}
      </div>
    </div>
  );
}

/* --------------------------------------------------- super-admin plan switch */
function AdminSwitch() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const plans = useQuery({ queryKey: ["billing-plans"], queryFn: () => billingApi.plans(), retry: 1 });
  const [planCode, setPlanCode] = useState<string>();
  const [duration, setDuration] = useState<CheckoutDuration>(30);
  const [addons, setAddons] = useState<Partial<SubscriptionAddons>>({});

  const apply = useMutation({
    mutationFn: () =>
      billingApi.adminChangePlan({ plan_code: planCode as string, duration_days: duration, addons }),
    onSuccess: () => {
      message.success(t("sub.admin.done"));
      void qc.invalidateQueries({ queryKey: ["billing-subscription"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Card
      size="small"
      title={
        <span>
          <SwapOutlined /> {t("sub.admin.title")}
        </span>
      }
    >
      <div className="sc-mkt-hint" style={{ marginBottom: 12 }}>{t("sub.admin.hint")}</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
        <Select
          style={{ width: 160 }}
          placeholder={t("sub.cp.selectPlan")}
          value={planCode}
          onChange={setPlanCode}
          options={(plans.data ?? []).map((p) => ({ value: p.code, label: p.name }))}
        />
        <Segmented
          value={duration}
          onChange={(v) => setDuration(v as CheckoutDuration)}
          options={DURATIONS.map((d) => ({ value: d, label: t(`sub.cp.dur.${d}` as Parameters<typeof t>[0]) }))}
        />
        {ADDON_KEYS.map((k) => (
          <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span className="sc-mkt-hint">{t(`sub.addon.${k}` as Parameters<typeof t>[0])}</span>
            <InputNumber min={0} size="small" value={addons[k] ?? 0} onChange={(v) => setAddons((a) => ({ ...a, [k]: v ?? 0 }))} />
          </span>
        ))}
        <Button type="primary" loading={apply.isPending} disabled={!planCode} onClick={() => apply.mutate()}>
          {t("sub.admin.apply")}
        </Button>
      </div>
    </Card>
  );
}
