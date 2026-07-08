import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { realtime } from "@/api/ws";
import type { Message, WsEnvelope } from "@/api/types";
import { t } from "@/i18n";
import { useNotificationStore } from "@/stores/notifications";
import { flashTitle, playBeep, stopTitleFlash } from "@/utils/notify";

/** Notification icon — the same inline SVG data-URI as the favicon
 *  (self-contained, no network fetch). */
const NOTIFY_ICON =
  "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%232C5CE6'/%3E%3Cpath d='M9 10.5A3.5 3.5 0 0 1 12.5 7h7A3.5 3.5 0 0 1 23 10.5v5a3.5 3.5 0 0 1-3.5 3.5H15l-4.2 3.6c-.65.56-1.8.1-1.8-.76V10.5Z' fill='white'/%3E%3Cpath d='M17.8 11.2l-3.3 4h2.2l-.7 2.6 3.3-4h-2.2l.7-2.6Z' fill='%232C5CE6'/%3E%3C/svg%3E";

/** Global new-inbound-message notifier. Mount once inside the authed shell,
 *  right after `useRealtime()`. It subscribes to the realtime event stream
 *  independently (a second `onEvent` handler alongside the cache reducer), so
 *  it fires on every page:
 *    - plays a throttled chime (respecting the sound preference),
 *    - while the tab is unfocused: counts unseen messages, flashes the tab
 *      title, and (if permitted + enabled) shows a desktop Notification whose
 *      click focuses the window and opens the conversation.
 *  Focusing the tab clears the counter and restores the title. */
export function useMessageNotifications(): void {
  const navigate = useNavigate();
  // Keep the latest navigate without re-subscribing the WS handler.
  const navRef = useRef(navigate);
  navRef.current = navigate;

  useEffect(() => {
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
      const msg = evt.payload["message"] as Message | undefined;
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

      // Everything below is for when the agent is NOT looking at the tab.
      if (isVisible()) return;

      unseen += 1;
      refreshFlash();

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
        } catch {
          /* ignore — Notification construction can throw on some platforms */
        }
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
