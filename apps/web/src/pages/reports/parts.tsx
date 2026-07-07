/** Shared report primitives: KPI card, chart card, and a load/empty/error
 *  wrapper so every report degrades gracefully when its endpoint is not yet
 *  live (loading skeleton → error/empty state, never a crash). */
import { BarChartOutlined } from "@ant-design/icons";
import { Empty, Skeleton } from "antd";
import type { ReactNode } from "react";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import "./reports.css";

export function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: string }) {
  return (
    <div className="sc-kpi-card">
      <div className="sc-kpi-label">{label}</div>
      <div className="sc-kpi-value">{value}</div>
      {sub && <div style={{ fontSize: 12, color: "var(--sc-text-secondary)", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

export function ChartCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="sc-chart-card">
      <div className="sc-chart-title">{title}</div>
      {children}
    </div>
  );
}

export function ReportBody({
  isLoading,
  isError,
  isEmpty,
  children,
}: {
  isLoading: boolean;
  isError: boolean;
  isEmpty?: boolean;
  children: ReactNode;
}) {
  if (isLoading) {
    return (
      <div className="sc-page-body">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  }
  if (isError) {
    return (
      <div className="sc-page-body">
        <EmptyState icon={<BarChartOutlined />} title={t("rpt.loadFailed")} />
      </div>
    );
  }
  if (isEmpty) {
    return (
      <div className="sc-page-body">
        <Empty description={t("rpt.noData")} />
      </div>
    );
  }
  return <div className="sc-page-body">{children}</div>;
}
