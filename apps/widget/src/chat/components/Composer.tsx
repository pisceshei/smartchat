import { useRef, useState } from "preact/hooks";

import { notifyTyping, sendFile, sendText } from "../controller";
import { t } from "../i18n";

const EMOJI = [
  "😀", "😄", "😊", "🙂", "😉", "😍", "😘", "🤔",
  "😅", "😂", "🥲", "😭", "😡", "👍", "👎", "🙏",
  "👌", "💪", "🎉", "❤️", "💯", "🔥", "⭐", "✅",
];

export function Composer(props: {
  disabled: boolean;
  allowUpload: boolean;
  allowEmoji: boolean;
  error: string | null;
}) {
  const [text, setText] = useState("");
  const [emojiOpen, setEmojiOpen] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const doSend = () => {
    if (!text.trim()) return;
    sendText(text);
    setText("");
    if (taRef.current) {
      taRef.current.style.height = "auto";
      taRef.current.focus();
    }
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      doSend();
    }
  };

  const onInput = (e: Event) => {
    const ta = e.currentTarget as HTMLTextAreaElement;
    setText(ta.value);
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
    notifyTyping();
  };

  const pickEmoji = (emoji: string) => {
    setText((v) => v + emoji);
    setEmojiOpen(false);
    taRef.current?.focus();
  };

  const onFile = (e: Event) => {
    const input = e.currentTarget as HTMLInputElement;
    const file = input.files && input.files[0];
    if (file) void sendFile(file);
    input.value = "";
  };

  return (
    <div class="sc-composer-wrap">
      {props.error ? <div class="sc-composer-error">{props.error}</div> : null}
      {emojiOpen ? (
        <div class="sc-emoji-pop">
          {EMOJI.map((em) => (
            <button type="button" class="sc-emoji" onClick={() => pickEmoji(em)}>
              {em}
            </button>
          ))}
        </div>
      ) : null}
      <div class="sc-composer">
        {props.allowEmoji ? (
          <button
            type="button"
            class="sc-icon-btn subtle"
            aria-label={t("emoji")}
            disabled={props.disabled}
            onClick={() => setEmojiOpen((v) => !v)}
          >
            <svg viewBox="0 0 24 24" width="21" height="21" fill="none">
              <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7" />
              <circle cx="9" cy="10" r="1.2" fill="currentColor" />
              <circle cx="15" cy="10" r="1.2" fill="currentColor" />
              <path
                d="M8.5 14.5c.9 1.1 2.1 1.7 3.5 1.7s2.6-.6 3.5-1.7"
                stroke="currentColor"
                stroke-width="1.7"
                stroke-linecap="round"
              />
            </svg>
          </button>
        ) : null}
        {props.allowUpload ? (
          <button
            type="button"
            class="sc-icon-btn subtle"
            aria-label={t("attach")}
            disabled={props.disabled}
            onClick={() => fileRef.current?.click()}
          >
            <svg viewBox="0 0 24 24" width="21" height="21" fill="none">
              <path
                d="M20 11.5 12.2 19.3a5 5 0 0 1-7-7L13 4.4a3.4 3.4 0 0 1 4.8 4.8l-7.6 7.6a1.8 1.8 0 0 1-2.5-2.5l7-7"
                stroke="currentColor"
                stroke-width="1.7"
                stroke-linecap="round"
              />
            </svg>
          </button>
        ) : null}
        <input type="file" ref={fileRef} hidden onChange={onFile} />
        <textarea
          ref={taRef}
          class="sc-input"
          rows={1}
          placeholder={t("composer_placeholder")}
          value={text}
          disabled={props.disabled}
          onKeyDown={onKeyDown}
          onInput={onInput}
        />
        <button
          type="button"
          class="sc-send"
          aria-label={t("send")}
          disabled={props.disabled || !text.trim()}
          onClick={doSend}
        >
          <svg viewBox="0 0 24 24" width="19" height="19">
            <path d="M3 11.5 21 3l-6.5 18-3.2-7.3L3 11.5Z" fill="currentColor" />
          </svg>
        </button>
      </div>
    </div>
  );
}
