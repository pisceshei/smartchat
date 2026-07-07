/** 角色管理 — RBAC permission matrix editor (modules × view/edit/manage). */
import { CrownOutlined, PlusOutlined, SafetyOutlined } from "@ant-design/icons";
import { App, Button, Checkbox, Form, Input, Modal, Popconfirm, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { rolesApi } from "@/api/endpoints";
import type { Role } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

const MODULES: { key: string; label: string }[] = [
  { key: "inbox", label: t("nav.inbox") },
  { key: "customers", label: t("nav.customers") },
  { key: "marketing", label: t("nav.marketing") },
  { key: "automation", label: t("nav.automation") },
  { key: "reports", label: t("nav.reports") },
  { key: "integrations", label: t("nav.integrations") },
  { key: "team", label: t("nav.team") },
  { key: "settings", label: t("nav.settings") },
];

const LEVELS: { key: string; label: string }[] = [
  { key: "view", label: t("roles.perm.view") },
  { key: "edit", label: t("roles.perm.edit") },
  { key: "manage", label: t("roles.perm.manage") },
];

function PermissionMatrix({
  value,
  onChange,
  disabled,
}: {
  value: string[];
  onChange: (perms: string[]) => void;
  disabled?: boolean;
}) {
  const has = (p: string) => value.includes(p);
  const toggle = (p: string, checked: boolean) => {
    onChange(checked ? [...value, p] : value.filter((x) => x !== p));
  };
  return (
    <table style={{ width: "100%", borderCollapse: "collapse" }}>
      <thead>
        <tr>
          <th style={{ textAlign: "left", padding: "8px 12px", fontSize: 12.5, color: "var(--sc-text-secondary)", borderBottom: "1px solid var(--sc-border)" }}>
            {t("roles.module")}
          </th>
          {LEVELS.map((l) => (
            <th key={l.key} style={{ padding: "8px 12px", fontSize: 12.5, color: "var(--sc-text-secondary)", borderBottom: "1px solid var(--sc-border)" }}>
              {l.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {MODULES.map((m) => (
          <tr key={m.key}>
            <td style={{ padding: "8px 12px", fontSize: 13.5, borderBottom: "1px solid var(--sc-border)" }}>
              {m.label}
            </td>
            {LEVELS.map((l) => {
              const perm = `${m.key}.${l.key}`;
              return (
                <td key={l.key} style={{ textAlign: "center", borderBottom: "1px solid var(--sc-border)" }}>
                  <Checkbox
                    checked={has(perm)}
                    disabled={disabled}
                    onChange={(e) => toggle(perm, e.target.checked)}
                  />
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RolesPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [perms, setPerms] = useState<string[]>([]);
  const [dirty, setDirty] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm] = Form.useForm();

  const roles = useQuery({ queryKey: ["roles"], queryFn: () => rolesApi.list(), retry: 1 });
  const selected = (roles.data ?? []).find((r) => r.id === selectedId) ?? null;

  useEffect(() => {
    if (!selectedId && (roles.data ?? []).length > 0) {
      setSelectedId(roles.data![0].id);
    }
  }, [roles.data, selectedId]);

  useEffect(() => {
    setPerms(selected?.permissions ?? []);
    setDirty(false);
  }, [selected?.id, selected?.permissions]);

  const save = useMutation({
    mutationFn: () => rolesApi.update(selectedId!, { permissions: perms }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      setDirty(false);
      void qc.invalidateQueries({ queryKey: ["roles"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const create = useMutation({
    mutationFn: (values: { name: string }) => rolesApi.create({ name: values.name, permissions: [] }),
    onSuccess: (r) => {
      message.success(t("common.createSuccess"));
      setCreateOpen(false);
      createForm.resetFields();
      void qc.invalidateQueries({ queryKey: ["roles"] });
      setSelectedId(r.id);
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => rolesApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      setSelectedId(null);
      void qc.invalidateQueries({ queryKey: ["roles"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<Role> = [
    {
      title: t("roles.name"),
      key: "name",
      render: (_, r) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          {r.is_system ? (
            <CrownOutlined style={{ color: "var(--sc-warning)" }} />
          ) : (
            <SafetyOutlined style={{ color: "var(--sc-text-tertiary)" }} />
          )}
          {r.name}
          {r.is_system && (
            <Tag color="gold" style={{ fontSize: 10 }}>
              {t("roles.superAdmin")}
            </Tag>
          )}
        </span>
      ),
    },
    { title: t("roles.members"), dataIndex: "member_count", width: 80, align: "center" },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("roles.title")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          {t("roles.add")}
        </Button>
      </div>
      <div className="sc-page-body" style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <div style={{ width: 320, flex: "none" }}>
          <Table<Role>
            rowKey="id"
            size="small"
            columns={columns}
            dataSource={roles.data ?? []}
            loading={roles.isLoading}
            pagination={false}
            onRow={(r) => ({ onClick: () => setSelectedId(r.id), style: { cursor: "pointer" } })}
            rowClassName={(r) => (r.id === selectedId ? "ant-table-row-selected" : "")}
            locale={{ emptyText: <EmptyState compact icon={<SafetyOutlined />} title={t("common.emptyData")} /> }}
          />
        </div>

        <div
          style={{
            flex: 1,
            minWidth: 0,
            background: "var(--sc-bg-container)",
            border: "1px solid var(--sc-border)",
            borderRadius: 10,
            padding: 16,
          }}
        >
          {selected ? (
            <>
              <div style={{ display: "flex", alignItems: "center", marginBottom: 12 }}>
                <b style={{ fontSize: 15 }}>
                  {selected.name} — {t("roles.permissions")}
                </b>
                <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
                  {!selected.is_system && (
                    <Popconfirm
                      title={t("common.confirmDeleteTitle")}
                      okText={t("common.confirm")}
                      cancelText={t("common.cancel")}
                      onConfirm={() => remove.mutate(selected.id)}
                    >
                      <Button danger>{t("common.delete")}</Button>
                    </Popconfirm>
                  )}
                  <Button
                    type="primary"
                    disabled={selected.is_system || !dirty}
                    loading={save.isPending}
                    onClick={() => save.mutate()}
                  >
                    {t("common.save")}
                  </Button>
                </div>
              </div>
              {selected.is_system && (
                <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", marginBottom: 10 }}>
                  {t("roles.superAdminHint")}
                </div>
              )}
              <PermissionMatrix
                value={selected.is_system ? MODULES.flatMap((m) => LEVELS.map((l) => `${m.key}.${l.key}`)) : perms}
                disabled={selected.is_system}
                onChange={(p) => {
                  setPerms(p);
                  setDirty(true);
                }}
              />
            </>
          ) : (
            <EmptyState compact icon={<SafetyOutlined />} title={t("common.emptyData")} />
          )}
        </div>
      </div>

      <Modal
        title={t("roles.add")}
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        confirmLoading={create.isPending}
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical" onFinish={(v) => create.mutate(v)}>
          <Form.Item
            name="name"
            label={t("roles.name")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input maxLength={30} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
