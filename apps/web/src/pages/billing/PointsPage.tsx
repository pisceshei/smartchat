/** 訂閱 › AI 點數 — balance + top-up ($0.375 / 10k points) via Stripe + append-
 *  only points ledger with cursor pagination. Contract: /billing/points/*. */
import { ThunderboltOutlined } from "@ant-design/icons";
import { App, Button, Card, Empty, InputNumber, Skeleton, Table, Tag } from "antd";
import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { billingApi } from "@/api/endpoints";
import type { PointsLedgerEntry, PointsTopupResult, StripeIntent } from "@/api/types";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";
import { StripePayment } from "./StripePayment";
import "./billing.css";

const PRICE_PER_10K = 0.375;
const STEP = 10_000;

export function PointsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [points, setPoints] = useState(50_000);
  const [intent, setIntent] = useState<StripeIntent | null>(null);

  const sub = useQuery({ queryKey: ["billing-subscription"], queryFn: () => billingApi.subscription(), retry: 1 });

  const ledger = useInfiniteQuery({
    queryKey: ["points-ledger"],
    queryFn: ({ pageParam }) => billingApi.pointsLedger(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    retry: 1,
  });

  const rows = ledger.data?.pages.flatMap((p) => p.items) ?? [];
  const price = (points / 10_000) * PRICE_PER_10K;

  const topup = useMutation({
    mutationFn: () => billingApi.topupPoints(points),
    onSuccess: (res: PointsTopupResult) => setIntent(res.stripe),
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns = [
    {
      title: t("sub.pts.col.delta"),
      dataIndex: "delta",
      width: 110,
      render: (v: number) => <Tag color={v >= 0 ? "success" : "default"}>{v >= 0 ? `+${v}` : v}</Tag>,
    },
    { title: t("sub.pts.col.reason"), dataIndex: "reason" },
    { title: t("sub.pts.col.balance"), dataIndex: "balance_after", width: 120, align: "right" as const },
    {
      title: t("sub.pts.col.time"),
      dataIndex: "created_at",
      width: 160,
      render: (v: string) => <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>{fullTime(v)}</span>,
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("sub.nav.points")}</h1>
      </div>
      <div className="sc-page-body" style={{ maxWidth: 820 }}>
        <Card size="small" style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 24, flexWrap: "wrap" }}>
            <div>
              <div className="sc-stat-tile-label">{t("sub.pts.balance")}</div>
              <div style={{ fontSize: 30, fontWeight: 800, color: "var(--sc-primary)", fontVariantNumeric: "tabular-nums" }}>
                {sub.isLoading ? "…" : (sub.data?.ai_points_balance ?? 0).toLocaleString()}
              </div>
            </div>
            <div style={{ flex: 1, minWidth: 240 }}>
              <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>
                {t("sub.pts.amount")} · {t("sub.pts.priceHint")}
              </div>
              <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                <InputNumber
                  min={STEP}
                  step={STEP}
                  value={points}
                  onChange={(v) => setPoints(v ?? STEP)}
                  style={{ width: 160 }}
                  formatter={(v) => `${Number(v).toLocaleString()}`}
                />
                <span style={{ fontSize: 15, fontWeight: 600 }}>${price.toFixed(2)}</span>
                <Button type="primary" icon={<ThunderboltOutlined />} loading={topup.isPending} onClick={() => topup.mutate()}>
                  {t("sub.pts.buy")}
                </Button>
              </div>
            </div>
          </div>
        </Card>

        <Card size="small" title={t("sub.pts.ledger")}>
          {ledger.isLoading ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : rows.length === 0 ? (
            <Empty description={t("sub.pts.empty")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <>
              <Table<PointsLedgerEntry> rowKey={(_, i) => String(i)} size="small" pagination={false} dataSource={rows} columns={columns} />
              {ledger.hasNextPage && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <Button size="small" loading={ledger.isFetchingNextPage} onClick={() => ledger.fetchNextPage()}>
                    {t("sub.pts.loadMore")}
                  </Button>
                </div>
              )}
            </>
          )}
        </Card>
      </div>

      <StripePayment
        open={!!intent}
        intent={intent}
        amountLabel={`$${price.toFixed(2)}`}
        onClose={() => setIntent(null)}
        onSuccess={() => {
          setIntent(null);
          void qc.invalidateQueries({ queryKey: ["billing-subscription"] });
          void qc.invalidateQueries({ queryKey: ["points-ledger"] });
        }}
      />
    </div>
  );
}
