/** 報告 › 綜合報表 — per-agent scorecard: 訊息/會話/首響/滿意度/解決/在線.
 *  Contract: GET /reports/summary. */
import { Table } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { reportsApi } from "@/api/endpoints";
import type { ReportFilters, SummaryAgentRow } from "@/api/types";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ReportBody } from "./parts";
import { fmtDuration } from "./OnlineTimeReport";

function fmtMs(ms: number): string {
  if (!ms) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function SummaryReport() {
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters());
  const query = useQuery({
    queryKey: ["report-summary", filters],
    queryFn: () => reportsApi.summary(filters),
    retry: 1,
  });
  const rows = query.data?.agents ?? [];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.summary")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar value={filters} onChange={setFilters} reportKey="summary" showInterval={false} showChannel={false} />
      <ReportBody isLoading={query.isLoading} isError={query.isError} isEmpty={rows.length === 0}>
        <Table<SummaryAgentRow>
          rowKey="member_id"
          size="small"
          pagination={{ pageSize: 20, hideOnSinglePage: true }}
          dataSource={rows}
          scroll={{ x: 760 }}
          columns={[
            { title: t("rpt.sum.member"), dataIndex: "display_name", render: (v, r) => v || r.member_id.slice(0, 8) },
            { title: t("rpt.sum.msgs"), dataIndex: "msgs", width: 90, align: "right" },
            { title: t("rpt.sum.convs"), dataIndex: "convs", width: 90, align: "right" },
            { title: t("rpt.sum.frt"), dataIndex: "frt_avg_ms", width: 110, align: "right", render: (v: number) => fmtMs(v) },
            {
              title: t("rpt.sum.csat"),
              dataIndex: "csat_avg",
              width: 90,
              align: "right",
              render: (v: number) => (v ? `${(v * 100).toFixed(0)}%` : "—"),
            },
            { title: t("rpt.sum.resolution"), dataIndex: "resolution_avg_ms", width: 110, align: "right", render: (v: number) => fmtMs(v) },
            { title: t("rpt.sum.online"), dataIndex: "online_seconds", width: 140, align: "right", render: (v: number) => fmtDuration(v) },
          ]}
        />
      </ReportBody>
    </div>
  );
}
