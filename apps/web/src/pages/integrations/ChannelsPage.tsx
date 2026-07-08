/** 社媒渠道 — gallery of 19 channel cards with connect/manage per type. */
import { AppstoreOutlined, DeleteOutlined, PlusOutlined, SettingOutlined } from "@ant-design/icons";
import { App, Badge, Button, Card, Drawer, List, Popconfirm, Skeleton, Tag } from "antd";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { channelsApi } from "@/api/endpoints";
import type { ChannelAccount, ChannelAccountStatus, ChannelType } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { CHANNEL_CATALOG, type ChannelMeta } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";
import {
  EmailConnectModal,
  LineConnectModal,
  MetaConnectModal,
  SlackConnectModal,
  TelegramConnectModal,
  TikTokBusinessConnectModal,
  VkConnectModal,
  WeChatKfConnectModal,
  WeComConnectModal,
  WhatsAppApiConnectModal,
  WhatsAppAppConnectModal,
  WidgetCreateModal,
  YouTubeConnectModal,
  ZaloConnectModal,
} from "./ConnectModals";

const STATUS_META: Record<ChannelAccountStatus, { color: string; label: string }> = {
  active: { color: "success", label: t("int.status.active") },
  error: { color: "error", label: t("int.status.error") },
  pending: { color: "warning", label: t("int.status.pending") },
  disconnected: { color: "default", label: t("int.status.disconnected") },
};

export function ChannelsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [connectType, setConnectType] = useState<ChannelType | null>(null);
  const [manageType, setManageType] = useState<ChannelType | null>(null);

  const accounts = useQuery({
    queryKey: ["channel-accounts"],
    queryFn: () => channelsApi.listAccounts(),
    retry: 1,
  });

  const removeAccount = useMutation({
    mutationFn: (id: string) => channelsApi.removeAccount(id),
    onSuccess: () => {
      message.success(t("common.deleteSuccess"));
      void qc.invalidateQueries({ queryKey: ["channel-accounts"] });
    },
    onError: (e) =>
      message.error(e instanceof ApiError && e.message ? e.message : t("common.operationFailed")),
  });

  const byType = useMemo(() => {
    const map = new Map<string, ChannelAccount[]>();
    for (const acc of accounts.data ?? []) {
      const arr = map.get(acc.channel_type) ?? [];
      arr.push(acc);
      map.set(acc.channel_type, arr);
    }
    return map;
  }, [accounts.data]);

  const renderCard = (meta: ChannelMeta) => {
    const accs = byType.get(meta.type) ?? [];
    const worst: ChannelAccountStatus | null =
      accs.length === 0
        ? null
        : accs.some((a) => a.status === "error")
          ? "error"
          : accs.some((a) => a.status === "pending")
            ? "pending"
            : accs.some((a) => a.status === "disconnected")
              ? "disconnected"
              : "active";

    return (
      <Card
        key={meta.type}
        size="small"
        hoverable
        styles={{ body: { padding: 16, display: "flex", flexDirection: "column", gap: 8, height: "100%" } }}
        style={{ height: "100%" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <ChannelIcon type={meta.type} size={34} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 14, display: "flex", alignItems: "center", gap: 6 }}>
              {meta.name}
              {meta.beta && (
                <Tag color="orange" style={{ fontSize: 10, lineHeight: "16px", margin: 0 }}>
                  Beta
                </Tag>
              )}
            </div>
            <div style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>
              {accs.length > 0 ? (
                <Badge
                  status={STATUS_META[worst!].color as "success" | "error" | "warning" | "default"}
                  text={
                    <span style={{ fontSize: 12 }}>
                      {accs.length} {t("int.accounts")} · {STATUS_META[worst!].label}
                    </span>
                  }
                />
              ) : (
                t("int.notConnected")
              )}
            </div>
          </div>
        </div>
        <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)", flex: 1, lineHeight: 1.6 }}>
          {t(meta.descKey)}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {meta.connectable ? (
            <>
              <Button
                type="primary"
                size="small"
                ghost
                icon={<PlusOutlined />}
                onClick={() => setConnectType(meta.type)}
                style={{ flex: 1 }}
              >
                {t("int.connect")}
              </Button>
              <Button
                size="small"
                icon={<SettingOutlined />}
                disabled={accs.length === 0}
                onClick={() => setManageType(meta.type)}
                style={{ flex: 1 }}
              >
                {t("int.manage")}
              </Button>
            </>
          ) : (
            <Button size="small" disabled style={{ flex: 1 }}>
              {t("common.comingSoon")}
            </Button>
          )}
        </div>
      </Card>
    );
  };

  const manageAccounts = manageType ? (byType.get(manageType) ?? []) : [];
  const manageMeta = CHANNEL_CATALOG.find((c) => c.type === manageType);

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("int.nav.channels")}</h1>
      </div>
      <div className="sc-page-body">
        {accounts.isLoading ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 14 }}>
            {[...Array(8)].map((_, i) => (
              <Card key={i} size="small">
                <Skeleton active avatar paragraph={{ rows: 2 }} />
              </Card>
            ))}
          </div>
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 14 }}>
            {CHANNEL_CATALOG.map(renderCard)}
          </div>
        )}
      </div>

      {/* connect modals */}
      <TelegramConnectModal open={connectType === "telegram_bot"} onClose={() => setConnectType(null)} />
      <MetaConnectModal
        open={connectType === "messenger"}
        channelType="messenger"
        onClose={() => setConnectType(null)}
      />
      <MetaConnectModal
        open={connectType === "instagram"}
        channelType="instagram"
        onClose={() => setConnectType(null)}
      />
      <LineConnectModal
        open={connectType === "line_oa"}
        channelType="line_oa"
        onClose={() => setConnectType(null)}
      />
      <EmailConnectModal open={connectType === "email"} onClose={() => setConnectType(null)} />
      <WhatsAppApiConnectModal
        open={connectType === "whatsapp_api"}
        onClose={() => setConnectType(null)}
      />
      <WhatsAppAppConnectModal
        open={connectType === "whatsapp_app"}
        onClose={() => setConnectType(null)}
      />
      <SlackConnectModal open={connectType === "slack"} onClose={() => setConnectType(null)} />
      <VkConnectModal open={connectType === "vk"} onClose={() => setConnectType(null)} />
      <WeChatKfConnectModal open={connectType === "wechat_kf"} onClose={() => setConnectType(null)} />
      <WeComConnectModal open={connectType === "wecom"} onClose={() => setConnectType(null)} />
      <ZaloConnectModal open={connectType === "zalo_app"} onClose={() => setConnectType(null)} />
      <YouTubeConnectModal open={connectType === "youtube"} onClose={() => setConnectType(null)} />
      <TikTokBusinessConnectModal
        open={connectType === "tiktok_business"}
        onClose={() => setConnectType(null)}
      />
      <WidgetCreateModal open={connectType === "widget"} onClose={() => setConnectType(null)} />

      {/* manage drawer */}
      <Drawer
        title={`${manageMeta?.name ?? ""} — ${t("int.manage")}`}
        open={!!manageType}
        onClose={() => setManageType(null)}
        width={420}
      >
        {manageAccounts.length === 0 ? (
          <EmptyState compact icon={<AppstoreOutlined />} title={t("int.notConnected")} />
        ) : (
          <List
            dataSource={manageAccounts}
            renderItem={(acc) => (
              <List.Item
                actions={[
                  <Popconfirm
                    key="del"
                    title={t("common.confirmDeleteTitle")}
                    okText={t("common.confirm")}
                    cancelText={t("common.cancel")}
                    onConfirm={() => removeAccount.mutate(acc.id)}
                  >
                    <Button type="text" danger size="small" icon={<DeleteOutlined />}>
                      {t("int.removeAccount")}
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  avatar={<ChannelIcon type={acc.channel_type} size={26} />}
                  title={
                    <span>
                      {acc.display_name}
                      <Tag
                        color={STATUS_META[acc.status].color}
                        style={{ marginLeft: 8, fontSize: 11 }}
                      >
                        {STATUS_META[acc.status].label}
                      </Tag>
                    </span>
                  }
                  description={
                    <span style={{ fontSize: 12 }}>
                      <span className="sc-mono">{acc.external_id}</span>
                      <br />
                      {fullTime(acc.created_at)}
                    </span>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </div>
  );
}
