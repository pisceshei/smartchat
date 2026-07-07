/** 收件匣 — flagship 3-pane workbench (views | list | conversation+customer). */
import { CommentOutlined } from "@ant-design/icons";
import { useCallback, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import type { InboxListFilter, InboxTab } from "@/api/endpoints";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { ConversationList } from "./ConversationList";
import { ConversationView } from "./ConversationView";
import { CustomerPanel } from "./CustomerPanel";
import { useConversation, useConversations } from "./hooks";
import { ViewsSidebar } from "./ViewsSidebar";

export function InboxPage() {
  const navigate = useNavigate();
  const { conversationId } = useParams<{ conversationId: string }>();

  const [tab, setTab] = useState<InboxTab>("mine");
  const [viewId, setViewId] = useState<string | undefined>(undefined);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<InboxListFilter>("all");

  const listParams = useMemo(() => ({ tab, viewId, q, filter }), [tab, viewId, q, filter]);
  const convQuery = useConversations(listParams);

  const conversations = useMemo(
    () => (convQuery.data?.pages ?? []).flatMap((p) => p.items),
    [convQuery.data],
  );

  const selected = useConversation(conversationId);
  // prefer the freshest copy of the conversation from the list cache when the
  // detail query hasn't resolved yet (smooth selection)
  const selectedConv =
    selected.data ?? conversations.find((c) => c.id === conversationId) ?? null;

  const onSelect = useCallback(
    (id: string) => navigate(`/inbox/${id}`),
    [navigate],
  );

  return (
    <div className="sc-inbox">
      <ViewsSidebar
        tab={tab}
        viewId={viewId}
        onSelectTab={(tk) => {
          setTab(tk);
          setViewId(undefined);
        }}
        onSelectView={(vid) => setViewId(vid)}
      />

      <ConversationList
        conversations={conversations}
        isLoading={convQuery.isLoading}
        isFetchingNextPage={convQuery.isFetchingNextPage}
        hasNextPage={!!convQuery.hasNextPage}
        onLoadMore={() => void convQuery.fetchNextPage()}
        selectedId={conversationId}
        onSelect={onSelect}
        q={q}
        onSearch={setQ}
        filter={filter}
        onFilter={setFilter}
      />

      {selectedConv ? (
        <>
          <ConversationView key={selectedConv.id} conversation={selectedConv} />
          <CustomerPanel key={`p-${selectedConv.id}`} conversation={selectedConv} />
        </>
      ) : (
        <div className="sc-conv-pane" style={{ alignItems: "center", justifyContent: "center" }}>
          <EmptyState
            icon={<CommentOutlined />}
            title={t("inbox.conv.selectOne")}
            hint={t("inbox.conv.selectHint")}
          />
        </div>
      )}
    </div>
  );
}
