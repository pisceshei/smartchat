import { create } from "zustand";
import type { WsStatus } from "@/api/ws";

interface RealtimeState {
  wsStatus: WsStatus;
  /** member_id -> online */
  memberPresence: Record<string, boolean>;
  /** contact_id -> online (widget visitors) */
  visitorPresence: Record<string, boolean>;
  /** conversation_id -> typing expiry epoch-ms */
  typing: Record<string, number>;
  setWsStatus: (s: WsStatus) => void;
  setMemberPresence: (memberId: string, online: boolean) => void;
  setVisitorPresence: (contactId: string, online: boolean) => void;
  setTyping: (conversationId: string) => void;
}

export const useRealtimeStore = create<RealtimeState>((set) => ({
  wsStatus: "offline",
  memberPresence: {},
  visitorPresence: {},
  typing: {},
  setWsStatus: (wsStatus) => set({ wsStatus }),
  setMemberPresence: (memberId, online) =>
    set((s) => ({ memberPresence: { ...s.memberPresence, [memberId]: online } })),
  setVisitorPresence: (contactId, online) =>
    set((s) => ({ visitorPresence: { ...s.visitorPresence, [contactId]: online } })),
  setTyping: (conversationId) =>
    set((s) => ({ typing: { ...s.typing, [conversationId]: Date.now() + 4000 } })),
}));
