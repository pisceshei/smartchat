/** 自動化 › 意圖識別 — intent list with name / description / examples. Consumed
 *  by the 訪客意圖識別 trigger and AI escalation rules; the backend classifies
 *  each inbound message once against these (plan 附錄 B.2). */
import { AimOutlined, DeleteOutlined, EditOutlined, PlusOutlined } from "@ant-design/icons";
import { App, Button, Drawer, Form, Input, Select, Skeleton, Switch, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { intentsApi } from "@/api/endpoints";
import type { Intent } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

interface FormShape {
  name: string;
  description?: string;
  examples: string[];
  enabled: boolean;
}

function IntentDrawer({
  open,
  editing,
  onClose,
}: {
  open: boolean;
  editing: Intent | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<FormShape>();

  useEffect(() => {
    if (open) {
      form.setFieldsValue(
        editing
          ? { name: editing.name, description: editing.description ?? "", examples: editing.examples, enabled: editing.enabled }
          : { name: "", description: "", examples: [], enabled: true },
      );
    }
  }, [open, editing, form]);

  const save = useMutation({
    mutationFn: (v: FormShape) =>
      editing
        ? intentsApi.update(editing.id, v)
        : intentsApi.create({ name: v.name, description: v.description, examples: v.examples }),
    onSuccess: () => {
      message.success(t("intent.saved"));
      void qc.invalidateQueries({ queryKey: ["intents"] });
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Drawer
      title={editing ? t("common.edit") : t("intent.add")}
      open={open}
      onClose={onClose}
      width={440}
      extra={
        <Button type="primary" loading={save.isPending} onClick={() => form.submit()}>
          {t("common.save")}
        </Button>
      }
    >
      <Form<FormShape> form={form} layout="vertical" onFinish={(v) => save.mutate(v)}>
        <Form.Item name="name" label={t("intent.name")} rules={[{ required: true, message: t("common.required") }]}>
          <Input placeholder="查詢訂單狀態" />
        </Form.Item>
        <Form.Item name="description" label={t("intent.desc")}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 4 }} />
        </Form.Item>
        <Form.Item
          name="examples"
          label={t("intent.examples")}
          extra={t("intent.examplesHint")}
          rules={[
            {
              validator: (_r, v: string[]) =>
                (v?.length ?? 0) >= 3 ? Promise.resolve() : Promise.reject(new Error(t("intent.examplesMin"))),
            },
          ]}
        >
          <Select mode="tags" tokenSeparators={["\n"]} open={false} suffixIcon={null} placeholder={t("intent.examplesHint")} />
        </Form.Item>
        <Form.Item name="enabled" label={t("common.enabled")} valuePropName="checked">
          <Switch />
        </Form.Item>
      </Form>
    </Drawer>
  );
}

export function IntentsPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<Intent | null>(null);

  const intents = useQuery({
    queryKey: ["intents"],
    queryFn: () => intentsApi.list(),
    retry: 1,
  });

  const remove = useMutation({
    mutationFn: (id: string) => intentsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["intents"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<Intent> = [
    {
      title: t("intent.col.name"),
      dataIndex: "name",
      render: (v: string, r) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontWeight: 600 }}>{v}</span>
          {!r.enabled && <Tag>{t("common.disabled")}</Tag>}
        </span>
      ),
    },
    { title: t("intent.col.desc"), dataIndex: "description", render: (v?: string) => <span style={{ color: "var(--sc-text-secondary)" }}>{v || "—"}</span> },
    { title: t("intent.col.examples"), width: 90, align: "right", render: (_, r) => r.examples?.length ?? 0 },
    {
      title: t("common.actions"),
      width: 110,
      render: (_, r) => (
        <div style={{ display: "flex", gap: 2 }}>
          <Button
            type="text"
            size="small"
            icon={<EditOutlined />}
            onClick={() => {
              setEditing(r);
              setDrawerOpen(true);
            }}
          />
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
        <h1 className="sc-page-title">{t("intent.title")}</h1>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => {
            setEditing(null);
            setDrawerOpen(true);
          }}
        >
          {t("intent.add")}
        </Button>
      </div>
      <div className="sc-page-body">
        {intents.isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : (intents.data ?? []).length === 0 ? (
          <EmptyState
            icon={<AimOutlined />}
            title={t("intent.empty")}
            hint={t("intent.emptyHint")}
            action={
              <Button
                type="primary"
                icon={<PlusOutlined />}
                onClick={() => {
                  setEditing(null);
                  setDrawerOpen(true);
                }}
              >
                {t("intent.add")}
              </Button>
            }
          />
        ) : (
          <Table<Intent> rowKey="id" size="middle" columns={columns} dataSource={intents.data} pagination={false} />
        )}
      </div>

      <IntentDrawer open={drawerOpen} editing={editing} onClose={() => setDrawerOpen(false)} />
    </div>
  );
}
