/** 報告 › AI 分析 — nightly LLM digest (day-over-day). Pro+ gated; costs 20
 *  AI points/day server-side. Contract: GET /reports/ai-summary. */
import { RobotOutlined } from "@ant-design/icons";
import { Empty, Skeleton } from "antd";
import { useQuery } from "@tanstack/react-query";
import { reportsApi } from "@/api/endpoints";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { EmptyState } from "@/components/EmptyState";
import "./reports.css";

export function AiSummaryReport() {
  const query = useQuery({
    queryKey: ["report-ai-summary"],
    queryFn: () => reportsApi.aiSummary(),
    retry: 1,
  });

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.ai")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <div className="sc-page-body">
        {query.isLoading ? (
          <Skeleton active paragraph={{ rows: 8 }} />
        ) : query.isError ? (
          <EmptyState icon={<RobotOutlined />} title={t("rpt.loadFailed")} />
        ) : !query.data?.text ? (
          <Empty description={t("rpt.ai.empty")} />
        ) : (
          <div className="sc-chart-card" style={{ maxWidth: 820 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
              <RobotOutlined style={{ color: "var(--sc-primary)" }} />
              <span style={{ fontWeight: 600 }}>{query.data.day}</span>
            </div>
            <div style={{ whiteSpace: "pre-wrap", lineHeight: 1.75, color: "var(--sc-text)", fontSize: 14 }}>
              {query.data.text}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
