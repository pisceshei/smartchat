/** Contact detail drawer used from the customers table. */
import { CommentOutlined, MailOutlined, PhoneOutlined } from "@ant-design/icons";
import { Avatar, Button, Descriptions, Drawer, Skeleton, Tabs, Tag } from "antd";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { contactsApi } from "@/api/endpoints";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import { fullTime, listTime } from "@/utils/time";

export function ContactDrawer({
  contactId,
  onClose,
}: {
  contactId: string | null;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const open = !!contactId;

  const contact = useQuery({
    queryKey: ["contact", contactId],
    queryFn: () => contactsApi.get(contactId!),
    enabled: open,
    retry: 1,
  });

  const conversations = useQuery({
    queryKey: ["contact-conversations", contactId],
    queryFn: () => contactsApi.conversations(contactId!),
    enabled: open,
    retry: 0,
  });

  const c = contact.data;

  return (
    <Drawer open={open} onClose={onClose} width={480} title={t("common.detail")}>
      {contact.isLoading || !c ? (
        <Skeleton active avatar paragraph={{ rows: 8 }} />
      ) : (
        <>
          <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 18 }}>
            <Avatar size={56} src={c.avatar_url ?? undefined} style={{ background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontWeight: 600, fontSize: 22 }}>
              {(c.display_name ?? "?").slice(0, 1).toUpperCase()}
            </Avatar>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 16, fontWeight: 600 }}>
                {c.display_name ?? t("inbox.cust.unnamed")}
                {c.is_blacklisted && (
                  <Tag color="red" style={{ marginLeft: 8 }}>
                    {t("cust.blacklisted")}
                  </Tag>
                )}
              </div>
              <div style={{ fontSize: 12.5, color: "var(--sc-text-secondary)", display: "flex", gap: 12, marginTop: 2 }}>
                {c.email && (
                  <span>
                    <MailOutlined /> {c.email}
                  </span>
                )}
                {c.phone && (
                  <span>
                    <PhoneOutlined /> {c.phone}
                  </span>
                )}
              </div>
              <div style={{ marginTop: 6 }}>
                {(c.tags ?? []).map((tg) => (
                  <Tag key={tg.id} color={tg.color} style={{ fontSize: 11 }}>
                    {tg.name}
                  </Tag>
                ))}
              </div>
            </div>
            <Button type="primary" icon={<CommentOutlined />} onClick={() => navigate("/inbox")}>
              {t("cust.chat")}
            </Button>
          </div>

          <Tabs
            items={[
              {
                key: "info",
                label: t("inbox.cust.basicInfo"),
                children: (
                  <Descriptions column={1} size="small" bordered>
                    <Descriptions.Item label={t("inbox.cust.oneId")}>
                      <span className="sc-mono">{c?.one_id ?? c?.id}</span>
                    </Descriptions.Item>
                    <Descriptions.Item label={t("inbox.cust.language")}>{c.language ?? "-"}</Descriptions.Item>
                    <Descriptions.Item label={t("inbox.cust.country")}>
                      {[c.country, c.city].filter(Boolean).join(" · ") || "-"}
                    </Descriptions.Item>
                    <Descriptions.Item label={t("inbox.cust.ip")}>{c.last_ip ?? "-"}</Descriptions.Item>
                    <Descriptions.Item label={t("inbox.cust.browser")}>{c.browser ?? "-"}</Descriptions.Item>
                    <Descriptions.Item label={t("inbox.cust.device")}>{c.device ?? "-"}</Descriptions.Item>
                    <Descriptions.Item label={t("cust.col.lastActive")}>
                      {fullTime(c.last_active_at) || "-"}
                    </Descriptions.Item>
                  </Descriptions>
                ),
              },
              {
                key: "identities",
                label: t("cust.detail.identities"),
                children: (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {(c.channel_identities ?? []).length === 0 ? (
                      <EmptyState compact icon={<CommentOutlined />} title={t("common.emptyData")} />
                    ) : (
                      (c.channel_identities ?? []).map((ci) => (
                        <div
                          key={ci.id}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            border: "1px solid var(--sc-border)",
                            borderRadius: 8,
                            padding: "8px 12px",
                          }}
                        >
                          <ChannelIcon type={ci.channel_type} size={22} />
                          <div>
                            <div style={{ fontSize: 13, fontWeight: 500 }}>
                              {ci.display_name ?? CHANNEL_NAME[ci.channel_type]}
                            </div>
                            <div className="sc-mono" style={{ fontSize: 11.5, color: "var(--sc-text-tertiary)" }}>
                              {ci.external_user_id}
                            </div>
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                ),
              },
              {
                key: "custom",
                label: t("cust.detail.custom"),
                children:
                  Object.keys(c.custom ?? {}).length === 0 ? (
                    <EmptyState compact icon={<CommentOutlined />} title={t("cust.detail.noCustom")} />
                  ) : (
                    <Descriptions column={1} size="small" bordered>
                      {Object.entries(c.custom).map(([k, v]) => (
                        <Descriptions.Item key={k} label={k}>
                          {String(v)}
                        </Descriptions.Item>
                      ))}
                    </Descriptions>
                  ),
              },
              {
                key: "conversations",
                label: t("inbox.title"),
                children: (
                  <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                    {(conversations.data ?? []).length === 0 ? (
                      <EmptyState compact icon={<CommentOutlined />} title={t("common.emptyData")} />
                    ) : (
                      (conversations.data ?? []).map((conv) => (
                        <div
                          key={conv.id}
                          className="sc-clickable"
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 10,
                            border: "1px solid var(--sc-border)",
                            borderRadius: 8,
                            padding: "8px 12px",
                          }}
                          onClick={() => navigate(`/inbox/${conv.id}`)}
                        >
                          <ChannelIcon type={conv.channel_type} size={20} />
                          <span
                            style={{
                              flex: 1,
                              minWidth: 0,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                              fontSize: 13,
                            }}
                          >
                            {conv.snippet ?? "-"}
                          </span>
                          <span style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>
                            {listTime(conv.last_message_at)}
                          </span>
                        </div>
                      ))
                    )}
                  </div>
                ),
              },
            ]}
          />
        </>
      )}
    </Drawer>
  );
}
