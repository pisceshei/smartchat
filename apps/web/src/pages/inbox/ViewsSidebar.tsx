/** Inbox pane 1 — system tabs + custom views (personal/public) + create. */
import {
  ApartmentOutlined,
  EyeOutlined,
  FolderOutlined,
  InboxOutlined,
  PlusOutlined,
  RobotOutlined,
  TeamOutlined,
  UserOutlined,
  UserSwitchOutlined,
} from "@ant-design/icons";
import { App, Form, Input, Modal, Radio, Select, Skeleton } from "antd";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { inboxApi, type InboxTab } from "@/api/endpoints";
import type { InboxView } from "@/api/types";
import { CHANNEL_CATALOG } from "@/constants/channels";
import { t } from "@/i18n";
import { useInboxSummary, useInboxViews } from "./hooks";

// AI 成員 leads the list and is the inbox default (AI-first reception is the
// product's primary workflow) — keep in sync with InboxPage's initial tab.
const SYSTEM_TABS: { key: InboxTab; labelKey: Parameters<typeof t>[0]; icon: React.ReactNode }[] = [
  { key: "ai", labelKey: "inbox.tab.ai", icon: <ApartmentOutlined /> },
  { key: "mine", labelKey: "inbox.tab.mine", icon: <UserOutlined /> },
  { key: "bot", labelKey: "inbox.tab.bot", icon: <RobotOutlined /> },
  { key: "unassigned", labelKey: "inbox.tab.unassigned", icon: <UserSwitchOutlined /> },
  { key: "all", labelKey: "inbox.tab.all", icon: <InboxOutlined /> },
  { key: "team", labelKey: "inbox.tab.team", icon: <TeamOutlined /> },
];

export function ViewsSidebar({
  tab,
  viewId,
  onSelectTab,
  onSelectView,
}: {
  tab: InboxTab;
  viewId?: string;
  onSelectTab: (tab: InboxTab) => void;
  onSelectView: (viewId: string) => void;
}) {
  const summary = useInboxSummary();
  const views = useInboxViews();
  const [createOpen, setCreateOpen] = useState(false);

  const personal = (views.data ?? []).filter((v) => v.visibility === "personal");
  const publicViews = (views.data ?? []).filter((v) => v.visibility === "public");

  const countOf = (key: InboxTab): number | undefined =>
    summary.data ? summary.data[key] : undefined;

  const renderView = (v: InboxView) => (
    <button
      key={v.id}
      type="button"
      className={`sc-view-item${viewId === v.id ? " sc-active" : ""}`}
      onClick={() => onSelectView(v.id)}
    >
      <FolderOutlined />
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {v.name}
      </span>
      {summary.data?.views?.[v.id] !== undefined && (
        <span className="sc-view-count">{summary.data.views[v.id]}</span>
      )}
    </button>
  );

  return (
    <aside className="sc-inbox-views" aria-label={t("inbox.title")}>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {SYSTEM_TABS.map(({ key, labelKey, icon }) => (
          <button
            key={key}
            type="button"
            className={`sc-view-item${!viewId && tab === key ? " sc-active" : ""}`}
            onClick={() => onSelectTab(key)}
          >
            {icon}
            <span>{t(labelKey)}</span>
            {countOf(key) !== undefined && <span className="sc-view-count">{countOf(key)}</span>}
          </button>
        ))}
      </div>

      <div className="sc-view-section">
        <span>{t("inbox.views.custom")}</span>
        <PlusOutlined
          className="sc-clickable"
          aria-label={t("inbox.views.create")}
          onClick={() => setCreateOpen(true)}
        />
      </div>

      {views.isLoading ? (
        <div style={{ padding: "4px 10px" }}>
          <Skeleton active paragraph={{ rows: 2 }} title={false} />
        </div>
      ) : (views.data ?? []).length === 0 ? (
        <div style={{ padding: "6px 10px", fontSize: 12, color: "var(--sc-text-tertiary)" }}>
          {t("inbox.views.empty")}
        </div>
      ) : (
        <>
          {personal.length > 0 && (
            <>
              <div style={{ padding: "4px 10px", fontSize: 11, color: "var(--sc-text-tertiary)" }}>
                {t("inbox.views.personal")}
              </div>
              {personal.map(renderView)}
            </>
          )}
          {publicViews.length > 0 && (
            <>
              <div style={{ padding: "4px 10px", fontSize: 11, color: "var(--sc-text-tertiary)" }}>
                {t("inbox.views.public")}
              </div>
              {publicViews.map(renderView)}
            </>
          )}
        </>
      )}

      <CreateViewModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </aside>
  );
}

function CreateViewModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [form] = Form.useForm();
  const qc = useQueryClient();
  const { message } = App.useApp();

  const create = useMutation({
    mutationFn: (values: {
      name: string;
      visibility: "personal" | "public";
      channel_type?: string;
      status?: string;
    }) =>
      inboxApi.createView({
        name: values.name,
        visibility: values.visibility,
        filters: {
          channel_type: (values.channel_type as InboxView["filters"]["channel_type"]) ?? null,
          status: (values.status as InboxView["filters"]["status"]) ?? null,
        },
      }),
    onSuccess: () => {
      message.success(t("common.createSuccess"));
      void qc.invalidateQueries({ queryKey: ["inbox-views"] });
      form.resetFields();
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Modal
      title={t("inbox.views.create")}
      open={open}
      onCancel={onClose}
      okText={t("common.create")}
      cancelText={t("common.cancel")}
      confirmLoading={create.isPending}
      onOk={() => form.submit()}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        onFinish={(v) => create.mutate(v)}
        initialValues={{ visibility: "personal" }}
      >
        <Form.Item
          name="name"
          label={t("inbox.views.name")}
          rules={[{ required: true, message: t("common.required") }]}
        >
          <Input maxLength={30} showCount prefix={<EyeOutlined style={{ color: "var(--sc-text-tertiary)" }} />} />
        </Form.Item>
        <Form.Item name="visibility" label={t("inbox.views.visibility")}>
          <Radio.Group
            options={[
              { label: t("inbox.views.personal"), value: "personal" },
              { label: t("inbox.views.public"), value: "public" },
            ]}
          />
        </Form.Item>
        <Form.Item name="channel_type" label={t("inbox.views.channel")}>
          <Select
            allowClear
            placeholder={t("common.all")}
            options={CHANNEL_CATALOG.map((c) => ({ value: c.type, label: c.name }))}
          />
        </Form.Item>
        <Form.Item name="status" label={t("inbox.views.status")}>
          <Select
            allowClear
            placeholder={t("common.all")}
            options={[
              { value: "open", label: t("inbox.status.open") },
              { value: "closed", label: t("inbox.status.closed") },
            ]}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
