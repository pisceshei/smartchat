/** 報告 › 服務概覽 — KPI(今日新會話/處理中/線上成員) + 會話量趨勢 (hourly).
 *  Contract: GET /reports/service-overview. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { reportsApi } from "@/api/endpoints";
import type { ReportFilters } from "@/api/types";
import { t } from "@/i18n";
import { dayjs } from "@/utils/time";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ChartCard, Kpi, ReportBody } from "./parts";

export function ServiceOverviewReport() {
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters("hour"));

  const query = useQuery({
    queryKey: ["report-service", filters],
    queryFn: () => reportsApi.serviceOverview(filters),
    retry: 1,
  });

  const trend = (query.data?.trend ?? []).map((p) => ({
    label: dayjs(p.ts).format("MM-DD HH:mm"),
    conversations: p.conversations,
  }));

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.service")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar value={filters} onChange={setFilters} reportKey="service-overview" showMember={false} />
      <ReportBody isLoading={query.isLoading} isError={query.isError}>
        <div className="sc-kpi-row">
          <Kpi label={t("rpt.svc.newToday")} value={query.data?.kpis.new_conversations_today ?? 0} />
          <Kpi label={t("rpt.svc.inProgress")} value={query.data?.kpis.in_progress ?? 0} />
          <Kpi label={t("rpt.svc.online")} value={query.data?.kpis.online_members ?? 0} />
        </div>
        <ChartCard title={t("rpt.svc.trend")}>
          <ResponsiveContainer width="100%" height={300}>
            <AreaChart data={trend} margin={{ top: 8, right: 16, bottom: 4, left: -12 }}>
              <defs>
                <linearGradient id="scConvFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--sc-primary)" stopOpacity={0.28} />
                  <stop offset="100%" stopColor="var(--sc-primary)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--sc-border)" />
              <XAxis dataKey="label" tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" minTickGap={24} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <RTooltip />
              <Area
                type="monotone"
                dataKey="conversations"
                name={t("rpt.svc.trend")}
                stroke="var(--sc-primary)"
                strokeWidth={2}
                fill="url(#scConvFill)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>
      </ReportBody>
    </div>
  );
}
