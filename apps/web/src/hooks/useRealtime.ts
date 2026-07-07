import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { realtime } from "@/api/ws";
import { applyEvent } from "@/realtime/applyEvent";
import { useAuthStore } from "@/stores/auth";
import { useRealtimeStore } from "@/stores/realtime";

/** Mount once inside the authed shell: connects the WS, applies events to
 *  react-query caches, tracks connection status, full-resync on demand. */
export function useRealtime(): void {
  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const setWsStatus = useRealtimeStore((s) => s.setWsStatus);

  useEffect(() => {
    if (!token || !workspaceId) return;
    realtime.start(token, workspaceId);
    const offEvent = realtime.onEvent((evt) => applyEvent(qc, evt));
    const offStatus = realtime.onStatus(setWsStatus);
    const offResync = realtime.onResync(() => {
      void qc.invalidateQueries();
    });
    return () => {
      offEvent();
      offStatus();
      offResync();
      realtime.stop();
    };
  }, [token, workspaceId, qc, setWsStatus]);
}
