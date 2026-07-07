/** Inbox pane 2 — searchable, filterable, virtualized conversation list. */
import { CommentOutlined, SearchOutlined } from "@ant-design/icons";
import { Avatar, Badge, Input, Segmented, Skeleton, Spin } from "antd";
import VirtualList from "rc-virtual-list";
import { useEffect, useMemo, useRef, useState } from "react";
import type { InboxListFilter } from "@/api/endpoints";
import type { Conversation } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { listTime } from "@/utils/time";

const ITEM_HEIGHT = 66;

function ConvItem({
  conv,
  active,
  onClick,
}: {
  conv: Conversation;
  active: boolean;
  onClick: () => void;
}) {
  const name = conv.contact?.display_name || t("inbox.cust.unnamed");
  return (
    <div
      className={`sc-conv-item${active ? " sc-active" : ""}`}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter") onClick();
      }}
      style={{ height: ITEM_HEIGHT }}
    >
      <Badge count={conv.agent_unread_count} size="small" overflowCount={99}>
        <Avatar size={38} src={conv.contact?.avatar_url ?? undefined} style={{ background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontWeight: 600 }}>
          {name.slice(0, 1).toUpperCase()}
        </Avatar>
      </Badge>
      <div className="sc-conv-main">
        <div className="sc-conv-top">
          <span className="sc-conv-name">{name}</span>
          {conv.needs_reply && (
            <span
              title={t("inbox.list.filter.needsReply")}
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: "var(--sc-warning)",
                flex: "none",
              }}
            />
          )}
          <span className="sc-conv-time">{listTime(conv.last_message_at ?? conv.created_at)}</span>
        </div>
        <div className="sc-conv-bottom">
          <ChannelIcon type={conv.channel_type} size={14} />
          <span className="sc-conv-snippet">{conv.snippet ?? ""}</span>
          {conv.status === "closed" && (
            <span style={{ fontSize: 11, color: "var(--sc-text-tertiary)", flex: "none" }}>
              {t("inbox.status.closed")}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export function ConversationList({
  conversations,
  isLoading,
  isFetchingNextPage,
  hasNextPage,
  onLoadMore,
  selectedId,
  onSelect,
  q,
  onSearch,
  filter,
  onFilter,
}: {
  conversations: Conversation[];
  isLoading: boolean;
  isFetchingNextPage: boolean;
  hasNextPage: boolean;
  onLoadMore: () => void;
  selectedId?: string;
  onSelect: (id: string) => void;
  q: string;
  onSearch: (q: string) => void;
  filter: InboxListFilter;
  onFilter: (f: InboxListFilter) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [listHeight, setListHeight] = useState(400);
  const [searchValue, setSearchValue] = useState(q);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setListHeight(el.clientHeight));
    ro.observe(el);
    setListHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  // debounce search input → parent
  useEffect(() => {
    const id = setTimeout(() => {
      if (searchValue !== q) onSearch(searchValue);
    }, 300);
    return () => clearTimeout(id);
  }, [searchValue, q, onSearch]);

  const filterOptions = useMemo(
    () => [
      { label: t("inbox.list.filter.all"), value: "all" },
      { label: t("inbox.list.filter.unread"), value: "unread" },
      { label: t("inbox.list.filter.needsReply"), value: "needs_reply" },
    ],
    [],
  );

  return (
    <section className="sc-inbox-list" aria-label={t("inbox.title")}>
      <div style={{ padding: "10px 12px 8px", borderBottom: "1px solid var(--sc-border)" }}>
        <Input
          allowClear
          size="middle"
          prefix={<SearchOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
          placeholder={t("inbox.list.searchPlaceholder")}
          value={searchValue}
          onChange={(e) => setSearchValue(e.target.value)}
        />
        <Segmented
          block
          size="small"
          style={{ marginTop: 8 }}
          options={filterOptions}
          value={filter}
          onChange={(v) => onFilter(v as InboxListFilter)}
        />
      </div>

      <div ref={containerRef} style={{ flex: 1, minHeight: 0 }}>
        {isLoading ? (
          <div style={{ padding: 12 }}>
            {[...Array(6)].map((_, i) => (
              <div key={i} style={{ display: "flex", gap: 10, marginBottom: 18 }}>
                <Skeleton.Avatar active size={38} />
                <Skeleton active title={{ width: "60%" }} paragraph={{ rows: 1 }} style={{ flex: 1 }} />
              </div>
            ))}
          </div>
        ) : conversations.length === 0 ? (
          <EmptyState
            icon={<CommentOutlined />}
            title={t("inbox.list.empty")}
            hint={t("inbox.list.emptyHint")}
          />
        ) : (
          <VirtualList
            data={conversations}
            height={listHeight}
            itemHeight={ITEM_HEIGHT}
            itemKey="id"
            onScroll={(e) => {
              const el = e.currentTarget;
              if (
                hasNextPage &&
                !isFetchingNextPage &&
                el.scrollHeight - el.scrollTop - el.clientHeight < 120
              ) {
                onLoadMore();
              }
            }}
          >
            {(conv: Conversation) => (
              <ConvItem conv={conv} active={conv.id === selectedId} onClick={() => onSelect(conv.id)} />
            )}
          </VirtualList>
        )}
        {isFetchingNextPage && (
          <div style={{ textAlign: "center", padding: 8 }}>
            <Spin size="small" />
          </div>
        )}
      </div>
    </section>
  );
}
