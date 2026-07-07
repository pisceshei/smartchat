/** 自動化 › 流程 — SaleSmartly-style flow list: left category folder tree +
 *  table (enable toggle / name / channel / 7-day 觸發次數·觸發人數·參與度·完成度 /
 *  modified time / actions) + 創建流程 template-gallery modal. */
import {
  BarChartOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  FolderOutlined,
  MoreOutlined,
  PlusOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Segmented,
  Skeleton,
  Switch,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { flowsApi } from "@/api/endpoints";
import type { FlowSummary, FlowTemplate } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime, listTime } from "@/utils/time";

const ALL = "__all__";
const UNCAT = "__uncat__";

function channelLabel(scope: string): string {
  return scope === "all" ? t("flow.channel.all") : (CHANNEL_NAME[scope] ?? scope);
}

/* ------------------------------------------------------------ template gallery */
function TemplateGallery({
  open,
  categoryId,
  onClose,
}: {
  open: boolean;
  categoryId: string | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [filter, setFilter] = useState<string>("all");

  const templates = useQuery({
    queryKey: ["flow-templates"],
    queryFn: () => flowsApi.templates(),
    enabled: open,
    retry: 1,
  });

  const createBlank = useMutation({
    mutationFn: () =>
      flowsApi.create({
        name: t("flow.newFlowName"),
        channel_type: "all",
        category_id: categoryId,
      }),
    onSuccess: (flow) => navigate(`/automation/flows/${flow.id}`),
    onError: () => message.error(t("common.operationFailed")),
  });

  const useTemplate = useMutation({
    mutationFn: (tpl: FlowTemplate) =>
      flowsApi.useTemplate(
        { id: tpl.id, channel_type: tpl.channel_type },
        { name: tpl.name, category_id: categoryId },
      ),
    onSuccess: (flow) => navigate(`/automation/flows/${flow.id}`),
    onError: () => message.error(t("common.operationFailed")),
  });

  const cats = useMemo(() => {
    const set = new Set<string>();
    for (const tp of templates.data ?? []) set.add(tp.category);
    return Array.from(set);
  }, [templates.data]);

  const shown = (templates.data ?? []).filter((tp) => filter === "all" || tp.category === filter);

  return (
    <Modal
      title={t("flow.tpl.title")}
      open={open}
      onCancel={onClose}
      footer={null}
      width={860}
      styles={{ body: { paddingTop: 8 } }}
    >
      {cats.length > 0 && (
        <Segmented
          style={{ marginBottom: 14 }}
          value={filter}
          onChange={(v) => setFilter(String(v))}
          options={[{ value: "all", label: t("flow.tpl.all") }, ...cats.map((c) => ({ value: c, label: c }))]}
        />
      )}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 12 }}>
        {/* blank flow — always first */}
        <button
          type="button"
          className="sc-clickable"
          onClick={() => createBlank.mutate()}
          disabled={createBlank.isPending}
          style={{
            textAlign: "left",
            border: "1px dashed var(--sc-border-strong)",
            borderRadius: 10,
            padding: 16,
            background: "var(--sc-bg-subtle)",
            cursor: "pointer",
            display: "flex",
            flexDirection: "column",
            gap: 6,
            minHeight: 128,
            fontFamily: "var(--sc-font)",
          }}
        >
          <PlusOutlined style={{ fontSize: 22, color: "var(--sc-primary)" }} />
          <div style={{ fontWeight: 600, fontSize: 14 }}>{t("flow.tpl.blank")}</div>
          <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>{t("flow.tpl.blankDesc")}</div>
        </button>

        {templates.isLoading &&
          [...Array(3)].map((_, i) => (
            <div key={i} style={{ border: "1px solid var(--sc-border)", borderRadius: 10, padding: 16 }}>
              <Skeleton active paragraph={{ rows: 2 }} />
            </div>
          ))}

        {shown.map((tp) => (
          <div
            key={tp.id}
            style={{
              border: "1px solid var(--sc-border)",
              borderRadius: 10,
              padding: 16,
              display: "flex",
              flexDirection: "column",
              gap: 6,
              minHeight: 128,
            }}
          >
            <div style={{ fontWeight: 600, fontSize: 14 }}>{tp.name}</div>
            <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)", flex: 1, lineHeight: 1.55 }}>
              {tp.description}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Tag color="blue" style={{ margin: 0 }}>
                {channelLabel(tp.channel_type)}
              </Tag>
              {tp.node_count != null && (
                <span style={{ fontSize: 11.5, color: "var(--sc-text-tertiary)" }}>
                  {tp.node_count} {t("flow.tpl.nodes")}
                </span>
              )}
            </div>
            <Button
              type="primary"
              ghost
              size="small"
              block
              loading={useTemplate.isPending && useTemplate.variables?.id === tp.id}
              onClick={() => useTemplate.mutate(tp)}
            >
              {t("flow.tpl.use")}
            </Button>
          </div>
        ))}
      </div>
      <div className="sc-fe-hint" style={{ marginTop: 14 }}>
        {t("flow.tpl.bindHint")}
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------- page */
export function FlowsPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const navigate = useNavigate();
  const [activeCat, setActiveCat] = useState<string>(ALL);
  const [galleryOpen, setGalleryOpen] = useState(false);
  const [statsFlow, setStatsFlow] = useState<FlowSummary | null>(null);

  const categories = useQuery({
    queryKey: ["flow-categories"],
    queryFn: () => flowsApi.categories(),
    retry: 1,
  });
  const flows = useQuery({
    queryKey: ["flows"],
    queryFn: () => flowsApi.list(),
    retry: 1,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["flows"] });
    void qc.invalidateQueries({ queryKey: ["flow-categories"] });
  };

  const toggleEnabled = useMutation({
    mutationFn: (v: { id: string; enabled: boolean }) => flowsApi.update(v.id, { enabled: v.enabled }),
    onSuccess: () => invalidate(),
    onError: () => message.error(t("common.operationFailed")),
  });
  const duplicate = useMutation({
    mutationFn: (id: string) => flowsApi.duplicate(id),
    onSuccess: () => {
      message.success(t("flow.duplicated"));
      invalidate();
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => flowsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      invalidate();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const addCategory = useMutation({
    mutationFn: (name: string) => flowsApi.createCategory({ name }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["flow-categories"] }),
    onError: () => message.error(t("common.operationFailed")),
  });

  const filtered = useMemo(() => {
    const list = flows.data ?? [];
    if (activeCat === ALL) return list;
    if (activeCat === UNCAT) return list.filter((f) => !f.category_id);
    return list.filter((f) => f.category_id === activeCat);
  }, [flows.data, activeCat]);

  const countFor = (catId: string): number => {
    const list = flows.data ?? [];
    if (catId === ALL) return list.length;
    if (catId === UNCAT) return list.filter((f) => !f.category_id).length;
    return list.filter((f) => f.category_id === catId).length;
  };

  const promptNewCategory = () => {
    let val = "";
    modal.confirm({
      title: t("flow.newCategory"),
      icon: <FolderOutlined />,
      content: (
        <Input placeholder={t("flow.categoryName")} onChange={(e) => (val = e.target.value)} autoFocus />
      ),
      okText: t("common.create"),
      cancelText: t("common.cancel"),
      onOk: () => {
        if (val.trim()) addCategory.mutate(val.trim());
      },
    });
  };

  const columns: ColumnsType<FlowSummary> = [
    {
      title: t("flow.enabled"),
      dataIndex: "enabled",
      width: 72,
      render: (_, r) => (
        <Switch
          size="small"
          checked={r.enabled}
          loading={toggleEnabled.isPending && toggleEnabled.variables?.id === r.id}
          onChange={(v) => toggleEnabled.mutate({ id: r.id, enabled: v })}
        />
      ),
    },
    {
      title: t("flow.col.name"),
      dataIndex: "name",
      render: (_, r) => (
        <div>
          <a onClick={() => navigate(`/automation/flows/${r.id}`)} style={{ fontWeight: 600 }}>
            {r.name}
          </a>
          <div className="sc-mono" style={{ fontSize: 11, color: "var(--sc-text-tertiary)" }}>
            {r.id.slice(0, 8)}
          </div>
        </div>
      ),
    },
    {
      title: t("flow.col.channel"),
      dataIndex: "channel_type",
      width: 140,
      render: (v: string) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {v !== "all" && <ChannelIcon type={v as never} size={16} />}
          {channelLabel(v)}
        </span>
      ),
    },
    {
      title: t("flow.col.triggers"),
      width: 96,
      align: "right",
      render: (_, r) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{r.stats_7d?.triggers ?? 0}</span>,
    },
    {
      title: t("flow.col.users"),
      width: 96,
      align: "right",
      render: (_, r) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{r.stats_7d?.users ?? 0}</span>,
    },
    {
      title: t("flow.col.engagement"),
      width: 120,
      render: (_, r) => (
        <Progress percent={Math.round((r.stats_7d?.engagement ?? 0) * 100)} size="small" strokeColor="var(--sc-primary)" />
      ),
    },
    {
      title: t("flow.col.completion"),
      width: 120,
      render: (_, r) => (
        <Progress percent={Math.round((r.stats_7d?.completion ?? 0) * 100)} size="small" strokeColor="var(--sc-success)" />
      ),
    },
    {
      title: t("flow.col.updated"),
      dataIndex: "updated_at",
      width: 130,
      render: (v: string) => (
        <span title={fullTime(v)} style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>
          {listTime(v)}
        </span>
      ),
    },
    {
      title: t("common.actions"),
      width: 150,
      fixed: "right",
      render: (_, r) => (
        <div style={{ display: "flex", gap: 2 }}>
          <Button type="text" size="small" icon={<EditOutlined />} onClick={() => navigate(`/automation/flows/${r.id}`)}>
            {t("flow.action.edit")}
          </Button>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "data", icon: <BarChartOutlined />, label: t("flow.action.data") },
                { key: "test", icon: <ExperimentOutlined />, label: t("flow.action.test") },
                { key: "dup", icon: <CopyOutlined />, label: t("flow.action.duplicate") },
                { type: "divider" },
                { key: "del", icon: <DeleteOutlined />, label: t("flow.action.delete"), danger: true },
              ],
              onClick: ({ key }) => {
                if (key === "data") setStatsFlow(r);
                else if (key === "test") navigate(`/automation/flows/${r.id}?test=1`);
                else if (key === "dup") duplicate.mutate(r.id);
                else if (key === "del") {
                  modal.confirm({
                    title: t("common.confirmDeleteTitle"),
                    content: r.name,
                    okText: t("common.delete"),
                    okButtonProps: { danger: true },
                    cancelText: t("common.cancel"),
                    onOk: () => remove.mutate(r.id),
                  });
                }
              },
            }}
          >
            <Button type="text" size="small" icon={<MoreOutlined />} />
          </Dropdown>
        </div>
      ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("auto.nav.flows")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setGalleryOpen(true)}>
          {t("flow.create")}
        </Button>
      </div>

      <div className="sc-page-body" style={{ display: "flex", gap: 16, padding: 16 }}>
        {/* category folders */}
        <div style={{ flex: "none", width: 172 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--sc-text-tertiary)" }}>{t("flow.categories")}</span>
            <Button type="text" size="small" icon={<PlusOutlined />} onClick={promptNewCategory} />
          </div>
          {[
            { id: ALL, name: t("flow.allFlows") },
            { id: UNCAT, name: t("flow.uncategorized") },
            ...(categories.data ?? []).map((c) => ({ id: c.id, name: c.name })),
          ].map((c) => (
            <button
              key={c.id}
              type="button"
              className={`sc-view-item${activeCat === c.id ? " sc-active" : ""}`}
              onClick={() => setActiveCat(c.id)}
            >
              <FolderOutlined />
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</span>
              <span className="sc-view-count">{countFor(c.id)}</span>
            </button>
          ))}
        </div>

        {/* flow table */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {flows.isLoading ? (
            <Skeleton active paragraph={{ rows: 6 }} />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<ThunderboltOutlined />}
              title={t("flow.empty")}
              hint={t("flow.emptyHint")}
              action={
                <Button type="primary" icon={<PlusOutlined />} onClick={() => setGalleryOpen(true)}>
                  {t("flow.create")}
                </Button>
              }
            />
          ) : (
            <>
              <div className="sc-fe-hint" style={{ marginBottom: 8 }}>
                {t("flow.7dHint")}
              </div>
              <Table<FlowSummary>
                rowKey="id"
                size="small"
                columns={columns}
                dataSource={filtered}
                pagination={false}
                scroll={{ x: 1000 }}
              />
            </>
          )}
        </div>
      </div>

      <TemplateGallery
        open={galleryOpen}
        categoryId={activeCat === ALL || activeCat === UNCAT ? null : activeCat}
        onClose={() => setGalleryOpen(false)}
      />

      <Drawer
        title={statsFlow?.name}
        open={!!statsFlow}
        onClose={() => setStatsFlow(null)}
        width={420}
      >
        {statsFlow && <FlowStatsView flowId={statsFlow.id} summary={statsFlow} />}
      </Drawer>
    </div>
  );
}

function FlowStatsView({ flowId, summary }: { flowId: string; summary: FlowSummary }) {
  const stats = useQuery({
    queryKey: ["flow-stats", flowId],
    queryFn: () => flowsApi.stats(flowId),
    retry: 1,
  });
  const s = stats.data?.summary ?? summary.stats_7d ?? { triggers: 0, users: 0, engagement: 0, completion: 0 };
  const cell = (label: string, value: string) => (
    <div style={{ border: "1px solid var(--sc-border)", borderRadius: 8, padding: "12px 14px", flex: 1 }}>
      <div style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="sc-fe-hint">{t("flow.7dHint")}</div>
      <div style={{ display: "flex", gap: 10 }}>
        {cell(t("flow.col.triggers"), String(s.triggers))}
        {cell(t("flow.col.users"), String(s.users))}
      </div>
      <div style={{ display: "flex", gap: 10 }}>
        {cell(t("flow.col.engagement"), `${Math.round(s.engagement * 100)}%`)}
        {cell(t("flow.col.completion"), `${Math.round(s.completion * 100)}%`)}
      </div>
    </div>
  );
}
