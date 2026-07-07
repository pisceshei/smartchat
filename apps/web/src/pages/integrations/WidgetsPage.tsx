/** 聊天外掛 — widget list + install modal (embed snippet). */
import { CodeOutlined, CopyOutlined, GlobalOutlined, PlusOutlined } from "@ant-design/icons";
import { App, Button, Modal, Popconfirm, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { widgetsApi } from "@/api/endpoints";
import type { WidgetConfig } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { WidgetCreateModal } from "./ConnectModals";

export function widgetEmbedSnippet(widgetKey: string): string {
  const host = location.origin;
  return `<script>
  (function (w, d) {
    w.SmartChatKey = "${widgetKey}";
    var s = d.createElement("script");
    s.src = "${host}/js/project_${widgetKey}.js";
    s.async = true;
    d.head.appendChild(s);
  })(window, document);
</script>`;
}

export function InstallModal({
  widget,
  onClose,
}: {
  widget: WidgetConfig | null;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const snippet = widget ? widgetEmbedSnippet(widget.widget_key) : "";
  return (
    <Modal
      title={t("widget.installTitle")}
      open={!!widget}
      onCancel={onClose}
      footer={[
        <Button
          key="copy"
          type="primary"
          icon={<CopyOutlined />}
          onClick={() => {
            void navigator.clipboard.writeText(snippet).then(() => message.success(t("common.copied")));
          }}
        >
          {t("common.copy")}
        </Button>,
      ]}
      width={640}
    >
      <p style={{ color: "var(--sc-text-secondary)" }}>{t("widget.installHint")}</p>
      <pre className="sc-code-block">{snippet}</pre>
    </Modal>
  );
}

export function WidgetsPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [createOpen, setCreateOpen] = useState(false);
  const [installTarget, setInstallTarget] = useState<WidgetConfig | null>(null);

  const query = useQuery({
    queryKey: ["widgets"],
    queryFn: () => widgetsApi.list(),
    retry: 1,
  });

  const remove = useMutation({
    mutationFn: (id: string) => widgetsApi.remove(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["widgets"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const columns: ColumnsType<WidgetConfig> = [
    {
      title: t("widget.name"),
      dataIndex: "name",
      render: (v, w) => (
        <span
          className="sc-clickable"
          style={{ color: "var(--sc-primary)", fontWeight: 500 }}
          onClick={() => navigate(`/integrations/widgets/${w.id}`)}
        >
          <GlobalOutlined style={{ marginRight: 6 }} />
          {v}
        </span>
      ),
    },
    {
      title: "Key",
      dataIndex: "widget_key",
      render: (v) => <span className="sc-mono" style={{ fontSize: 12 }}>{v}</span>,
    },
    {
      title: t("widget.config.allowedDomains"),
      dataIndex: "allowed_domains",
      render: (v: string[]) =>
        v.length > 0 ? (
          <span>
            {v.slice(0, 2).map((d) => (
              <Tag key={d} style={{ fontSize: 11 }}>
                {d}
              </Tag>
            ))}
            {v.length > 2 && <Tag style={{ fontSize: 11 }}>+{v.length - 2}</Tag>}
          </span>
        ) : (
          <span className="sc-text-tertiary">{t("common.all")}</span>
        ),
    },
    {
      title: t("common.status"),
      dataIndex: "status",
      width: 100,
      render: (v: WidgetConfig["status"]) => (
        <Tag color={v === "active" ? "success" : "default"}>
          {v === "active" ? t("common.enabled") : t("common.disabled")}
        </Tag>
      ),
    },
    {
      title: t("common.actions"),
      key: "actions",
      width: 220,
      render: (_, w) => (
        <span>
          <Button type="link" size="small" onClick={() => navigate(`/integrations/widgets/${w.id}`)}>
            {t("common.edit")}
          </Button>
          <Button type="link" size="small" icon={<CodeOutlined />} onClick={() => setInstallTarget(w)}>
            {t("widget.install")}
          </Button>
          <Popconfirm
            title={t("common.confirmDeleteTitle")}
            okText={t("common.confirm")}
            cancelText={t("common.cancel")}
            onConfirm={() => remove.mutate(w.id)}
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
        <h1 className="sc-page-title">{t("widget.title")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
          {t("widget.add")}
        </Button>
      </div>
      <div className="sc-page-body">
        <Table<WidgetConfig>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={query.data ?? []}
          loading={query.isLoading}
          pagination={false}
          locale={{
            emptyText: (
              <EmptyState
                icon={<GlobalOutlined />}
                title={t("widget.empty")}
                hint={t("widget.emptyHint")}
                action={
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
                    {t("widget.add")}
                  </Button>
                }
              />
            ),
          }}
        />
      </div>

      <WidgetCreateModal open={createOpen} onClose={() => setCreateOpen(false)} />
      <InstallModal widget={installTarget} onClose={() => setInstallTarget(null)} />
    </div>
  );
}
