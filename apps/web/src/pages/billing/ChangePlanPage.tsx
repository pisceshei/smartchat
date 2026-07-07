/** 訂閱 › 變更套餐 — replicates the captured upgrade flow: 選方案 (Pro/Max) +
 *  時長階梯 (7 試用/30/90-10%/180-15%/360-20%/720-25%) + 加購 steppers + 訂單
 *  預覽 (原價/優惠/手續費/餘額折抵/應付) + Stripe 付款. Contract: /billing/*. */
import { CheckCircleFilled } from "@ant-design/icons";
import { App, Button, Checkbox, InputNumber, Skeleton } from "antd";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  billingApi,
  DURATION_DISCOUNT,
  estimateOrder,
  type CheckoutBody,
} from "@/api/endpoints";
import type {
  CheckoutDuration,
  CheckoutResult,
  OrderPreview,
  Plan,
  SubscriptionAddons,
} from "@/api/types";
import { t } from "@/i18n";
import { StripePayment } from "./StripePayment";
import "./billing.css";

const DURATIONS: CheckoutDuration[] = [7, 30, 90, 180, 360, 720];

/** Addon unit monthly prices for the LIVE estimate only — the authoritative
 *  amounts come back from /billing/checkout. */
const ADDON_UNIT_PRICE: Record<keyof SubscriptionAddons, number> = {
  seats: 5,
  official_channels: 10,
  hosted_devices: 15,
};

const ADDON_KEYS: (keyof SubscriptionAddons)[] = ["seats", "official_channels", "hosted_devices"];

function money(n: number, currency = "USD"): string {
  return `${currency === "USD" ? "$" : ""}${n.toFixed(2)}`;
}

export function ChangePlanPage() {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const navigate = useNavigate();

  const plans = useQuery({ queryKey: ["billing-plans"], queryFn: () => billingApi.plans(), retry: 1 });
  const sub = useQuery({ queryKey: ["billing-subscription"], queryFn: () => billingApi.subscription(), retry: 1 });

  const [planCode, setPlanCode] = useState<string | null>(null);
  const [duration, setDuration] = useState<CheckoutDuration>(30);
  const [addons, setAddons] = useState<Partial<SubscriptionAddons>>({});
  const [useBalance, setUseBalance] = useState(false);
  const [payIntent, setPayIntent] = useState<CheckoutResult | null>(null);

  const publicPlans = (plans.data ?? []).filter((p) => p.is_public && p.price_monthly > 0);
  const selectedPlan: Plan | undefined =
    publicPlans.find((p) => p.code === planCode) ?? publicPlans[0];
  const effectivePlanCode = planCode ?? selectedPlan?.code ?? null;

  const addonMonthly = useMemo(
    () => ADDON_KEYS.reduce((sum, k) => sum + (addons[k] ?? 0) * ADDON_UNIT_PRICE[k], 0),
    [addons],
  );

  const order: OrderPreview | null = useMemo(() => {
    if (!selectedPlan) return null;
    return estimateOrder({
      plan_monthly: selectedPlan.price_monthly,
      duration_days: duration,
      addon_monthly_total: addonMonthly,
      balance: sub.data?.balance ?? 0,
      use_balance: useBalance,
    });
  }, [selectedPlan, duration, addonMonthly, useBalance, sub.data?.balance]);

  const checkout = useMutation({
    mutationFn: () => {
      const body: CheckoutBody = {
        plan_code: effectivePlanCode as string,
        duration_days: duration,
        addons,
        use_balance: useBalance,
      };
      return billingApi.checkout(body);
    },
    onSuccess: (res) => setPayIntent(res),
    onError: () => message.error(t("common.operationFailed")),
  });

  const onPaid = () => {
    setPayIntent(null);
    void qc.invalidateQueries({ queryKey: ["billing-subscription"] });
    navigate("/subscription");
  };

  if (plans.isLoading) {
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
        <h1 className="sc-page-title">{t("sub.cp.title")}</h1>
      </div>
      <div className="sc-page-body" style={{ maxWidth: 900 }}>
        {/* plan select */}
        <Section title={t("sub.cp.selectPlan")}>
          <div className="sc-plan-grid">
            {publicPlans.map((p) => (
              <div
                key={p.code}
                className={`sc-plan-card${effectivePlanCode === p.code ? " sc-active" : ""}`}
                onClick={() => setPlanCode(p.code)}
                role="button"
                tabIndex={0}
              >
                {effectivePlanCode === p.code && (
                  <CheckCircleFilled style={{ position: "absolute", top: 14, right: 14, color: "var(--sc-primary)", fontSize: 18 }} />
                )}
                <div className="sc-plan-name">{p.name}</div>
                <div className="sc-plan-price">
                  {money(p.price_monthly, "USD")}
                  <small>{t("sub.cp.perMonth")}</small>
                </div>
                <ul className="sc-plan-limits">
                  {ADDON_KEYS.map((k) =>
                    p.limits[k] != null ? (
                      <li key={k}>
                        <CheckCircleFilled style={{ color: "var(--sc-success)", fontSize: 12 }} />
                        {t(`sub.limit.${k}` as Parameters<typeof t>[0])}: {String(p.limits[k])}
                      </li>
                    ) : null,
                  )}
                  {p.limits.ai_points != null && (
                    <li>
                      <CheckCircleFilled style={{ color: "var(--sc-success)", fontSize: 12 }} />
                      {t("sub.limit.ai_points")}: {String(p.limits.ai_points)}
                    </li>
                  )}
                </ul>
              </div>
            ))}
          </div>
        </Section>

        {/* duration */}
        <Section title={t("sub.cp.duration")}>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {DURATIONS.map((d) => {
              const pct = Math.round(DURATION_DISCOUNT[d] * 100);
              return (
                <div className="sc-dur-chip" key={d}>
                  <Button
                    type={duration === d ? "primary" : "default"}
                    onClick={() => setDuration(d)}
                  >
                    {t(`sub.cp.dur.${d}` as Parameters<typeof t>[0])}
                  </Button>
                  {pct > 0 && <span className="sc-dur-save">{t("sub.cp.save", { pct })}</span>}
                </div>
              );
            })}
          </div>
        </Section>

        {/* addons */}
        <Section title={t("sub.cp.addons")}>
          <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 420 }}>
            {ADDON_KEYS.map((k) => (
              <div key={k} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span style={{ flex: 1, fontSize: 14 }}>
                  {t(`sub.addon.${k}` as Parameters<typeof t>[0])}
                  <span className="sc-mkt-hint" style={{ display: "inline", marginLeft: 8 }}>
                    {money(ADDON_UNIT_PRICE[k])}{t("sub.cp.perMonth")}／{t("sub.cp.addonUnit")}
                  </span>
                </span>
                <InputNumber
                  min={0}
                  value={addons[k] ?? 0}
                  onChange={(v) => setAddons((a) => ({ ...a, [k]: v ?? 0 }))}
                />
              </div>
            ))}
          </div>
        </Section>

        {/* order preview */}
        {order && (
          <Section title={t("sub.cp.order")}>
            <div className="sc-order" style={{ maxWidth: 420 }}>
              <div className="sc-order-row">
                <span>{t("sub.cp.basePrice")}</span>
                <span>{money(order.base_price, order.currency)}</span>
              </div>
              {order.discount > 0 && (
                <div className="sc-order-row">
                  <span>{t("sub.cp.orderDiscount")}</span>
                  <span className="sc-discount">−{money(order.discount, order.currency)}</span>
                </div>
              )}
              <div className="sc-order-row">
                <span>{t("sub.cp.handlingFee")}</span>
                <span>{money(order.handling_fee, order.currency)}</span>
              </div>
              <div className="sc-order-row">
                <Checkbox checked={useBalance} onChange={(e) => setUseBalance(e.target.checked)}>
                  {t("sub.cp.useBalance")}
                  {sub.data ? ` (${money(sub.data.balance, order.currency)})` : ""}
                </Checkbox>
                <span className="sc-discount">
                  {order.balance_applied > 0 ? `−${money(order.balance_applied, order.currency)}` : ""}
                </span>
              </div>
              <div className="sc-order-row sc-order-total">
                <span>{t("sub.cp.amountDue")}</span>
                <span>{money(order.amount_due, order.currency)}</span>
              </div>
            </div>
            <div className="sc-mkt-hint" style={{ marginTop: 8 }}>{t("sub.cp.estimateHint")}</div>
            <Button
              type="primary"
              size="large"
              style={{ marginTop: 14 }}
              loading={checkout.isPending}
              disabled={!effectivePlanCode}
              onClick={() => checkout.mutate()}
            >
              {t("sub.cp.checkout")}
            </Button>
          </Section>
        )}
      </div>

      <StripePayment
        open={!!payIntent}
        intent={payIntent?.stripe ?? null}
        amountLabel={payIntent ? money(payIntent.order.amount_due, payIntent.order.currency) : undefined}
        onClose={() => setPayIntent(null)}
        onSuccess={onPaid}
      />
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 26 }}>
      <div style={{ fontSize: 14, fontWeight: 600, color: "var(--sc-text-heading)", marginBottom: 12 }}>{title}</div>
      {children}
    </div>
  );
}
