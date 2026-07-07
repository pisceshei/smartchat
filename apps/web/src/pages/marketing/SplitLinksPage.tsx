/** 行銷 › 分流連結 — list + create form (name / channel / strategy / accounts /
 *  prefill) + QR display + click-stats drawer. Contract: /split-links.
 *  Public redirect (/s/{slug}) is served by the edge app, not here. */
import {
  BarChartOutlined,
  CopyOutlined,
  DeleteOutlined,
  PlusOutlined,
  QrcodeOutlined,
  ShareAltOutlined,
} from "@ant-design/icons";
import {
  App,
  Button,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Skeleton,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { channelsApi, splitLinksApi } from "@/api/endpoints";
import type { SplitLink, SplitLinkTarget, SplitStrategy } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import "./marketing.css";

/** Channels a split-link can route to (external chat channels). */
const SPLIT_CHANNELS = ["whatsapp_api", "whatsapp_app", "messenger", "telegram_bot", "line_oa"];

export function SplitLinksPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [formOpen, setFormOpen] = useState(false);
  const [qr, setQr] = useState<SplitLink | null>(null);
  const [stats, setStats] = useState<SplitLink | null>(null);

  const list = useQuery({ queryKey: ["split-links"], queryFn: () => splitLinksApi.list(), retry: 1 });

  const remove = useMutation({
    mutationFn: (id: string) => splitLinksApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["split-links"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const copy = (url: string) => {
    void navigator.clipboard.writeText(url);
    message.success(t("common.copied"));
  };

  const rows = list.data ?? [];

  const columns: ColumnsType<SplitLink> = [
    { title: t("sl.col.name"), dataIndex: "name", render: (v: string) => <span style={{ fontWeight: 600 }}>{v}</span> },
    {
      title: t("sl.col.channel"),
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
      title: t("sl.col.status"),
      dataIndex: "status",
      width: 100,
      render: (v: SplitLink["status"]) => (
        <Tag color={v === "active" ? "success" : "default"}>
          {t(`sl.status.${v}` as Parameters<typeof t>[0])}
        </Tag>
      ),
    },
    {
      title: t("sl.col.link"),
      dataIndex: "short_url",
      render: (v: string) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span className="sc-mono" style={{ fontSize: 12.5 }}>{v}</span>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => copy(v)} />
        </span>
      ),
    },
    {
      title: t("sl.col.qr"),
      width: 90,
      render: (_, r) => <Button type="text" size="small" icon={<QrcodeOutlined />} onClick={() => setQr(r)}>{t("sl.showQr")}</Button>,
    },
    {
      title: t("sl.clickCount"),
      dataIndex: "click_count",
      width: 90,
      align: "right",
      render: (v: number) => <span style={{ fontVariantNumeric: "tabular-nums" }}>{v}</span>,
    },
    {
      title: t("common.actions"),
      width: 120,
      fixed: "right",
      render: (_, r) => (
        <div style={{ display: "flex", gap: 2 }}>
          <Button type="text" size="small" icon={<BarChartOutlined />} onClick={() => setStats(r)} />
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
                onOk: () => remove.mutate(r.id),
              })
            }
          />
        </div>
      ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("mkt.nav.splitLinks")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>{t("sl.create")}</Button>
      </div>
      <div className="sc-page-body">
        <div className="sc-mkt-hint" style={{ marginBottom: 10 }}>
          <ShareAltOutlined /> {t("sl.stickyHint")}
        </div>
        {list.isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<ShareAltOutlined />}
            title={t("sl.empty")}
            hint={t("sl.emptyHint")}
            action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>{t("sl.create")}</Button>}
          />
        ) : (
          <Table<SplitLink> rowKey="id" size="small" columns={columns} dataSource={rows} pagination={false} scroll={{ x: 980 }} />
        )}
      </div>

      <SplitLinkForm open={formOpen} onClose={() => setFormOpen(false)} />

      <Modal title={t("sl.qr")} open={!!qr} onCancel={() => setQr(null)} footer={null}>
        {qr && (
          <div className="sc-qr-box">
            {qr.qr_url ? <img src={qr.qr_url} alt={qr.name} /> : <Empty description={t("common.emptyData")} />}
            <div className="sc-mono" style={{ fontSize: 12.5 }}>{qr.short_url}</div>
            <Button icon={<CopyOutlined />} onClick={() => copy(qr.short_url)}>{t("sl.copyLink")}</Button>
          </div>
        )}
      </Modal>

      <Drawer title={stats?.name} open={!!stats} onClose={() => setStats(null)} width={520} destroyOnHidden>
        {stats && <ClickStats link={stats} />}
      </Drawer>
    </div>
  );
}

/* ---------------------------------------------------------------- create form */
function SplitLinkForm({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [name, setName] = useState("");
  const [channelType, setChannelType] = useState("whatsapp_api");
  const [strategy, setStrategy] = useState<SplitStrategy>("random");
  const [accountIds, setAccountIds] = useState<string[]>([]);
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [prefill, setPrefill] = useState("");

  const accounts = useQuery({ queryKey: ["channel-accounts"], queryFn: () => channelsApi.listAccounts(), enabled: open, retry: 1 });
  const channelAccounts = (accounts.data ?? []).filter((a) => a.channel_type === channelType);

  const create = useMutation({
    mutationFn: () => {
      const targets: SplitLinkTarget[] = accountIds.map((id) => ({
        channel_account_id: id,
        weight: strategy === "random" ? (weights[id] ?? 1) : undefined,
        enabled: true,
      }));
      return splitLinksApi.create({ name: name.trim(), channel_type: channelType, strategy, targets, prefill_text: prefill });
    },
    onSuccess: () => {
      message.success(t("sl.saved"));
      void qc.invalidateQueries({ queryKey: ["split-links"] });
      reset();
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const reset = () => {
    setName(""); setChannelType("whatsapp_api"); setStrategy("random"); setAccountIds([]); setWeights({}); setPrefill("");
  };

  return (
    <Modal
      title={t("sl.create")}
      open={open}
      onCancel={() => { reset(); onClose(); }}
      onOk={() => {
        if (!name.trim()) { message.warning(t("sl.name")); return; }
        if (accountIds.length === 0) { message.warning(t("sl.accounts")); return; }
        create.mutate();
      }}
      confirmLoading={create.isPending}
      okText={t("common.create")}
      cancelText={t("common.cancel")}
      width={600}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingTop: 6 }}>
        <Field label={t("sl.name")}>
          <Input value={name} maxLength={50} showCount onChange={(e) => setName(e.target.value)} placeholder={t("sl.namePlaceholder")} />
        </Field>
        <Field label={t("sl.channel")}>
          <Select
            style={{ width: "100%" }}
            value={channelType}
            onChange={(v) => { setChannelType(v); setAccountIds([]); }}
            options={SPLIT_CHANNELS.map((c) => ({ value: c, label: CHANNEL_NAME[c] ?? c }))}
          />
        </Field>
        <Field label={t("sl.strategy")}>
          <Radio.Group
            value={strategy}
            onChange={(e) => setStrategy(e.target.value)}
            options={[
              { value: "random", label: t("sl.strategy.random") },
              { value: "time_period", label: t("sl.strategy.time_period") },
              { value: "sequential", label: t("sl.strategy.sequential") },
            ]}
            optionType="button"
          />
        </Field>
        <Field label={t("sl.accounts")} hint={t("sl.accountsHint")}>
          <Select
            mode="multiple"
            style={{ width: "100%" }}
            value={accountIds}
            onChange={setAccountIds}
            placeholder={t("common.select")}
            options={channelAccounts.map((a) => ({ value: a.id, label: a.display_name }))}
          />
          {strategy === "random" && accountIds.length > 1 && (
            <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
              {accountIds.map((id) => (
                <div key={id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1, fontSize: 13 }}>{channelAccounts.find((a) => a.id === id)?.display_name ?? id}</span>
                  <span className="sc-mkt-hint">{t("sl.weight")}</span>
                  <InputNumber min={1} value={weights[id] ?? 1} onChange={(v) => setWeights((w) => ({ ...w, [id]: v ?? 1 }))} size="small" />
                </div>
              ))}
            </div>
          )}
        </Field>
        <Field label={t("sl.prefill")} hint={t("sl.prefillHint")}>
          <Input.TextArea value={prefill} maxLength={300} showCount rows={3} onChange={(e) => setPrefill(e.target.value)} />
        </Field>
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------- click stats */
function ClickStats({ link }: { link: SplitLink }) {
  const clicks = useQuery({
    queryKey: ["split-link-clicks", link.id],
    queryFn: () => splitLinksApi.clicks(link.id),
    retry: 1,
  });

  const series = useMemo(() => {
    const byDate = new Map<string, number>();
    for (const p of clicks.data?.series ?? []) byDate.set(p.date, (byDate.get(p.date) ?? 0) + p.clicks);
    return Array.from(byDate.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([date, c]) => ({ date, clicks: c }));
  }, [clicks.data]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="sc-kpi-card">
        <div className="sc-kpi-label">{t("sl.clicksTotal")}</div>
        <div className="sc-kpi-value">{clicks.data?.total ?? link.click_count}</div>
      </div>
      <div>
        <div className="sc-mkt-hint" style={{ marginBottom: 8 }}>{t("sl.clicksTitle")}</div>
        {clicks.isLoading ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : series.length === 0 ? (
          <Empty description={t("sl.noClicks")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={series} margin={{ top: 8, right: 12, bottom: 4, left: -12 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--sc-border)" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} stroke="var(--sc-text-tertiary)" />
              <RTooltip />
              <Line type="monotone" dataKey="clicks" stroke="var(--sc-primary)" strokeWidth={2} dot={false} />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12.5, fontWeight: 500, color: "var(--sc-text-secondary)", marginBottom: 6 }}>{label}</div>
      {children}
      {hint && <div className="sc-mkt-hint">{hint}</div>}
    </div>
  );
}
