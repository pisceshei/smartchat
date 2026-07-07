/** Inbox pane 3 — top action bar, message timeline (bubbles, date separators,
 *  system chips, delivery ticks, automation attribution), composer. */
import {
  CheckCircleOutlined,
  CheckOutlined,
  ClockCircleOutlined,
  DownOutlined,
  ExclamationCircleOutlined,
  HistoryOutlined,
  LoadingOutlined,
  RobotOutlined,
  TagsOutlined,
  ThunderboltOutlined,
  TranslationOutlined,
} from "@ant-design/icons";
import {
  App,
  Avatar,
  Button,
  Divider,
  Dropdown,
  Select,
  Skeleton,
  Spin,
  Switch,
  Tag as AntTag,
  Tooltip,
} from "antd";
import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { tagsApi } from "@/api/endpoints";
import type { Conversation, Message } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { MessageBlocks } from "@/components/MessageBlocks";
import { PresenceDot } from "@/components/PresenceDot";
import { TRANSLATE_LANGS } from "@/constants/channels";
import { t } from "@/i18n";
import { useRealtimeStore } from "@/stores/realtime";
import { dateSeparator, dayjs, msgTime } from "@/utils/time";
import { Composer } from "./Composer";
import { HistoryDrawer } from "./HistoryDrawer";
import { useMarkRead, useMessages, useUpdateConversation } from "./hooks";
import { useState } from "react";

function DeliveryTick({ status }: { status: Message["delivery_status"] }) {
  switch (status) {
    case "pending":
      return <LoadingOutlined spin style={{ fontSize: 11 }} />;
    case "sent":
      return (
        <Tooltip title={t("inbox.msg.delivery.sent")}>
          <CheckOutlined style={{ fontSize: 11 }} />
        </Tooltip>
      );
    case "delivered":
      return (
        <Tooltip title={t("inbox.msg.delivery.delivered")}>
          <span style={{ letterSpacing: -4 }}>
            <CheckOutlined style={{ fontSize: 11 }} />
            <CheckOutlined style={{ fontSize: 11 }} />
          </span>
        </Tooltip>
      );
    case "read":
      return (
        <Tooltip title={t("inbox.msg.delivery.read")}>
          <span style={{ letterSpacing: -4, color: "var(--sc-primary)" }}>
            <CheckOutlined style={{ fontSize: 11 }} />
            <CheckOutlined style={{ fontSize: 11 }} />
          </span>
        </Tooltip>
      );
    case "failed":
      return (
        <Tooltip title={t("inbox.msg.delivery.failed")}>
          <ExclamationCircleOutlined style={{ fontSize: 12, color: "var(--sc-error)" }} />
        </Tooltip>
      );
    default:
      return null;
  }
}

function MessageRow({ msg, contactName }: { msg: Message; contactName: string }) {
  const sysBlock = msg.content?.blocks?.find((b) => b.kind === "system_event");
  if (msg.sender_type === "system" || sysBlock) {
    return (
      <div className="sc-sys-chip sc-fade-in">
        <span>
          {sysBlock ? sysBlock.event : (msg.text_plain ?? "")} · {msgTime(msg.created_at)}
        </span>
      </div>
    );
  }

  const out = msg.direction === "out";
  const fromAutomation = msg.sender_type === "automation" || msg.sender_type === "flow" || !!msg.source_flow_id;
  const fromAi = msg.sender_type === "ai_agent";
  const translated = msg.translations && Object.values(msg.translations)[0];

  return (
    <div className={`sc-msg-row sc-fade-in${out ? " sc-out" : ""}`}>
      {!out && (
        <Avatar size={30} style={{ flex: "none", background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontSize: 13 }}>
          {contactName.slice(0, 1).toUpperCase()}
        </Avatar>
      )}
      <div className="sc-msg-stack">
        <div className={`sc-bubble${msg.is_note ? " sc-note" : ""}`}>
          {msg.is_note && (
            <div style={{ fontSize: 11, color: "var(--sc-warning)", marginBottom: 2, fontWeight: 600 }}>
              {t("inbox.msg.note")}
            </div>
          )}
          <MessageBlocks content={msg.content} />
          {translated && (
            <div className="sc-translated">
              <TranslationOutlined style={{ marginRight: 4 }} />
              {translated}
            </div>
          )}
        </div>
        <div className="sc-msg-meta">
          {out && msg.sender_name && <span>{msg.sender_name}</span>}
          {fromAutomation && (
            <AntTag
              icon={<ThunderboltOutlined />}
              color="blue"
              style={{ fontSize: 10, lineHeight: "16px", margin: 0, padding: "0 4px" }}
            >
              {t("inbox.conv.viaAutomation")}
            </AntTag>
          )}
          {fromAi && (
            <AntTag icon={<RobotOutlined />} color="purple" style={{ fontSize: 10, lineHeight: "16px", margin: 0, padding: "0 4px" }}>
              AI
            </AntTag>
          )}
          <span>{msgTime(msg.created_at)}</span>
          {out && !msg.is_note && <DeliveryTick status={msg.delivery_status} />}
        </div>
      </div>
    </div>
  );
}

export function ConversationView({ conversation }: { conversation: Conversation }) {
  const { message: antMessage, modal } = App.useApp();
  const messages = useMessages(conversation.id);
  const update = useUpdateConversation(conversation.id);
  const markRead = useMarkRead(conversation.id);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [historyOpen, setHistoryOpen] = useState(false);

  const typingUntil = useRealtimeStore((s) => s.typing[conversation.id]);
  const visitorOnline = useRealtimeStore((s) => s.visitorPresence[conversation.contact_id]);
  const isTyping = !!typingUntil && typingUntil > Date.now();

  const convTags = useQuery({
    queryKey: ["tags", "conversation"],
    queryFn: () => tagsApi.list("conversation"),
    retry: 1,
    staleTime: 60_000,
  });

  // flatten: pages[0] is newest batch; each page's items ascend by time
  const allMessages = useMemo(() => {
    const pages = messages.data?.pages ?? [];
    const flat: Message[] = [];
    for (let i = pages.length - 1; i >= 0; i--) flat.push(...pages[i].items);
    return flat.sort((a, b) => a.created_at.localeCompare(b.created_at));
  }, [messages.data]);

  // mark read when opened / new inbound while open
  useEffect(() => {
    if (conversation.agent_unread_count > 0) markRead.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id, conversation.agent_unread_count]);

  // autoscroll to bottom on new messages when already near bottom
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [allMessages.length, conversation.id]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (el.scrollTop < 60 && messages.hasNextPage && !messages.isFetchingNextPage) {
      const prevHeight = el.scrollHeight;
      void messages.fetchNextPage().then(() => {
        requestAnimationFrame(() => {
          if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight - prevHeight;
          }
        });
      });
    }
  };

  const contactName = conversation.contact?.display_name || t("inbox.cust.unnamed");
  const translateCfg = conversation.translate;

  const setTags = (tagIds: string[]) => {
    update.mutate({ tag_ids: tagIds });
  };

  const resolveConversation = () => {
    modal.confirm({
      title: t("inbox.conv.resolve"),
      icon: <CheckCircleOutlined style={{ color: "var(--sc-success)" }} />,
      okText: t("common.confirm"),
      cancelText: t("common.cancel"),
      onOk: () =>
        update.mutateAsync({ status: "closed" }).then(() => {
          antMessage.success(t("inbox.conv.resolved"));
        }),
    });
  };

  const tagMenu = {
    items: (convTags.data ?? []).map((tag) => {
      const active = (conversation.tags ?? []).some((x) => x.id === tag.id);
      return {
        key: tag.id,
        label: (
          <span>
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: 2,
                background: tag.color,
                marginRight: 8,
              }}
            />
            {tag.name}
            {active && <CheckOutlined style={{ marginLeft: 8, color: "var(--sc-primary)" }} />}
          </span>
        ),
      };
    }),
    onClick: ({ key }: { key: string }) => {
      const current = (conversation.tags ?? []).map((x) => x.id);
      setTags(current.includes(key) ? current.filter((x) => x !== key) : [...current, key]);
    },
  };

  return (
    <div className="sc-conv-pane">
      {/* top bar */}
      <div className="sc-conv-header">
        <Avatar size={34} style={{ background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontWeight: 600 }}>
          {contactName.slice(0, 1).toUpperCase()}
        </Avatar>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 14.5, color: "var(--sc-text-heading)", display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {contactName}
            </span>
            <ChannelIcon type={conversation.channel_type} size={15} />
          </div>
          <div style={{ fontSize: 12, color: "var(--sc-text-tertiary)", display: "flex", alignItems: "center", gap: 6 }}>
            <PresenceDot online={!!visitorOnline} />
            {isTyping
              ? t("inbox.conv.typing")
              : visitorOnline
                ? t("inbox.conv.visitorOnline")
                : t("inbox.conv.visitorOffline")}
          </div>
        </div>

        <div style={{ flex: 1 }} />

        {/* translate toggle */}
        <Tooltip title={t("inbox.conv.translate")}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <TranslationOutlined style={{ color: translateCfg?.enabled ? "var(--sc-primary)" : "var(--sc-text-tertiary)" }} />
            <Switch
              size="small"
              checked={!!translateCfg?.enabled}
              onChange={(enabled) =>
                update.mutate({
                  translate: {
                    enabled,
                    agent_lang: translateCfg?.agent_lang ?? "zh-TW",
                    customer_lang: translateCfg?.customer_lang ?? null,
                  },
                })
              }
            />
          </span>
        </Tooltip>
        {translateCfg?.enabled && (
          <Select
            size="small"
            style={{ width: 110 }}
            value={translateCfg.agent_lang ?? "zh-TW"}
            options={TRANSLATE_LANGS}
            onChange={(agent_lang) =>
              update.mutate({
                translate: { enabled: true, agent_lang, customer_lang: translateCfg.customer_lang },
              })
            }
            aria-label={t("inbox.conv.translateTo")}
          />
        )}

        <Divider type="vertical" />

        {/* managed toggle */}
        <Tooltip title={t("inbox.conv.managedHint")}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <RobotOutlined style={{ color: conversation.bot_managed ? "var(--sc-primary)" : "var(--sc-text-tertiary)" }} />
            <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>
              {t("inbox.conv.managed")}
            </span>
            <Switch
              size="small"
              checked={conversation.bot_managed}
              onChange={(bot_managed) => update.mutate({ bot_managed })}
            />
          </span>
        </Tooltip>

        <Divider type="vertical" />

        <Dropdown menu={tagMenu} trigger={["click"]}>
          <Button size="small" type="text" icon={<TagsOutlined />}>
            {t("inbox.conv.tags")}
            <DownOutlined style={{ fontSize: 10 }} />
          </Button>
        </Dropdown>

        <Tooltip title={t("inbox.conv.history")}>
          <Button size="small" type="text" icon={<HistoryOutlined />} onClick={() => setHistoryOpen(true)} />
        </Tooltip>

        {conversation.status === "open" ? (
          <Button size="small" icon={<CheckCircleOutlined />} onClick={resolveConversation}>
            {t("inbox.conv.resolve")}
          </Button>
        ) : (
          <Button size="small" onClick={() => update.mutate({ status: "open" })}>
            {t("inbox.conv.reopen")}
          </Button>
        )}
      </div>

      {/* conversation tags strip */}
      {(conversation.tags ?? []).length > 0 && (
        <div style={{ padding: "6px 14px 0", display: "flex", gap: 4, flexWrap: "wrap" }}>
          {(conversation.tags ?? []).map((tag) => (
            <AntTag
              key={tag.id}
              color={tag.color}
              closable
              onClose={(e) => {
                e.preventDefault();
                setTags((conversation.tags ?? []).filter((x) => x.id !== tag.id).map((x) => x.id));
              }}
            >
              {tag.name}
            </AntTag>
          ))}
        </div>
      )}

      {/* timeline */}
      <div className="sc-msg-scroll" ref={scrollRef} onScroll={onScroll}>
        {messages.isLoading ? (
          <div style={{ padding: 12 }}>
            <Skeleton active avatar paragraph={{ rows: 2 }} />
            <Skeleton active avatar paragraph={{ rows: 1 }} style={{ marginTop: 20 }} />
            <Skeleton active avatar paragraph={{ rows: 2 }} style={{ marginTop: 20 }} />
          </div>
        ) : (
          <>
            {messages.isFetchingNextPage && (
              <div style={{ textAlign: "center", padding: 6 }}>
                <Spin size="small" />
              </div>
            )}
            {!messages.hasNextPage && allMessages.length > 20 && (
              <div className="sc-date-sep">{t("inbox.conv.noMore")}</div>
            )}
            {allMessages.map((msg, i) => {
              const prev = allMessages[i - 1];
              const showDate = !prev || !dayjs(prev.created_at).isSame(msg.created_at, "day");
              return (
                <div key={msg.id}>
                  {showDate && <div className="sc-date-sep">{dateSeparator(msg.created_at)}</div>}
                  <MessageRow msg={msg} contactName={contactName} />
                </div>
              );
            })}
            {isTyping && (
              <div className="sc-msg-row">
                <div className="sc-bubble" style={{ color: "var(--sc-text-tertiary)" }}>
                  <ClockCircleOutlined /> {t("inbox.conv.typing")}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <Composer conversation={conversation} />

      <HistoryDrawer
        conversationId={conversation.id}
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
      />
    </div>
  );
}
