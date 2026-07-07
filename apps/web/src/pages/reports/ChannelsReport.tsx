/** 報告 › 渠道分析 — per-channel conversations + inbound/outbound messages,
 *  with a grouped bar chart. Contract: GET /reports/channels. */
import { Table } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { reportsApi } from "@/api/endpoints";
import type { ChannelsReportRow, ReportFilters } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ChartCard, ReportBody } from "./parts";

export function ChannelsReport() {
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters());
  const query = useQuery({
    queryKey: ["report-channels", filters],
    queryFn: () => reportsApi.channels(filters),
    retry: 1,
  });
  const rows = query.data?.rows ?? [];
  const chartData = rows.map((r) => ({
    name: CHANNEL_NAME[r.channel_type] ?? r.channel_type,
    conversations: r.conversations,
    messages_in: r.messages_in,
    messages_out: r.messages_out,
  }));

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.channels")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar value={filters} onChange={setFilters} reportKey="channels" showInterval={false} showMember={false} />
      <ReportBody isLoading={query.isLoading} isError={query.isError} isEmpty={rows.length === 0}>
        <ChartCard title={t("rpt.nav.channels")}>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={chartData} margin={{ top: 8, right: 16, bottom: 4, left: -12 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--sc-border)" />
              <XAxis dataKey="name" tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <RTooltip />
              <Legend />
              <Bar dataKey="conversations" name={t("rpt.chan.conversations")} fill="var(--sc-primary)" radius={[4, 4, 0, 0]} />
              <Bar dataKey="messages_in" name={t("rpt.chan.messagesIn")} fill="#16A34A" radius={[4, 4, 0, 0]} />
              <Bar dataKey="messages_out" name={t("rpt.chan.messagesOut")} fill="#D97706" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
        <Table<ChannelsReportRow>
          rowKey="channel_type"
          size="small"
          pagination={false}
          dataSource={rows}
          columns={[
            {
              title: t("rpt.chan.channel"),
              dataIndex: "channel_type",
              render: (v: string) => (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <ChannelIcon type={v} size={16} />
                  {CHANNEL_NAME[v] ?? v}
                </span>
              ),
            },
            { title: t("rpt.chan.conversations"), dataIndex: "conversations", width: 120, align: "right" },
            { title: t("rpt.chan.messagesIn"), dataIndex: "messages_in", width: 120, align: "right" },
            { title: t("rpt.chan.messagesOut"), dataIndex: "messages_out", width: 120, align: "right" },
          ]}
        />
      </ReportBody>
    </div>
  );
}
