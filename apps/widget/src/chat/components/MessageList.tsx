import { useEffect, useRef } from "preact/hooks";

import type { WidgetBootstrap } from "../../shared/config";
import { retryMessage } from "../controller";
import { formatTime, t } from "../i18n";
import type { UiMessage } from "../store";
import { Block, isBare } from "./Blocks";

function isSystem(m: UiMessage): boolean {
  return (
    m.sender_type === "system" ||
    m.content.blocks.every((b) => b.kind === "system_event")
  );
}

function Row(props: {
  msg: UiMessage;
  config: WidgetBootstrap;
  quickAnswered: boolean;
}) {
  const { msg, config, quickAnswered } = props;
  if (isSystem(msg)) {
    return (
      <div class="sc-row-system">
        {msg.content.blocks.map((b) => (
          <Block block={b} messageId={msg.id} quickAnswered={quickAnswered} />
        ))}
      </div>
    );
  }
  const mine = msg.sender_type === "contact";
  const bare = msg.content.blocks.length === 1 && isBare(msg.content.blocks[0]);
  return (
    <div class={"sc-row " + (mine ? "mine" : "theirs")}>
      {!mine ? (
        msg.sender_avatar_url ? (
          <img class="sc-msg-avatar" src={msg.sender_avatar_url} alt="" />
        ) : (
          <div class="sc-msg-avatar sc-avatar-fallback">
            {(msg.sender_name || config.brand?.name || "S").slice(0, 1).toUpperCase()}
          </div>
        )
      ) : null}
      <div class="sc-msg-col">
        <div class={"sc-bubble" + (bare ? " bare" : "")}>
          {msg.content.blocks.map((b) => (
            <Block block={b} messageId={msg.id} quickAnswered={quickAnswered} />
          ))}
        </div>
        <div class="sc-meta">
          {msg.local_state === "failed" ? (
            <button type="button" class="sc-retry" onClick={() => retryMessage(msg.id)}>
              {t("failed")} · {t("retry")}
            </button>
          ) : msg.local_state === "pending" ? (
            <span class="sc-pending">…</span>
          ) : (
            <span>{formatTime(msg.created_at)}</span>
          )}
        </div>
      </div>
    </div>
  );
}

export function MessageList(props: {
  messages: UiMessage[];
  config: WidgetBootstrap;
  agentTyping: boolean;
  answeredQuickBlocks: string[];
  offlineBanner: preact.ComponentChildren;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [props.messages, props.agentTyping]);

  return (
    <div class="sc-list" ref={ref} onScroll={onScroll}>
      {props.offlineBanner}
      {props.messages.map((m) => (
        <Row
          key={m.id}
          msg={m}
          config={props.config}
          quickAnswered={props.answeredQuickBlocks.indexOf(m.id) >= 0}
        />
      ))}
      {props.agentTyping ? (
        <div class="sc-row theirs">
          <div class="sc-msg-avatar sc-avatar-fallback">
            {(props.config.brand?.name || "S").slice(0, 1).toUpperCase()}
          </div>
          <div class="sc-msg-col">
            <div class="sc-bubble sc-typing" aria-label={t("typing")}>
              <span class="sc-tdot" />
              <span class="sc-tdot" />
              <span class="sc-tdot" />
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
