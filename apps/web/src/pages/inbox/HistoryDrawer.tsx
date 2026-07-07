/** Past service sessions of the same contact/thread. */
import { HistoryOutlined } from "@ant-design/icons";
import { Drawer, List, Skeleton, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import { inboxApi } from "@/api/endpoints";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";

export function HistoryDrawer({
  conversationId,
  open,
  onClose,
}: {
  conversationId: string;
  open: boolean;
  onClose: () => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["conversation-history", conversationId],
    queryFn: () => inboxApi.history(conversationId),
    enabled: open,
    retry: 1,
  });

  return (
    <Drawer title={t("inbox.conv.history")} open={open} onClose={onClose} width={380}>
      {isLoading ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : (data ?? []).length === 0 ? (
        <EmptyState compact icon={<HistoryOutlined />} title={t("common.emptyData")} />
      ) : (
        <List
          dataSource={data}
          renderItem={(conv) => (
            <List.Item>
              <List.Item.Meta
                avatar={<ChannelIcon type={conv.channel_type} size={22} />}
                title={
                  <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <span style={{ fontSize: 13 }}>{fullTime(conv.created_at)}</span>
                    <Tag color={conv.status === "open" ? "green" : "default"} style={{ fontSize: 11 }}>
                      {conv.status === "open" ? t("inbox.status.open") : t("inbox.status.closed")}
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
                    {conv.snippet ?? "-"}
                  </span>
                }
              />
            </List.Item>
          )}
        />
      )}
    </Drawer>
  );
}
