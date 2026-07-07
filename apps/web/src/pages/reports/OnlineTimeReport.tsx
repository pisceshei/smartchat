/** 報告 › 在線時長 — per-member online duration (from agent_presence_sessions,
 *  not events). Contract: GET /reports/online-time. */
import { Table } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { reportsApi } from "@/api/endpoints";
import type { OnlineTimeRow, ReportFilters } from "@/api/types";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ChartCard, ReportBody } from "./parts";

export function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}

export function OnlineTimeReport() {
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters());
  const query = useQuery({
    queryKey: ["report-online", filters],
    queryFn: () => reportsApi.onlineTime(filters),
    retry: 1,
  });

  const rows = query.data?.rows ?? [];
  const chartData = [...rows]
    .sort((a, b) => b.online_seconds - a.online_seconds)
    .slice(0, 10)
    .map((r) => ({ name: r.display_name, hours: Math.round((r.online_seconds / 3600) * 10) / 10 }));

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.onlineTime")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar value={filters} onChange={setFilters} reportKey="online-time" showInterval={false} showChannel={false} />
      <ReportBody isLoading={query.isLoading} isError={query.isError} isEmpty={rows.length === 0}>
        <ChartCard title={t("rpt.nav.onlineTime")}>
          <ResponsiveContainer width="100%" height={Math.max(180, chartData.length * 34)}>
            <BarChart data={chartData} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 12 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--sc-border)" horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <YAxis type="category" dataKey="name" width={90} tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <RTooltip />
              <Bar dataKey="hours" name={t("rpt.online.duration")} fill="var(--sc-primary)" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
        <Table<OnlineTimeRow>
          rowKey="member_id"
          size="small"
          pagination={{ pageSize: 15, hideOnSinglePage: true }}
          dataSource={rows}
          columns={[
            { title: t("rpt.online.member"), dataIndex: "display_name" },
            {
              title: t("rpt.online.duration"),
              dataIndex: "online_seconds",
              width: 200,
              align: "right",
              render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{fmtDuration(v)}</span>,
            },
          ]}
        />
      </ReportBody>
    </div>
  );
}
