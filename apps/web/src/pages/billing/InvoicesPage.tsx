/** 訂閱 › 發票 — invoice list with download links. Contract: /billing/invoices. */
import { DownloadOutlined, FileTextOutlined } from "@ant-design/icons";
import { Button, Empty, Skeleton, Table, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import { billingApi } from "@/api/endpoints";
import type { Invoice } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";

export function InvoicesPage() {
  const query = useQuery({ queryKey: ["billing-invoices"], queryFn: () => billingApi.invoices(), retry: 1 });
  const rows = query.data ?? [];

  const columns = [
    {
      title: t("sub.inv.col.number"),
      dataIndex: "number",
      render: (v: string | null, r: Invoice) => v || r.id.slice(0, 10),
    },
    {
      title: t("sub.inv.col.amount"),
      dataIndex: "amount",
      width: 130,
      align: "right" as const,
      render: (v: number, r: Invoice) => `${r.currency === "USD" ? "$" : ""}${v.toFixed(2)}`,
    },
    {
      title: t("sub.inv.col.status"),
      dataIndex: "status",
      width: 110,
      render: (v: string) => <Tag color={v === "paid" ? "success" : "default"}>{v}</Tag>,
    },
    {
      title: t("sub.inv.col.date"),
      dataIndex: "created_at",
      width: 170,
      render: (v: string) => fullTime(v),
    },
    {
      title: t("common.actions"),
      width: 90,
      render: (_: unknown, r: Invoice) =>
        r.url ? (
          <a href={r.url} target="_blank" rel="noreferrer">
            <Button type="link" size="small" icon={<DownloadOutlined />}>
              {t("common.download")}
            </Button>
          </a>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("sub.nav.invoices")}</h1>
      </div>
      <div className="sc-page-body" style={{ maxWidth: 820 }}>
        {query.isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : query.isError ? (
          <EmptyState icon={<FileTextOutlined />} title={t("rpt.loadFailed")} />
        ) : rows.length === 0 ? (
          <Empty description={t("sub.inv.empty")} />
        ) : (
          <Table<Invoice> rowKey="id" size="small" pagination={false} dataSource={rows} columns={columns} />
        )}
      </div>
    </div>
  );
}
