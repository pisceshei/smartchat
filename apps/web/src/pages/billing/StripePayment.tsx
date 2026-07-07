/** Stripe payment step. Two paths per the billing contract's stripe intent:
 *   - checkout_url  → redirect to a hosted Stripe Checkout Session.
 *   - client_secret → confirm a PaymentIntent inline with a Card Element
 *     (pure @stripe/stripe-js, no react wrapper needed).
 *  Degrades to a clear "billing disabled/not configured" state when the
 *  publishable key is missing or no intent is returned — never crashes. */
import { App, Button, Modal } from "antd";
import { useEffect, useRef, useState } from "react";
import type { Stripe, StripeCardElement } from "@stripe/stripe-js";
import type { StripeIntent } from "@/api/types";
import { t } from "@/i18n";
import "./billing.css";

const PUBLISHABLE_KEY = (import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY as string | undefined) ?? "";

export function StripePayment({
  open,
  intent,
  amountLabel,
  onClose,
  onSuccess,
}: {
  open: boolean;
  intent: StripeIntent | null;
  amountLabel?: string;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const { message } = App.useApp();
  const cardRef = useRef<HTMLDivElement>(null);
  const stripeRef = useRef<Stripe | null>(null);
  const cardElRef = useRef<StripeCardElement | null>(null);
  const [ready, setReady] = useState(false);
  const [paying, setPaying] = useState(false);

  const useCard = !intent?.checkout_url && !!intent?.client_secret && !!PUBLISHABLE_KEY;

  useEffect(() => {
    let cancelled = false;
    if (!open || !useCard) return;
    (async () => {
      try {
        const { loadStripe } = await import("@stripe/stripe-js");
        const stripe = await loadStripe(PUBLISHABLE_KEY);
        if (cancelled || !stripe || !cardRef.current) return;
        stripeRef.current = stripe;
        const elements = stripe.elements();
        const card = elements.create("card", { style: { base: { fontSize: "14px" } } });
        card.mount(cardRef.current);
        cardElRef.current = card;
        setReady(true);
      } catch {
        /* network / script blocked — leave not-ready */
      }
    })();
    return () => {
      cancelled = true;
      cardElRef.current?.unmount();
      cardElRef.current = null;
      setReady(false);
    };
  }, [open, useCard]);

  const payWithCard = async () => {
    if (!stripeRef.current || !cardElRef.current || !intent?.client_secret) return;
    setPaying(true);
    try {
      const res = await stripeRef.current.confirmCardPayment(intent.client_secret, {
        payment_method: { card: cardElRef.current },
      });
      if (res.error) {
        message.error(res.error.message ?? t("sub.pay.failed"));
      } else if (res.paymentIntent?.status === "succeeded") {
        message.success(t("sub.pay.success"));
        onSuccess();
      }
    } catch {
      message.error(t("sub.pay.failed"));
    } finally {
      setPaying(false);
    }
  };

  return (
    <Modal
      title={t("sub.pay.title")}
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnHidden
      width={440}
    >
      {!intent ? (
        <div style={{ color: "var(--sc-text-secondary)" }}>{t("sub.pay.disabled")}</div>
      ) : intent.checkout_url ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {amountLabel && <div style={{ fontSize: 20, fontWeight: 700 }}>{amountLabel}</div>}
          <Button type="primary" size="large" block onClick={() => (window.location.href = intent.checkout_url as string)}>
            {t("sub.pay.redirect")}
          </Button>
        </div>
      ) : !PUBLISHABLE_KEY ? (
        <div style={{ color: "var(--sc-warning)" }}>{t("sub.pay.notConfigured")}</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          {amountLabel && <div style={{ fontSize: 20, fontWeight: 700 }}>{amountLabel}</div>}
          <div>
            <div className="sc-mkt-hint" style={{ marginBottom: 6 }}>{t("sub.pay.card")}</div>
            <div className="sc-card-element" ref={cardRef} />
          </div>
          <Button
            type="primary"
            size="large"
            block
            loading={paying}
            disabled={!ready}
            onClick={payWithCard}
          >
            {t("sub.pay.pay")}
          </Button>
        </div>
      )}
    </Modal>
  );
}
