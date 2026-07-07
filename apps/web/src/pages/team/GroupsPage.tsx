/** 分組 — simple CRUD. */
import { PlusOutlined, TeamOutlined } from "@ant-design/icons";
import { App, Button, Form, Input, Modal, Popconfirm, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { groupsApi } from "@/api/endpoints";
import type { MemberGroup } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

export function GroupsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<MemberGroup | null>(null);
  const [form] = Form.useForm();

  const query = useQuery({ queryKey: ["member-groups"], queryFn: () => groupsApi.list(), retry: 1 });

  const save = useMutation({
    mutationFn: (values: { name: string; description?: string }) =>
      editing ? groupsApi.update(editing.id, values) : groupsApi.create(values),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["member-groups"] });
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => groupsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["member-groups"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<MemberGroup> = [
    { title: t("groups.name"), dataIndex: "name" },
    {
      title: t("groups.desc"),
      dataIndex: "description",
      render: (v) => v || <span className="sc-text-tertiary">-</span>,
    },
    { title: t("groups.memberCount"), dataIndex: "member_count", width: 110, align: "center" },
    {
      title: t("common.actions"),
      key: "actions",
      width: 140,
      render: (_, g) => (
        <span>
          <Button
            type="link"
            size="small"
            onClick={() => {
              setEditing(g);
              form.setFieldsValue({ name: g.name, description: g.description ?? "" });
              setModalOpen(true);
            }}
          >
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("common.confirmDeleteTitle")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => remove.mutate(g.id)}
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
        <h1 className="sc-page-title">{t("groups.title")}</h1>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            form.resetFields();
            setModalOpen(true);
          }}
        >
          {t("groups.add")}
        </Button>
      </div>
      <div className="sc-page-body">
        <Table<MemberGroup>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={query.data ?? []}
          loading={query.isLoading}
          pagination={false}
          locale={{ emptyText: <EmptyState icon={<TeamOutlined />} title={t("common.emptyData")} /> }}
        />
      </div>

      <Modal
        title={editing ? t("common.edit") : t("groups.add")}
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
            label={t("groups.name")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={30} showCount />
          </Form.Item>
          <Form.Item name="description" label={t("groups.desc")}>
            <Input.TextArea rows={3} maxLength={200} showCount />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
