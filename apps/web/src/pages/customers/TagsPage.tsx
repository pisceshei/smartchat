/** 標籤管理 — 訪客標籤 / 會話標籤 CRUD. */
import { PlusOutlined, TagsOutlined } from "@ant-design/icons";
import { App, Button, Form, Input, Modal, Popconfirm, Table, Tabs, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { tagsApi } from "@/api/endpoints";
import type { Tag as TagType } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { TAG_COLORS } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";

function ColorPicker({ value, onChange }: { value?: string; onChange?: (v: string) => void }) {
  return (
    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
      {TAG_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          onClick={() => onChange?.(c)}
          aria-label={c}
          style={{
            width: 24,
            height: 24,
            borderRadius: 6,
            background: c,
            cursor: "pointer",
            border: value === c ? "2px solid var(--sc-text-heading)" : "2px solid transparent",
          }}
        />
      ))}
    </div>
  );
}

function TagTable({ kind }: { kind: "visitor" | "conversation" }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<TagType | null>(null);
  const [form] = Form.useForm();

  const query = useQuery({
    queryKey: ["tags", kind],
    queryFn: () => tagsApi.list(kind),
    retry: 1,
  });

  const save = useMutation({
    mutationFn: (values: { name: string; color: string }) =>
      editing ? tagsApi.update(editing.id, values) : tagsApi.create({ kind, ...values }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["tags", kind] });
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => tagsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["tags", kind] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<TagType> = [
    {
      title: t("tags.name"),
      key: "name",
      render: (_, tg) => <Tag color={tg.color}>{tg.name}</Tag>,
    },
    {
      title: t("tags.count"),
      dataIndex: "usage_count",
      width: 120,
      render: (v) => v ?? 0,
    },
    {
      title: "建立時間",
      dataIndex: "created_at",
      width: 180,
      render: (v) => fullTime(v) || "-",
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_, tg) => (
        <span>
          <Button
            type="link"
            size="small"
            onClick={() => {
              setEditing(tg);
              form.setFieldsValue({ name: tg.name, color: tg.color });
              setModalOpen(true);
            }}
          >
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("common.confirmDeleteTitle")}
            description={t("common.confirmDeleteDesc")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => remove.mutate(tg.id)}
          >
            <Button type="link" size="small" danger>
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </span>
      ),
    },
  ];

  return (
    <>
      <div style={{ marginBottom: 12, textAlign: "right" }}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            form.setFieldsValue({ name: "", color: TAG_COLORS[0] });
            setModalOpen(true);
          }}
        >
          {t("tags.add")}
        </Button>
      </div>
      <Table<TagType>
        rowKey="id"
        size="middle"
        columns={columns}
        dataSource={query.data ?? []}
        loading={query.isLoading}
        pagination={false}
        locale={{
          emptyText: <EmptyState icon={<TagsOutlined />} title={t("tags.empty")} />,
        }}
      />
      <Modal
        title={editing ? t("common.edit") : t("tags.add")}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        confirmLoading={save.isPending}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={(v) => save.mutate(v)}>
          <Form.Item
            name="name"
            label={t("tags.name")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={20} showCount />
          </Form.Item>
          <Form.Item name="color" label={t("tags.color")} rules={[{ required: true }]}>
            <ColorPicker />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

export function TagsPage() {
  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("cust.nav.tags")}</h1>
      </div>
      <div className="sc-page-body">
        <Tabs
          items={[
            { key: "visitor", label: t("tags.visitor"), children: <TagTable kind="visitor" /> },
            {
              key: "conversation",
              label: t("tags.conversation"),
              children: <TagTable kind="conversation" />,
            },
          ]}
        />
      </div>
    </div>
  );
}
