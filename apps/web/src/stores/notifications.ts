import { create } from "zustand";
import { persist } from "zustand/middleware";

/** Agent-facing new-message notification preferences, persisted to
 *  localStorage. Sound and desktop notifications both default ON — the real
 *  gate for popups is the browser Notification permission (requested via a
 *  one-time in-app prompt); the bell popover stays the manual opt-out. */
interface NotificationState {
  soundEnabled: boolean;
  desktopEnabled: boolean;
  setSoundEnabled: (v: boolean) => void;
  setDesktopEnabled: (v: boolean) => void;
}

export const useNotificationStore = create<NotificationState>()(
  persist(
    (set) => ({
      soundEnabled: true,
      desktopEnabled: true,
      setSoundEnabled: (soundEnabled) => set({ soundEnabled }),
      setDesktopEnabled: (desktopEnabled) => set({ desktopEnabled }),
    }),
    {
      name: "smartchat.notifications",
      version: 1,
      // v0 shipped desktopEnabled:false as the DEFAULT (not a user choice) —
      // flip it on once; the permission gate keeps it inert until granted.
      migrate: (state, version) => {
        const s = (state ?? {}) as Partial<NotificationState>;
        if (version < 1) s.desktopEnabled = true;
        return s as NotificationState;
      },
    },
  ),
);
