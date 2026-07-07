/** Composer: 回覆/備註 tabs, quick-reply "/" picker, emoji, attachments,
 *  Enter-send / Shift+Enter-newline, optimistic send. */
import {
  PaperClipOutlined,
  SendOutlined,
  SmileOutlined,
  StarFilled,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { App, Button, Input, Popover, Tabs, Tag, Upload } from "antd";
import type { InputRef } from "antd";
import { useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { filesApi, quickRepliesApi } from "@/api/endpoints";
import type { ContentBlock, Conversation, FileRef, QuickReply } from "@/api/types";
import { realtime } from "@/api/ws";
import { t } from "@/i18n";
import { newClientId, useSendMessage } from "./hooks";

const EMOJIS = [
  "😀","😄","😊","🙂","😉","😍","🤩","😘","😜","🤔",
  "😐","😅","😂","🥲","😭","😤","😡","🙏","👍","👎",
  "👌","🤝","👏","💪","🎉","🎁","✨","🔥","💯","❤️",
  "💔","⭐","⚡","☀️","🌙","🍀","🌹","🛒","💰","📦",
];

function QuickReplyList({
  search,
  onPick,
}: {
  search: string;
  onPick: (qr: QuickReply) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["quick-replies", "all"],
    queryFn: () => quickRepliesApi.list(),
    staleTime: 30_000,
    retry: 1,
  });

  const filtered = useMemo(() => {
    const list = data ?? [];
    const q = search.trim().toLowerCase();
    const hit = q
      ? list.filter(
          (r) =>
            r.title.toLowerCase().includes(q) ||
            r.content.toLowerCase().includes(q) ||
            (r.shortcut ?? "").toLowerCase().includes(q),
        )
      : list;
    return [...hit].sort((a, b) => Number(b.starred) - Number(a.starred));
  }, [data, search]);

  if (isLoading) {
    return <div style={{ padding: 12, color: "var(--sc-text-tertiary)" }}>{t("common.loading")}</div>;
  }
  if (filtered.length === 0) {
    return (
      <div style={{ padding: 12, color: "var(--sc-text-tertiary)" }}>
        {t("inbox.composer.noQuickReply")}
      </div>
    );
  }
  return (
    <div style={{ maxHeight: 260, overflowY: "auto", width: 320 }}>
      {filtered.map((qr) => (
        <div
          key={qr.id}
          className="sc-clickable"
          style={{ padding: "8px 12px", borderBottom: "1px solid var(--sc-border)" }}
          onClick={() => onPick(qr)}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {qr.starred && <StarFilled style={{ color: "var(--sc-warning)", fontSize: 12 }} />}
            <span style={{ fontWeight: 600, fontSize: 13 }}>{qr.title}</span>
            <Tag style={{ marginLeft: "auto", fontSize: 10 }}>
              {qr.visibility === "public" ? t("qr.public") : t("qr.personal")}
            </Tag>
          </div>
          <div
            style={{
              fontSize: 12.5,
              color: "var(--sc-text-secondary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {qr.content}
          </div>
        </div>
      ))}
    </div>
  );
}

export function Composer({ conversation }: { conversation: Conversation }) {
  const { message } = App.useApp();
  const [mode, setMode] = useState<"reply" | "note">("reply");
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<FileRef[]>([]);
  const [qrOpen, setQrOpen] = useState(false);
  const [emojiOpen, setEmojiOpen] = useState(false);
  const inputRef = useRef<InputRef>(null);
  const lastTypingSent = useRef(0);
  const send = useSendMessage(conversation.id);

  const isNote = mode === "note";
  const slashActive = text.startsWith("/");
  const slashSearch = slashActive ? text.slice(1) : "";

  const doSend = () => {
    const trimmed = text.trim();
    if (!trimmed && attachments.length === 0) return;
    const blocks: ContentBlock[] = [];
    for (const f of attachments) {
      blocks.push({
        kind: "media",
        media_type: f.mime.startsWith("image/")
          ? "image"
          : f.mime.startsWith("video/")
            ? "video"
            : f.mime.startsWith("audio/")
              ? "audio"
              : "file",
        file_id: f.id,
        url: f.url,
        mime: f.mime,
        size: f.size,
        file_name: f.file_name,
      });
    }
    if (trimmed) blocks.push({ kind: "text", text: trimmed });
    send.mutate({ content: { blocks }, isNote, clientMsgId: newClientId() });
    setText("");
    setAttachments([]);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!slashActive) doSend();
    }
    if (e.key === "Escape") {
      setQrOpen(false);
    }
  };

  const onChange = (v: string) => {
    setText(v);
    if (v.startsWith("/")) setQrOpen(true);
    else if (qrOpen && !v.startsWith("/")) setQrOpen(false);
    // throttled typing signal (only for replies)
    const now = Date.now();
    if (!isNote && now - lastTypingSent.current > 3000) {
      lastTypingSent.current = now;
      realtime.sendTyping(conversation.id);
    }
  };

  const pickQuickReply = (qr: QuickReply) => {
    setText(qr.content);
    setQrOpen(false);
    inputRef.current?.focus();
  };

  const uploadFile = async (file: File) => {
    try {
      const ref = await filesApi.upload(file);
      setAttachments((prev) => [...prev, ref]);
    } catch {
      message.error(t("inbox.composer.uploadFailed"));
    }
  };

  return (
    <div className={`sc-composer${isNote ? " sc-note-mode" : ""}`}>
      <Tabs
        size="small"
        activeKey={mode}
        onChange={(k) => setMode(k as "reply" | "note")}
        items={[
          { key: "reply", label: t("inbox.composer.reply") },
          { key: "note", label: t("inbox.composer.note") },
        ]}
        tabBarStyle={{ margin: 0, padding: "0 12px" }}
      />

      {attachments.length > 0 && (
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "8px 12px 0" }}>
          {attachments.map((f) => (
            <Tag
              key={f.id}
              closable
              icon={<PaperClipOutlined />}
              onClose={() => setAttachments((prev) => prev.filter((x) => x.id !== f.id))}
            >
              {f.file_name}
            </Tag>
          ))}
        </div>
      )}

      <Popover
        open={qrOpen && slashActive}
        placement="topLeft"
        arrow={false}
        content={<QuickReplyList search={slashSearch} onPick={pickQuickReply} />}
      >
        <Input.TextArea
          ref={inputRef}
          value={text}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={isNote ? t("inbox.composer.notePlaceholder") : t("inbox.composer.placeholder")}
          autoSize={{ minRows: 2, maxRows: 8 }}
          variant="borderless"
        />
      </Popover>

      <div className="sc-composer-bar">
        <Popover
          open={emojiOpen}
          onOpenChange={setEmojiOpen}
          trigger="click"
          content={
            <div style={{ display: "grid", gridTemplateColumns: "repeat(10, 28px)", gap: 2 }}>
              {EMOJIS.map((e) => (
                <button
                  key={e}
                  type="button"
                  style={{
                    border: "none",
                    background: "none",
                    fontSize: 18,
                    cursor: "pointer",
                    borderRadius: 4,
                    padding: 2,
                  }}
                  onClick={() => {
                    setText((v) => v + e);
                    setEmojiOpen(false);
                    inputRef.current?.focus();
                  }}
                >
                  {e}
                </button>
              ))}
            </div>
          }
        >
          <Button type="text" size="small" icon={<SmileOutlined />} aria-label={t("inbox.composer.emoji")} />
        </Popover>

        <Upload
          showUploadList={false}
          multiple
          customRequest={({ file, onSuccess }) => {
            void uploadFile(file as File).then(() => onSuccess?.(null));
          }}
        >
          <Button type="text" size="small" icon={<PaperClipOutlined />} aria-label={t("inbox.composer.attach")} />
        </Upload>

        <Popover
          trigger="click"
          placement="topLeft"
          content={<QuickReplyList search="" onPick={pickQuickReply} />}
        >
          <Button
            type="text"
            size="small"
            icon={<ThunderboltOutlined />}
            aria-label={t("inbox.composer.quickReply")}
          />
        </Popover>

        <span className="sc-composer-hint">
          <span>{t("inbox.composer.enterHint")}</span>
          <Button
            type="primary"
            size="small"
            icon={<SendOutlined />}
            onClick={doSend}
            loading={send.isPending}
            disabled={!text.trim() && attachments.length === 0}
          >
            {isNote ? t("inbox.composer.sendNote") : t("inbox.composer.send")}
          </Button>
        </span>
      </div>
    </div>
  );
}
