/** 話術庫 — folders (個人/公共) + quick replies with star/shortcut. */
import {
  FolderOutlined,
  FolderOpenOutlined,
  PlusOutlined,
  StarFilled,
  StarOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { App, Button, Form, Input, List, Modal, Popconfirm, Radio, Select, Tag } from "antd";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { quickRepliesApi } from "@/api/endpoints";
import type { QuickReply } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

export function QuickRepliesPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [folderId, setFolderId] = useState<string | "all">("all");
  const [modalOpen, setModalOpen] = useState(false);
  const [folderModalOpen, setFolderModalOpen] = useState(false);
  const [editing, setEditing] = useState<QuickReply | null>(null);
  const [form] = Form.useForm();
  const [folderForm] = Form.useForm();

  const folders = useQuery({
    queryKey: ["qr-folders"],
    queryFn: () => quickRepliesApi.folders(),
    retry: 1,
  });

  const replies = useQuery({
    queryKey: ["quick-replies", folderId],
    queryFn: () => quickRepliesApi.list(folderId === "all" ? {} : { folder_id: folderId }),
    retry: 1,
  });

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["quick-replies"] });
    void qc.invalidateQueries({ queryKey: ["qr-folders"] });
  };

  const save = useMutation({
    mutationFn: (values: {
      title: string;
      content: string;
      folder_id?: string;
      visibility: "personal" | "public";
    }) =>
      editing
        ? quickRepliesApi.update(editing.id, values)
        : quickRepliesApi.create(values),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      invalidate();
      setModalOpen(false);
      setEditing(null);
      form.resetFields();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const createFolder = useMutation({
    mutationFn: (values: { name: string; visibility: "personal" | "public" }) =>
      quickRepliesApi.createFolder(values),
    onSuccess: () => {
      message.success(t("common.createSuccess"));
      invalidate();
      setFolderModalOpen(false);
      folderForm.resetFields();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const toggleStar = useMutation({
    mutationFn: (qr: QuickReply) => quickRepliesApi.update(qr.id, { starred: !qr.starred }),
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (id: string) => quickRepliesApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      invalidate();
    },
  });

  const personalFolders = useMemo(
    () => (folders.data ?? []).filter((f) => f.visibility === "personal"),
    [folders.data],
  );
  const publicFolders = useMemo(
    () => (folders.data ?? []).filter((f) => f.visibility === "public"),
    [folders.data],
  );

  const folderBtn = (id: string | "all", name: string, icon: React.ReactNode) => (
    <button
      key={id}
      type="button"
      className={`sc-view-item${folderId === id ? " sc-active" : ""}`}
      onClick={() => setFolderId(id)}
    >
      {icon}
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
    </button>
  );

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("qr.title")}</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<FolderOutlined />} onClick={() => setFolderModalOpen(true)}>
            {t("qr.newFolder")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              setEditing(null);
              form.setFieldsValue({ title: "", content: "", visibility: "personal", folder_id: undefined });
              setModalOpen(true);
            }}
          >
            {t("qr.add")}
          </Button>
        </div>
      </div>

      <div className="sc-page-body" style={{ display: "flex", gap: 16, overflow: "hidden", padding: 0 }}>
        <div
          style={{
            width: 220,
            flex: "none",
            background: "var(--sc-bg-container)",
            borderRight: "1px solid var(--sc-border)",
            padding: 10,
            overflowY: "auto",
          }}
        >
          {folderBtn("all", t("common.all"), <FolderOpenOutlined />)}
          <div className="sc-view-section">
            <span>{t("qr.personal")}</span>
          </div>
          {personalFolders.map((f) => folderBtn(f.id, f.name, <FolderOutlined />))}
          <div className="sc-view-section">
            <span>{t("qr.public")}</span>
          </div>
          {publicFolders.map((f) => folderBtn(f.id, f.name, <FolderOutlined />))}
        </div>

        <div style={{ flex: 1, minWidth: 0, overflowY: "auto", padding: 16 }}>
          <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", marginBottom: 10 }}>
            <ThunderboltOutlined /> {t("qr.shortcutHint")}
          </div>
          <List
            loading={replies.isLoading}
            dataSource={replies.data ?? []}
            locale={{
              emptyText: <EmptyState icon={<ThunderboltOutlined />} title={t("qr.empty")} />,
            }}
            renderItem={(qr) => (
              <List.Item
                style={{
                  background: "var(--sc-bg-container)",
                  border: "1px solid var(--sc-border)",
                  borderRadius: 8,
                  marginBottom: 8,
                  padding: "10px 14px",
                }}
                actions={[
                  <Button
                    key="star"
                    type="text"
                    size="small"
                    icon={
                      qr.starred ? (
                        <StarFilled style={{ color: "var(--sc-warning)" }} />
                      ) : (
                        <StarOutlined />
                      )
                    }
                    onClick={() => toggleStar.mutate(qr)}
                    aria-label={t("qr.star")}
                  />,
                  <Button
                    key="edit"
                    type="link"
                    size="small"
                    onClick={() => {
                      setEditing(qr);
                      form.setFieldsValue({
                        title: qr.title,
                        content: qr.content,
                        visibility: qr.visibility,
                        folder_id: qr.folder_id ?? undefined,
                      });
                      setModalOpen(true);
                    }}
                  >
                    {t("common.edit")}
                  </Button>,
                  <Popconfirm
                    key="del"
                    title={t("common.confirmDeleteTitle")}
                    okText={t("common.confirm")}
                    cancelText={t("common.cancel")}
                    onConfirm={() => remove.mutate(qr.id)}
                  >
                    <Button type="link" size="small" danger>
                      {t("common.delete")}
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <span>
                      {qr.title}
                      <Tag style={{ marginLeft: 8, fontSize: 10 }}>
                        {qr.visibility === "public" ? t("qr.public") : t("qr.personal")}
                      </Tag>
                    </span>
                  }
                  description={
                    <span
                      style={{
                        display: "block",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {qr.content}
                    </span>
                  }
                />
              </List.Item>
            )}
          />
        </div>
      </div>

      {/* quick reply modal */}
      <Modal
        title={editing ? t("common.edit") : t("qr.add")}
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
            name="title"
            label={t("qr.titleField")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={40} showCount />
          </Form.Item>
          <Form.Item
            name="content"
            label={t("qr.content")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input.TextArea rows={4} maxLength={2000} showCount />
          </Form.Item>
          <Form.Item name="folder_id" label={t("qr.folder")}>
            <Select
              allowClear
              options={(folders.data ?? []).map((f) => ({ value: f.id, label: f.name }))}
            />
          </Form.Item>
          <Form.Item name="visibility" label={t("qr.visibility")} initialValue="personal">
            <Radio.Group
              options={[
                { label: t("qr.personal"), value: "personal" },
                { label: t("qr.public"), value: "public" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* folder modal */}
      <Modal
        title={t("qr.newFolder")}
        open={folderModalOpen}
        onCancel={() => setFolderModalOpen(false)}
        onOk={() => folderForm.submit()}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        confirmLoading={createFolder.isPending}
        destroyOnHidden
      >
        <Form form={folderForm} layout="vertical" onFinish={(v) => createFolder.mutate(v)}>
          <Form.Item
            name="name"
            label={t("qr.folderName")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={30} showCount />
          </Form.Item>
          <Form.Item name="visibility" label={t("qr.visibility")} initialValue="personal">
            <Radio.Group
              options={[
                { label: t("qr.personal"), value: "personal" },
                { label: t("qr.public"), value: "public" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
