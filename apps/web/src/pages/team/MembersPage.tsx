/** 成員管理 — presence, roles, groups, inline 接待上限 edit, invite. */
import { RobotOutlined, TeamOutlined, UserAddOutlined } from "@ant-design/icons";
import {
  App,
  Avatar,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Switch,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { groupsApi, membersApi, rolesApi } from "@/api/endpoints";
import type { Member } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { PresenceDot } from "@/components/PresenceDot";
import { t } from "@/i18n";
import { useRealtimeStore } from "@/stores/realtime";

export function MembersPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteForm] = Form.useForm();
  const memberPresence = useRealtimeStore((s) => s.memberPresence);

  const members = useQuery({ queryKey: ["members"], queryFn: () => membersApi.list(), retry: 1 });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => rolesApi.list(), retry: 1 });
  const groups = useQuery({ queryKey: ["member-groups"], queryFn: () => groupsApi.list(), retry: 1 });

  const update = useMutation({
    mutationFn: (vars: { id: string; body: Parameters<typeof membersApi.update>[1] }) =>
      membersApi.update(vars.id, vars.body),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const invite = useMutation({
    mutationFn: (values: { email: string; role_id?: string; group_ids?: string[] }) =>
      membersApi.invite(values),
    onSuccess: () => {
      message.success(t("team.invite.sent"));
      setInviteOpen(false);
      inviteForm.resetFields();
      void qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const groupName = (id: string) => (groups.data ?? []).find((g) => g.id === id)?.name ?? id;

  const columns: ColumnsType<Member> = [
    {
      title: t("team.col.member"),
      key: "member",
      width: 240,
      render: (_, m) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
          <Avatar
            size={32}
            src={m.avatar_url ?? undefined}
            icon={m.member_type === "ai_agent" ? <RobotOutlined /> : undefined}
            style={{
              background:
                m.member_type === "ai_agent" ? "#7C3AED" : "var(--sc-primary-bg-strong)",
              color: m.member_type === "ai_agent" ? "#fff" : "var(--sc-primary)",
              fontWeight: 600,
            }}
          >
            {m.member_type === "human" ? m.display_name.slice(0, 1).toUpperCase() : undefined}
          </Avatar>
          <span>
            <span style={{ fontWeight: 500 }}>
              {m.display_name}
              {m.member_type === "ai_agent" && (
                <Tag color="purple" style={{ marginLeft: 6, fontSize: 10 }}>
                  {t("team.aiMember")}
                </Tag>
              )}
            </span>
            {m.email && (
              <span style={{ display: "block", fontSize: 12, color: "var(--sc-text-tertiary)" }}>
                {m.email}
              </span>
            )}
          </span>
        </span>
      ),
    },
    {
      title: t("team.col.presence"),
      key: "presence",
      width: 110,
      render: (_, m) => (
        <PresenceDot
          online={memberPresence[m.id] ?? m.presence === "online"}
          showLabel
        />
      ),
    },
    {
      title: t("team.col.role"),
      key: "role",
      width: 160,
      render: (_, m) => (
        <Select
          size="small"
          style={{ width: 140 }}
          value={m.role_id ?? undefined}
          placeholder="-"
          options={(roles.data ?? []).map((r) => ({ value: r.id, label: r.name }))}
          onChange={(role_id) => update.mutate({ id: m.id, body: { role_id } })}
        />
      ),
    },
    {
      title: t("team.col.groups"),
      key: "groups",
      width: 160,
      render: (_, m) =>
        (m.group_ids ?? []).length > 0 ? (
          <span>
            {(m.group_ids ?? []).map((gid) => (
              <Tag key={gid} style={{ fontSize: 11 }}>
                {groupName(gid)}
              </Tag>
            ))}
          </span>
        ) : (
          <span className="sc-text-tertiary">-</span>
        ),
    },
    {
      title: t("team.col.active"),
      dataIndex: "active_conversations",
      width: 90,
      align: "center",
    },
    {
      title: t("team.col.todayTotal"),
      dataIndex: "today_total",
      width: 90,
      align: "center",
    },
    {
      title: t("team.col.capacity"),
      key: "cap",
      width: 150,
      render: (_, m) => (
        <InputNumber
          size="small"
          min={0}
          max={999}
          defaultValue={m.daily_cap}
          onBlur={(e) => {
            const v = Number((e.target as HTMLInputElement).value);
            if (!Number.isNaN(v) && v !== m.daily_cap) {
              update.mutate({ id: m.id, body: { daily_cap: v } });
            }
          }}
        />
      ),
    },
    {
      title: t("common.status"),
      key: "enabled",
      width: 90,
      render: (_, m) => (
        <Switch
          size="small"
          checked={m.enabled}
          onChange={(enabled) => update.mutate({ id: m.id, body: { enabled } })}
        />
      ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("team.nav.members")}</h1>
        <Button type="primary" icon={<UserAddOutlined />} onClick={() => setInviteOpen(true)}>
          {t("team.invite")}
        </Button>
      </div>
      <div className="sc-page-body">
        <Table<Member>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={members.data ?? []}
          loading={members.isLoading}
          pagination={false}
          scroll={{ x: 1050 }}
          locale={{ emptyText: <EmptyState icon={<TeamOutlined />} title={t("team.empty")} /> }}
        />
      </div>

      <Modal
        title={t("team.invite")}
        open={inviteOpen}
        onCancel={() => setInviteOpen(false)}
        onOk={() => inviteForm.submit()}
        okText={t("team.invite")}
        cancelText={t("common.cancel")}
        confirmLoading={invite.isPending}
        destroyOnHidden
      >
        <Form form={inviteForm} layout="vertical" onFinish={(v) => invite.mutate(v)}>
          <Form.Item
            name="email"
            label={t("team.invite.email")}
            rules={[
              { required: true, message: t("common.required") },
              { type: "email", message: t("auth.emailInvalid") },
            ]}
          >
            <Input placeholder="member@example.com" />
          </Form.Item>
          <Form.Item name="role_id" label={t("team.invite.role")}>
            <Select
              allowClear
              options={(roles.data ?? []).map((r) => ({ value: r.id, label: r.name }))}
            />
          </Form.Item>
          <Form.Item name="group_ids" label={t("team.invite.groups")}>
            <Select
              mode="multiple"
              allowClear
              options={(groups.data ?? []).map((g) => ({ value: g.id, label: g.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
