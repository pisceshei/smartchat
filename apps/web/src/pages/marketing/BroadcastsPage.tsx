/** 行銷 › 群發訊息 — 群發計劃 / 循環計劃 tabs, batch ops, recycle bin, create
 *  wizard, run-detail drawer (recipients by state). Contract: /broadcasts. */
import {
  DeleteOutlined,
  NotificationOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RestOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Drawer,
  Empty,
  Input,
  Modal,
  Progress,
  Skeleton,
  Table,
  Tabs,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { broadcastsApi } from "@/api/endpoints";
import type {
  BroadcastListItem,
  BroadcastStatus,
  BroadcastType,
  RecipientState,
} from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime, listTime } from "@/utils/time";
import { BroadcastWizard } from "./BroadcastWizard";
import { ProNotice } from "./ProNotice";
import { RunDetailDrawer } from "./RunDetailDrawer";
import "./marketing.css";

const STATUS_COLOR: Record<BroadcastStatus, string> = {
  draft: "default",
  scheduled: "blue",
  running: "processing",
  paused: "warning",
  completed: "success",
  cancelled: "default",
  failed: "error",
};

function statusTag(s: BroadcastStatus) {
  return <Tag color={STATUS_COLOR[s]}>{t(`bc.status.${s}` as Parameters<typeof t>[0])}</Tag>;
}

export function BroadcastsPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [tab, setTab] = useState<BroadcastType>("one_time");
  const [q, setQ] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [recycleOpen, setRecycleOpen] = useState(false);
  const [detail, setDetail] = useState<BroadcastListItem | null>(null);
  const [selected, setSelected] = useState<React.Key[]>([]);

  const list = useQuery({
    queryKey: ["broadcasts", tab, q],
    queryFn: () => broadcastsApi.list({ type: tab, q: q || undefined }),
    retry: 1,
  });

  const invalidate = () => void qc.invalidateQueries({ queryKey: ["broadcasts"] });

  const act = useMutation({
    mutationFn: async (v: { id: string; op: "pause" | "resume" | "cancel" | "remove" }) => {
      if (v.op === "pause") await broadcastsApi.pause(v.id);
      else if (v.op === "resume") await broadcastsApi.resume(v.id);
      else if (v.op === "cancel") await broadcastsApi.cancel(v.id);
      else await broadcastsApi.remove(v.id);
    },
    onSuccess: (_r, v) => {
      message.success(
        v.op === "pause"
          ? t("bc.paused")
          : v.op === "resume"
            ? t("bc.resumed")
            : v.op === "cancel"
              ? t("bc.cancelled")
              : t("common.deleteSuccess"),
      );
      invalidate();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const batchDelete = useMutation({
    mutationFn: async (ids: React.Key[]) => {
      await Promise.all(ids.map((id) => broadcastsApi.remove(String(id))));
    },
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      setSelected([]);
      invalidate();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const rows = list.data ?? [];

  const columns: ColumnsType<BroadcastListItem> = useMemo(
    () => [
      {
        title: t("bc.col.name"),
        dataIndex: "name",
        render: (_, r) => (
          <a style={{ fontWeight: 600 }} onClick={() => setDetail(r)}>
            {r.name}
          </a>
        ),
      },
      {
        title: t("bc.col.status"),
        dataIndex: "status",
        width: 100,
        render: (s: BroadcastStatus) => statusTag(s),
      },
      {
        title: t("bc.col.channel"),
        dataIndex: "channel_type",
        width: 150,
        render: (v: string) => (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <ChannelIcon type={v} size={16} />
            {CHANNEL_NAME[v] ?? v}
          </span>
        ),
      },
      {
        title: t("bc.col.sendRule"),
        dataIndex: "send_rule_summary",
        width: 180,
        render: (v: string) => (
          <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>{v || "—"}</span>
        ),
      },
      {
        title: t("bc.col.planned"),
        dataIndex: "planned_count",
        width: 120,
        align: "right",
        render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{v}</span>,
      },
      {
        title: t("bc.col.sent"),
        dataIndex: "sent_count",
        width: 120,
        align: "right",
        render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{v}</span>,
      },
      {
        title: t("bc.col.successRate"),
        dataIndex: "success_rate",
        width: 130,
        render: (v: number) => (
          <Progress
            percent={Math.round((v ?? 0) * 100)}
            size="small"
            strokeColor="var(--sc-success)"
          />
        ),
      },
      {
        title: t("bc.col.created"),
        dataIndex: "created_at",
        width: 120,
        render: (v: string) => (
          <span title={fullTime(v)} style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>
            {listTime(v)}
          </span>
        ),
      },
      {
        title: t("common.actions"),
        width: 200,
        fixed: "right",
        render: (_, r) => (
          <div style={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
            {r.status === "running" && (
              <Button
                type="text"
                size="small"
                icon={<PauseCircleOutlined />}
                onClick={() => act.mutate({ id: r.id, op: "pause" })}
              >
                {t("bc.action.pause")}
              </Button>
            )}
            {r.status === "paused" && (
              <Button
                type="text"
                size="small"
                icon={<PlayCircleOutlined />}
                onClick={() => act.mutate({ id: r.id, op: "resume" })}
              >
                {t("bc.action.resume")}
              </Button>
            )}
            {(r.status === "scheduled" || r.status === "running" || r.status === "paused") && (
              <Button
                type="text"
                size="small"
                icon={<StopOutlined />}
                onClick={() =>
                  modal.confirm({
                    title: t("bc.cancelConfirm"),
                    content: r.name,
                    okText: t("bc.action.cancel"),
                    okButtonProps: { danger: true },
                    cancelText: t("common.cancel"),
                    onOk: () => act.mutate({ id: r.id, op: "cancel" }),
                  })
                }
              >
                {t("bc.action.cancel")}
              </Button>
            )}
            <Button
              type="text"
              size="small"
              danger
              icon={<DeleteOutlined />}
              onClick={() =>
                modal.confirm({
                  title: t("common.confirmDeleteTitle"),
                  content: r.name,
                  okText: t("common.delete"),
                  okButtonProps: { danger: true },
                  cancelText: t("common.cancel"),
                  onOk: () => act.mutate({ id: r.id, op: "remove" }),
                })
              }
            />
          </div>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [act.isPending],
  );

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("mkt.nav.broadcasts")}</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<RestOutlined />} onClick={() => setRecycleOpen(true)}>
            {t("bc.recycleBin")}
          </Button>
          <Button icon={<ReloadOutlined />} onClick={() => list.refetch()} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setWizardOpen(true)}>
            {t("bc.create")}
          </Button>
        </div>
      </div>

      <ProNotice />

      <div className="sc-page-body">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
          <Tabs
            activeKey={tab}
            onChange={(k) => setTab(k as BroadcastType)}
            items={[
              { key: "one_time", label: t("bc.tab.oneTime") },
              { key: "recurring", label: t("bc.tab.recurring") },
            ]}
            style={{ flex: 1 }}
          />
          <Input.Search
            allowClear
            placeholder={t("bc.searchPlaceholder")}
            style={{ width: 220 }}
            onSearch={setQ}
          />
        </div>

        <div className="sc-mkt-hint" style={{ marginBottom: 8 }}>{t("bc.dataHint")}</div>

        {selected.length > 0 && (
          <div style={{ marginBottom: 8 }}>
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              loading={batchDelete.isPending}
              onClick={() =>
                modal.confirm({
                  title: t("common.confirmDeleteTitle"),
                  okText: t("common.delete"),
                  okButtonProps: { danger: true },
                  cancelText: t("common.cancel"),
                  onOk: () => batchDelete.mutate(selected),
                })
              }
            >
              {t("bc.batchOps")} ({selected.length})
            </Button>
          </div>
        )}

        {list.isLoading ? (
          <Skeleton active paragraph={{ rows: 6 }} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<NotificationOutlined />}
            title={t("bc.empty")}
            hint={t("bc.emptyHint")}
            action={
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setWizardOpen(true)}>
                {t("bc.create")}
              </Button>
            }
          />
        ) : (
          <Table<BroadcastListItem>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={rows}
            pagination={false}
            scroll={{ x: 1180 }}
            rowSelection={{ selectedRowKeys: selected, onChange: setSelected }}
          />
        )}
      </div>

      <BroadcastWizard open={wizardOpen} onClose={() => setWizardOpen(false)} />
      <RecycleBin open={recycleOpen} onClose={() => setRecycleOpen(false)} />
      <Drawer
        title={detail?.name}
        open={!!detail}
        onClose={() => setDetail(null)}
        width={560}
        destroyOnHidden
      >
        {detail && <RunDetailDrawer broadcast={detail} />}
      </Drawer>
    </div>
  );
}

/* ---------------------------------------------------------------- recycle bin */
function RecycleBin({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const bin = useQuery({
    queryKey: ["broadcasts-recycle"],
    queryFn: () => broadcastsApi.recycleBin(),
    enabled: open,
    retry: 1,
  });
  const restore = useMutation({
    mutationFn: (id: string) => broadcastsApi.restore(id),
    onSuccess: () => {
      message.success(t("bc.restored"));
      void qc.invalidateQueries({ queryKey: ["broadcasts-recycle"] });
      void qc.invalidateQueries({ queryKey: ["broadcasts"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const rows = bin.data ?? [];
  return (
    <Modal title={t("bc.recycleBin")} open={open} onCancel={onClose} footer={null} width={640}>
      {bin.isLoading ? (
        <Skeleton active paragraph={{ rows: 4 }} />
      ) : rows.length === 0 ? (
        <Empty description={t("bc.recycleEmpty")} />
      ) : (
        <Table<BroadcastListItem>
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={rows}
          columns={[
            { title: t("bc.col.name"), dataIndex: "name" },
            {
              title: t("bc.col.channel"),
              dataIndex: "channel_type",
              width: 130,
              render: (v: string) => CHANNEL_NAME[v] ?? v,
            },
            {
              title: t("common.actions"),
              width: 90,
              render: (_, r) => (
                <Button
                  type="link"
                  size="small"
                  loading={restore.isPending && restore.variables === r.id}
                  onClick={() => restore.mutate(r.id)}
                >
                  {t("bc.action.restore")}
                </Button>
              ),
            },
          ]}
        />
      )}
    </Modal>
  );
}

export const RECIPIENT_STATES: RecipientState[] = [
  "planned",
  "queued",
  "sent",
  "delivered",
  "read",
  "failed",
  "skipped",
];
