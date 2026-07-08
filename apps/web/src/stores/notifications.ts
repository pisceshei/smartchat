import { create } from "zustand";
import { persist } from "zustand/middleware";

/** Agent-facing new-message notification preferences, persisted to
 *  localStorage. Sound defaults ON; desktop notifications default OFF until the
 *  agent opts in (which also triggers the browser permission prompt). */
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
      desktopEnabled: false,
      setSoundEnabled: (soundEnabled) => set({ soundEnabled }),
      setDesktopEnabled: (desktopEnabled) => set({ desktopEnabled }),
    }),
    { name: "smartchat.notifications" },
  ),
);
