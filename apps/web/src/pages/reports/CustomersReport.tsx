/** 報告 › 客戶分析 — KPI(新增/去重/重複) + 客戶趨勢折線圖 + 客戶明細 pivot
 *  table (統計維度 接待成員|社群管道|社群帳號|天|週|月|時). Contract:
 *  GET /reports/customers. */
import { Segmented, Table } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { reportsApi } from "@/api/endpoints";
import type { CustomerDimension, ReportFilters } from "@/api/types";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ChartCard, Kpi, ReportBody } from "./parts";

const DIMENSIONS: CustomerDimension[] = ["member", "channel", "account", "day", "week", "month", "hour"];

export function CustomersReport() {
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters());
  const [dimension, setDimension] = useState<CustomerDimension>("day");

  const query = useQuery({
    queryKey: ["report-customers", filters, dimension],
    queryFn: () => reportsApi.customers({ ...filters, dimension }),
    retry: 1,
  });

  const data = query.data;
  const detailRows = data?.detail.rows ?? [];
  const detailCols =
    detailRows.length > 0
      ? Object.keys(detailRows[0]).map((k) => ({
          title: k,
          dataIndex: k,
          render: (v: unknown) => String(v ?? "—"),
        }))
      : [];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.customers")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar
        value={filters}
        onChange={setFilters}
        reportKey="customers"
        exportExtra={{ dimension }}
      />
      <ReportBody isLoading={query.isLoading} isError={query.isError}>
        <div className="sc-kpi-row">
          <Kpi label={t("rpt.cust.new")} value={data?.kpis.new ?? 0} />
          <Kpi label={t("rpt.cust.newDeduped")} value={data?.kpis.new_deduped ?? 0} />
          <Kpi label={t("rpt.cust.repeat")} value={data?.kpis.repeat ?? 0} />
        </div>

        <ChartCard title={t("rpt.cust.trend")}>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={data?.trend ?? []} margin={{ top: 8, right: 16, bottom: 4, left: -12 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--sc-border)" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <RTooltip />
              <Legend />
              <Line type="monotone" dataKey="new" name={t("rpt.cust.new")} stroke="var(--sc-primary)" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="new_deduped" name={t("rpt.cust.newDeduped")} stroke="#16A34A" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="repeat" name={t("rpt.cust.repeat")} stroke="#D97706" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title={t("rpt.cust.detail")}>
          <div style={{ marginBottom: 12 }}>
            <span style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", marginRight: 8 }}>
              {t("rpt.cust.dimension")}
            </span>
            <Segmented
              value={dimension}
              onChange={(v) => setDimension(v as CustomerDimension)}
              options={DIMENSIONS.map((d) => ({ value: d, label: t(`rpt.cust.dim.${d}` as Parameters<typeof t>[0]) }))}
            />
          </div>
          <Table
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            dataSource={detailRows}
            columns={detailCols}
            locale={{ emptyText: t("rpt.noData") }}
            scroll={{ x: true }}
          />
        </ChartCard>
      </ReportBody>
    </div>
  );
}
