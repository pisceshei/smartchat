/** 行銷 › 第三方代發 (EDM) — list + create form. Same UI, provider-swappable
 *  delivery backend (smtp/ses/sendgrid/edm_provider). Contract: /edm. */
import { MailOutlined, PlusOutlined } from "@ant-design/icons";
import {
  App,
  Button,
  DatePicker,
  Input,
  Modal,
  Radio,
  Select,
  Skeleton,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { edmApi, msgTemplatesApi, segmentsApi } from "@/api/endpoints";
import type { BroadcastSchedule, EdmCampaign, EdmProvider, Segment } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { dayjs, listTime } from "@/utils/time";
import { SegmentBuilder } from "./SegmentBuilder";
import "./marketing.css";

const PROVIDERS: EdmProvider[] = ["smtp", "ses", "sendgrid", "edm_provider"];

export function EdmPage() {
  const qc = useQueryClient();
  const [formOpen, setFormOpen] = useState(false);
  const list = useQuery({ queryKey: ["edm"], queryFn: () => edmApi.list(), retry: 1 });
  const rows = list.data ?? [];

  const columns: ColumnsType<EdmCampaign> = [
    { title: t("edm.col.name"), dataIndex: "name", render: (v: string) => <span style={{ fontWeight: 600 }}>{v}</span> },
    {
      title: t("edm.col.provider"),
      dataIndex: "provider",
      width: 140,
      render: (v: EdmProvider) => t(`edm.provider.${v}` as Parameters<typeof t>[0]),
    },
    {
      title: t("edm.col.status"),
      dataIndex: "status",
      width: 100,
      render: (v: string) => <Tag>{t(`bc.status.${v}` as Parameters<typeof t>[0])}</Tag>,
    },
    { title: t("edm.col.sent"), dataIndex: "sent_count", width: 100, align: "right", render: (v) => v ?? 0 },
    { title: t("edm.col.created"), dataIndex: "created_at", width: 120, render: (v: string) => listTime(v) },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("mkt.nav.edm")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>{t("edm.create")}</Button>
      </div>
      <div className="sc-page-body">
        {list.isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<MailOutlined />}
            title={t("edm.empty")}
            hint={t("edm.emptyHint")}
            action={<Button type="primary" icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>{t("edm.create")}</Button>}
          />
        ) : (
          <Table<EdmCampaign> rowKey="id" size="small" columns={columns} dataSource={rows} pagination={false} />
        )}
      </div>
      <EdmForm open={formOpen} onClose={() => setFormOpen(false)} />
    </div>
  );
}

function EdmForm({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [segBuilderOpen, setSegBuilderOpen] = useState(false);
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<EdmProvider>("smtp");
  const [segmentId, setSegmentId] = useState<string>();
  const [templateId, setTemplateId] = useState<string>();
  const [mode, setMode] = useState<BroadcastSchedule["mode"]>("immediate");
  const [sendAt, setSendAt] = useState<string | null>(null);

  const segments = useQuery({ queryKey: ["segments"], queryFn: () => segmentsApi.list(), enabled: open, retry: 1 });
  const templates = useQuery({ queryKey: ["msg-templates", "email"], queryFn: () => msgTemplatesApi.list("email"), enabled: open, retry: 1 });

  const create = useMutation({
    mutationFn: () =>
      edmApi.create({
        name: name.trim(),
        provider,
        segment_id: segmentId as string,
        template_id: templateId as string,
        schedule: { mode, send_at: mode === "scheduled" ? sendAt : null, timezone: "Asia/Hong_Kong" },
      }),
    onSuccess: () => {
      message.success(t("edm.saved"));
      void qc.invalidateQueries({ queryKey: ["edm"] });
      reset();
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const reset = () => { setName(""); setProvider("smtp"); setSegmentId(undefined); setTemplateId(undefined); setMode("immediate"); setSendAt(null); };

  return (
    <Modal
      title={t("edm.create")}
      open={open}
      onCancel={() => { reset(); onClose(); }}
      onOk={() => {
        if (!name.trim() || !segmentId || !templateId) { message.warning(t("common.required")); return; }
        create.mutate();
      }}
      confirmLoading={create.isPending}
      okText={t("common.create")}
      cancelText={t("common.cancel")}
      width={560}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingTop: 6 }}>
        <Field label={t("edm.name")}><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label={t("edm.provider")}>
          <Select style={{ width: "100%" }} value={provider} onChange={setProvider} options={PROVIDERS.map((p) => ({ value: p, label: t(`edm.provider.${p}` as Parameters<typeof t>[0]) }))} />
        </Field>
        <Field label={t("edm.segment")}>
          <div style={{ display: "flex", gap: 8 }}>
            <Select
              style={{ flex: 1 }}
              value={segmentId}
              onChange={setSegmentId}
              placeholder={t("common.select")}
              loading={segments.isLoading}
              options={(segments.data ?? []).map((s) => ({ value: s.id, label: s.name }))}
            />
            <Button icon={<PlusOutlined />} onClick={() => setSegBuilderOpen(true)}>{t("bc.wiz.createSegment")}</Button>
          </div>
        </Field>
        <Field label={t("edm.template")}>
          <Select
            style={{ width: "100%" }}
            value={templateId}
            onChange={setTemplateId}
            placeholder={t("common.select")}
            loading={templates.isLoading}
            notFoundContent={t("tpl.empty")}
            options={(templates.data ?? []).map((tp) => ({ value: tp.id, label: tp.name }))}
          />
        </Field>
        <Field label={t("edm.schedule")}>
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            options={[
              { value: "immediate", label: t("bc.wiz.immediate") },
              { value: "scheduled", label: t("bc.wiz.scheduled") },
            ]}
            optionType="button"
          />
          {mode === "scheduled" && (
            <div style={{ marginTop: 8 }}>
              <DatePicker showTime format="YYYY-MM-DD HH:mm" value={sendAt ? dayjs(sendAt) : null} onChange={(v) => setSendAt(v ? v.toISOString() : null)} />
            </div>
          )}
        </Field>
      </div>
      <SegmentBuilder
        open={segBuilderOpen}
        onClose={() => setSegBuilderOpen(false)}
        onCreated={(seg: Segment) => {
          setSegBuilderOpen(false);
          void qc.invalidateQueries({ queryKey: ["segments"] });
          setSegmentId(seg.id);
        }}
      />
    </Modal>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 12.5, fontWeight: 500, color: "var(--sc-text-secondary)", marginBottom: 6 }}>{label}</div>
      {children}
    </div>
  );
}
