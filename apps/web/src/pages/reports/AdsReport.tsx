/** 報告 › 廣告分析 — Facebook 廣告 + 訊息廣告 (CTWA) sub-nav. Attribution is
 *  captured at conversation creation (conversation_attribution). Contract:
 *  GET /reports/ads/facebook | /reports/ads/messenger. */
import { Empty, Table, Tabs } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { reportsApi } from "@/api/endpoints";
import type { ReportFilters } from "@/api/types";
import { t } from "@/i18n";
import { ProNotice } from "@/pages/marketing/ProNotice";
import { defaultFilters, ReportFilterBar } from "./ReportFilterBar";
import { ReportBody } from "./parts";

type AdsTab = "facebook" | "messenger";

export function AdsReport() {
  const [tab, setTab] = useState<AdsTab>("facebook");
  const [filters, setFilters] = useState<ReportFilters>(defaultFilters());

  const query = useQuery({
    queryKey: ["report-ads", tab, filters],
    queryFn: () => (tab === "facebook" ? reportsApi.adsFacebook(filters) : reportsApi.adsMessenger(filters)),
    retry: 1,
  });

  const rows = query.data?.rows ?? [];
  const cols =
    rows.length > 0
      ? Object.keys(rows[0]).map((k) => ({ title: k, dataIndex: k, render: (v: unknown) => String(v ?? "—") }))
      : [];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("rpt.nav.ads")}</h1>
      </div>
      <ProNotice message={t("rpt.proOnly")} />
      <ReportFilterBar value={filters} onChange={setFilters} reportKey={`ads/${tab}`} showInterval={false} showChannel={false} showMember={false} />
      <ReportBody isLoading={query.isLoading} isError={query.isError}>
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as AdsTab)}
          items={[
            { key: "facebook", label: t("rpt.ads.facebook") },
            { key: "messenger", label: t("rpt.ads.messenger") },
          ]}
        />
        {rows.length === 0 ? (
          <Empty description={t("rpt.ads.empty")} />
        ) : (
          <Table
            rowKey={(_, i) => String(i)}
            size="small"
            pagination={{ pageSize: 15, hideOnSinglePage: true }}
            dataSource={rows}
            columns={cols}
            scroll={{ x: true }}
          />
        )}
      </ReportBody>
    </div>
  );
}
