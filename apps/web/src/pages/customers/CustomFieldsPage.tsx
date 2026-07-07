/** 客戶自定義字段 — field definition CRUD (text/number/date/select/bool). */
import { DatabaseOutlined, PlusOutlined } from "@ant-design/icons";
import { App, Button, Form, Input, Modal, Popconfirm, Select, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { customFieldsApi } from "@/api/endpoints";
import type { CustomFieldDef } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

const TYPE_LABEL: Record<CustomFieldDef["field_type"], string> = {
  text: t("cf.type.text"),
  number: t("cf.type.number"),
  date: t("cf.type.date"),
  select: t("cf.type.select"),
  bool: t("cf.type.bool"),
};

export function CustomFieldsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<CustomFieldDef | null>(null);
  const [form] = Form.useForm();
  const fieldType = Form.useWatch("field_type", form);

  const query = useQuery({
    queryKey: ["custom-fields"],
    queryFn: () => customFieldsApi.list(),
    retry: 1,
  });

  const save = useMutation({
    mutationFn: (values: { key: string; label: string; field_type: CustomFieldDef["field_type"]; options_text?: string }) => {
      const options =
        values.field_type === "select"
          ? (values.options_text ?? "")
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean)
          : undefined;
      const body = { key: values.key, label: values.label, field_type: values.field_type, options };
      return editing ? customFieldsApi.update(editing.id, body) : customFieldsApi.create(body);
    },
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["custom-fields"] });
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => customFieldsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["custom-fields"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<CustomFieldDef> = [
    { title: t("cf.label"), dataIndex: "label" },
    {
      title: t("cf.key"),
      dataIndex: "key",
      render: (v) => <span className="sc-mono">{v}</span>,
    },
    {
      title: t("cf.type"),
      dataIndex: "field_type",
      width: 120,
      render: (v: CustomFieldDef["field_type"]) => <Tag>{TYPE_LABEL[v]}</Tag>,
    },
    {
      title: t("cf.options"),
      dataIndex: "options",
      render: (v: string[] | null) =>
        v && v.length > 0 ? (
          <span>
            {v.slice(0, 4).map((o) => (
              <Tag key={o} style={{ fontSize: 11 }}>
                {o}
              </Tag>
            ))}
            {v.length > 4 && <Tag style={{ fontSize: 11 }}>+{v.length - 4}</Tag>}
          </span>
        ) : (
          <span className="sc-text-tertiary">-</span>
        ),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_, f) => (
        <span>
          <Button
            type="link"
            size="small"
            onClick={() => {
              setEditing(f);
              form.setFieldsValue({
                key: f.key,
                label: f.label,
                field_type: f.field_type,
                options_text: (f.options ?? []).join("\n"),
              });
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
            onConfirm={() => remove.mutate(f.id)}
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
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("cf.title")}</h1>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            form.resetFields();
            setModalOpen(true);
          }}
        >
          {t("cf.add")}
        </Button>
      </div>
      <div className="sc-page-body">
        <Table<CustomFieldDef>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={query.data ?? []}
          loading={query.isLoading}
          pagination={false}
          locale={{ emptyText: <EmptyState icon={<DatabaseOutlined />} title={t("cf.empty")} /> }}
        />
      </div>

      <Modal
        title={editing ? t("common.edit") : t("cf.add")}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText={t("common.save")}
        cancelText={t("common.cancel")}
        confirmLoading={save.isPending}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" onFinish={(v) => save.mutate(v)} initialValues={{ field_type: "text" }}>
          <Form.Item
            name="label"
            label={t("cf.label")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={30} showCount />
          </Form.Item>
          <Form.Item
            name="key"
            label={t("cf.key")}
            extra={t("cf.keyHint")}
            rules={[
              { required: true, message: t("common.required") },
              { pattern: /^[a-z][a-z0-9_]*$/i, message: t("cf.keyHint") },
            ]}
          >
            <Input maxLength={40} disabled={!!editing} />
          </Form.Item>
          <Form.Item name="field_type" label={t("cf.type")} rules={[{ required: true }]}>
            <Select
              disabled={!!editing}
              options={(Object.keys(TYPE_LABEL) as CustomFieldDef["field_type"][]).map((k) => ({
                value: k,
                label: TYPE_LABEL[k],
              }))}
            />
          </Form.Item>
          {fieldType === "select" && (
            <Form.Item
              name="options_text"
              label={t("cf.options")}
              rules={[{ required: true, message: t("common.required") }]}
            >
              <Input.TextArea rows={4} />
            </Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  );
}
