/** Broadcast run-detail body (rendered inside a Drawer): run batches + the
 *  selected run's recipients filtered by delivery state, with a success-rate
 *  summary. Contract: /broadcasts/{id}/runs + /runs/{run_id}/recipients. */
import { Empty, Segmented, Skeleton, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { broadcastsApi } from "@/api/endpoints";
import type {
  BroadcastListItem,
  BroadcastRecipient,
  BroadcastRun,
  RecipientState,
} from "@/api/types";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";

const STATE_COLOR: Record<RecipientState, string> = {
  planned: "default",
  queued: "blue",
  sent: "cyan",
  delivered: "success",
  read: "green",
  failed: "error",
  skipped: "warning",
};

function summaryCell(label: string, value: number, color?: string) {
  return (
    <div style={{ border: "1px solid var(--sc-border)", borderRadius: 8, padding: "8px 10px", flex: 1, minWidth: 68 }}>
      <div style={{ fontSize: 11.5, color: "var(--sc-text-tertiary)" }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: color ?? "var(--sc-text-heading)", fontVariantNumeric: "tabular-nums" }}>
        {value}
      </div>
    </div>
  );
}

export function RunDetailDrawer({ broadcast }: { broadcast: BroadcastListItem }) {
  const [runId, setRunId] = useState<string | null>(null);
  const [state, setState] = useState<RecipientState | "all">("all");

  const runs = useQuery({
    queryKey: ["broadcast-runs", broadcast.id],
    queryFn: () => broadcastsApi.runs(broadcast.id),
    retry: 1,
  });

  useEffect(() => {
    if (!runId && runs.data && runs.data.length > 0) setRunId(runs.data[0].id);
  }, [runs.data, runId]);

  const recipients = useQuery({
    queryKey: ["broadcast-recipients", broadcast.id, runId, state],
    queryFn: () =>
      broadcastsApi.recipients(broadcast.id, runId as string, {
        state: state === "all" ? undefined : state,
      }),
    enabled: !!runId,
    retry: 1,
  });

  const run: BroadcastRun | undefined = runs.data?.find((r) => r.id === runId);

  const columns: ColumnsType<BroadcastRecipient> = [
    {
      title: t("bc.run.col.recipient"),
      dataIndex: "display_name",
      render: (v: string | null, r) => v || r.contact_id.slice(0, 8),
    },
    {
      title: t("bc.run.col.state"),
      dataIndex: "state",
      width: 90,
      render: (s: RecipientState) => (
        <Tag color={STATE_COLOR[s]}>{t(`bc.run.state.${s}` as Parameters<typeof t>[0])}</Tag>
      ),
    },
    {
      title: t("bc.run.col.detail"),
      width: 140,
      render: (_, r) => (
        <span style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>
          {r.error || r.skip_reason || "—"}
        </span>
      ),
    },
  ];

  if (runs.isLoading) return <Skeleton active paragraph={{ rows: 5 }} />;
  if (!runs.data || runs.data.length === 0) return <Empty description={t("bc.run.noRuns")} />;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div>
        <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.run.runs")}</div>
        <Segmented
          value={runId ?? undefined}
          onChange={(v) => {
            setRunId(String(v));
            setState("all");
          }}
          options={runs.data.map((r, i) => ({
            value: r.id,
            label: r.scheduled_at ? fullTime(r.scheduled_at).slice(5, 16) : `#${i + 1}`,
          }))}
        />
      </div>

      {run && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {summaryCell(t("bc.run.summary.planned"), run.planned)}
          {summaryCell(t("bc.run.summary.sent"), run.sent)}
          {summaryCell(t("bc.run.summary.delivered"), run.delivered, "var(--sc-success)")}
          {summaryCell(t("bc.run.summary.read"), run.read, "var(--sc-success)")}
          {summaryCell(t("bc.run.summary.failed"), run.failed, "var(--sc-error)")}
          {summaryCell(t("bc.run.summary.skipped"), run.skipped, "var(--sc-warning)")}
        </div>
      )}

      <div>
        <div className="sc-mkt-hint" style={{ marginBottom: 6 }}>{t("bc.run.recipients")}</div>
        <Segmented
          size="small"
          value={state}
          onChange={(v) => setState(v as RecipientState | "all")}
          style={{ marginBottom: 8 }}
          options={[
            { value: "all", label: t("bc.run.state.all") },
            { value: "sent", label: t("bc.run.state.sent") },
            { value: "delivered", label: t("bc.run.state.delivered") },
            { value: "read", label: t("bc.run.state.read") },
            { value: "failed", label: t("bc.run.state.failed") },
            { value: "skipped", label: t("bc.run.state.skipped") },
          ]}
        />
        {recipients.isLoading ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : (recipients.data?.items.length ?? 0) === 0 ? (
          <Empty description={t("rpt.noData")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Table<BroadcastRecipient>
            rowKey="contact_id"
            size="small"
            pagination={false}
            columns={columns}
            dataSource={recipients.data?.items ?? []}
          />
        )}
      </div>
    </div>
  );
}
