import { Button, notification as antdNotification } from "antd";
import { createElement, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { realtime } from "@/api/ws";
import type { Message, WsEnvelope } from "@/api/types";
import { t } from "@/i18n";
import { useNotificationStore } from "@/stores/notifications";
import {
  ensureNotificationPermission,
  flashTitle,
  playBeep,
  primeAudio,
  stopTitleFlash,
} from "@/utils/notify";

/** Notification icon — the same inline SVG data-URI as the favicon
 *  (self-contained, no network fetch). */
const NOTIFY_ICON =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%232C5CE6'/%3E%3Cpath d='M9 10.5A3.5 3.5 0 0 1 12.5 7h7A3.5 3.5 0 0 1 23 10.5v5a3.5 3.5 0 0 1-3.5 3.5H15l-4.2 3.6c-.65.56-1.8.1-1.8-.76V10.5Z' fill='white'/%3E%3Cpath d='M17.8 11.2l-3.3 4h2.2l-.7 2.6 3.3-4h-2.2l.7-2.6Z' fill='%232C5CE6'/%3E%3C/svg%3E";

const PROMPT_DISMISS_KEY = "smartchat.notify.promptDismissed";

/** One-time friendly banner asking for Notification permission. The button
 *  click is the user gesture Chrome requires for requestPermission(); it also
 *  primes the WebAudio context so the chime can play later. */
function maybePromptForPermission(): void {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "default") return;
  if (localStorage.getItem(PROMPT_DISMISS_KEY)) return;
  const key = "notify-permission-prompt";
  antdNotification.open({
    key,
    message: t("notify.prompt.title"),
    description: t("notify.prompt.desc"),
    duration: 0,
    onClose: () => localStorage.setItem(PROMPT_DISMISS_KEY, "1"),
    btn: createElement(
      Button,
      {
        type: "primary",
        size: "small",
        onClick: () => {
          primeAudio();
          void ensureNotificationPermission().then((perm) => {
            if (perm === "granted") {
              useNotificationStore.getState().setDesktopEnabled(true);
            }
          });
          localStorage.setItem(PROMPT_DISMISS_KEY, "1");
          antdNotification.destroy(key);
        },
      },
      t("notify.prompt.enable"),
    ),
  });
}

/** Best-effort message extraction: prefer the nested full row, fall back to
 *  the flat event fields (outbound events and older publishers are flat). */
function messageFrom(evt: WsEnvelope): Message | undefined {
  const payload = evt.payload ?? {};
  const nested = payload["message"] as Message | undefined;
  if (nested?.id) return nested;
  const id = payload["message_id"] as string | undefined;
  const conversationId =
    (payload["conversation_id"] as string | undefined) ?? evt.conversation_id ?? undefined;
  if (!id || !conversationId) return undefined;
  return {
    id,
    conversation_id: conversationId,
    direction: (payload["direction"] as string) ?? "",
    sender_type: (payload["sender_type"] as string) ?? "",
    msg_type: (payload["msg_type"] as string) ?? "text",
    text_plain: (payload["text_plain"] as string | null) ?? null,
    is_note: payload["is_note"] === true,
  } as unknown as Message;
}

/** Global new-inbound-message notifier. Mount once inside the authed shell,
 *  right after `useRealtime()`. It subscribes to the realtime event stream
 *  independently (a second `onEvent` handler alongside the cache reducer), so
 *  it fires on every page:
 *    - plays a throttled chime (respecting the sound preference),
 *    - shows a desktop Notification (if permitted + enabled) whenever the
 *      agent is not actively viewing that conversation — tab hidden, window
 *      unfocused, or a different page/conversation open,
 *    - while the tab is hidden it also counts unseen messages and flashes the
 *      tab title.
 *  Focusing the tab clears the counter and restores the title. */
export function useMessageNotifications(): void {
  const navigate = useNavigate();
  // Keep the latest navigate without re-subscribing the WS handler.
  const navRef = useRef(navigate);
  navRef.current = navigate;

  useEffect(() => {
    maybePromptForPermission();

    // Dedupe by message id (belt-and-suspenders; the WS client already dedupes
    // by envelope id). Bounded FIFO so it can't grow without limit.
    const seen = new Set<string>();
    const seenQueue: string[] = [];
    let unseen = 0;

    const isVisible = () => document.visibilityState === "visible";

    const refreshFlash = () => {
      if (unseen > 0 && !isVisible()) {
        flashTitle(`(${unseen}) ${t("notify.titleFlash")}`);
      } else {
        stopTitleFlash();
      }
    };

    const clearUnseen = () => {
      unseen = 0;
      stopTitleFlash();
    };

    const onVisible = () => {
      if (isVisible()) clearUnseen();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);

    const off = realtime.onEvent((evt: WsEnvelope) => {
      if (evt.type !== "message.created") return;
      const msg = messageFrom(evt);
      if (!msg) return;
      // Inbound from a contact only — excludes our own outbound + AI/bot/flow
      // replies (all direction "out") and internal notes.
      if (msg.direction !== "in" || msg.is_note) return;
      if (seen.has(msg.id)) return;
      seen.add(msg.id);
      seenQueue.push(msg.id);
      if (seenQueue.length > 512) {
        const drop = seenQueue.shift();
        if (drop) seen.delete(drop);
      }

      const { soundEnabled, desktopEnabled } = useNotificationStore.getState();

      if (soundEnabled) playBeep();

      const tabHidden = !isVisible() || !document.hasFocus();
      const viewingThisConversation =
        !tabHidden && window.location.pathname === `/inbox/${msg.conversation_id}`;
      // Actively reading this very conversation → no popup needed.
      if (viewingThisConversation) return;

      if (!isVisible()) {
        unseen += 1;
        refreshFlash();
      }

      if (
        desktopEnabled &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted"
      ) {
        try {
          const title = msg.sender_name || t("inbox.cust.unnamed");
          const n = new Notification(title, {
            body: msg.text_plain || t("notify.newMessage"),
            tag: msg.conversation_id,
            icon: NOTIFY_ICON,
          });
          n.onclick = () => {
            window.focus();
            navRef.current(`/inbox/${msg.conversation_id}`);
            clearUnseen();
            n.close();
          };
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn("desktop notification failed", e);
        }
      } else if (desktopEnabled) {
        maybePromptForPermission();
      }
    });

    return () => {
      off();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      stopTitleFlash();
    };
  }, []);
}
