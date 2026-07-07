/** Realtime WebSocket client implementing the plan's delivery protocol:
 *  - per-workspace seq, reconnect with resume_from replay
 *  - `resync_required` → full REST refetch signal
 *  - event-id LRU dedupe (at-least-once safe)
 *  - heartbeat ping + exponential backoff reconnect
 *  Uplink NEVER goes through the socket (REST POST only); we only send
 *  lightweight ping/typing frames. */
import { wsUrl } from "./client";
import type { WsEnvelope } from "./types";

export type WsStatus = "connecting" | "online" | "offline";

type EventHandler = (evt: WsEnvelope) => void;
type StatusHandler = (status: WsStatus) => void;
type ResyncHandler = () => void;

const HEARTBEAT_MS = 25_000;
const MAX_BACKOFF_MS = 30_000;
const DEDUPE_CAP = 2048;

export class RealtimeClient {
  private ws: WebSocket | null = null;
  private lastSeq = 0;
  private seen = new Set<string>();
  private seenQueue: string[] = [];
  private retries = 0;
  private stopped = true;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  private eventHandlers = new Set<EventHandler>();
  private statusHandlers = new Set<StatusHandler>();
  private resyncHandlers = new Set<ResyncHandler>();

  private token = "";
  private workspaceId = "";

  start(token: string, workspaceId: string): void {
    if (!this.stopped && this.token === token && this.workspaceId === workspaceId) return;
    this.stop();
    this.token = token;
    this.workspaceId = workspaceId;
    this.stopped = false;
    this.lastSeq = 0;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.clearHeartbeat();
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
  }

  onEvent(fn: EventHandler): () => void {
    this.eventHandlers.add(fn);
    return () => this.eventHandlers.delete(fn);
  }

  onStatus(fn: StatusHandler): () => void {
    this.statusHandlers.add(fn);
    return () => this.statusHandlers.delete(fn);
  }

  onResync(fn: ResyncHandler): () => void {
    this.resyncHandlers.add(fn);
    return () => this.resyncHandlers.delete(fn);
  }

  /** Lightweight typing indicator (throttled by callers). */
  sendTyping(conversationId: string): void {
    this.safeSend({ type: "typing", conversation_id: conversationId });
  }

  /* ------------------------------------------------------------ private */

  private open(): void {
    this.emitStatus("connecting");
    const url = wsUrl({
      token: this.token,
      workspace_id: this.workspaceId,
      ...(this.lastSeq > 0 ? { resume_from: this.lastSeq } : {}),
    });
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.retries = 0;
      this.emitStatus("online");
      this.clearHeartbeat();
      this.heartbeat = setInterval(() => this.safeSend({ type: "ping" }), HEARTBEAT_MS);
    };

    ws.onmessage = (raw) => {
      let evt: WsEnvelope & { type: string };
      try {
        evt = JSON.parse(String(raw.data)) as WsEnvelope;
      } catch {
        return;
      }
      if (evt.type === "pong" || evt.type === "ping") return;
      if (evt.type === "resync_required") {
        this.lastSeq = 0;
        this.resyncHandlers.forEach((fn) => fn());
        return;
      }
      if (typeof evt.seq === "number" && evt.seq > this.lastSeq) this.lastSeq = evt.seq;
      if (evt.id) {
        if (this.seen.has(evt.id)) return;
        this.seen.add(evt.id);
        this.seenQueue.push(evt.id);
        if (this.seenQueue.length > DEDUPE_CAP) {
          const drop = this.seenQueue.shift();
          if (drop) this.seen.delete(drop);
        }
      }
      this.eventHandlers.forEach((fn) => fn(evt));
    };

    ws.onclose = () => {
      this.clearHeartbeat();
      this.ws = null;
      if (!this.stopped) {
        this.emitStatus("offline");
        this.scheduleReconnect();
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer) return;
    const backoff = Math.min(MAX_BACKOFF_MS, 1000 * 2 ** this.retries);
    const jitter = backoff * (0.75 + Math.random() * 0.5);
    this.retries += 1;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.open();
    }, jitter);
  }

  private clearHeartbeat(): void {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
  }

  private safeSend(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  private emitStatus(status: WsStatus): void {
    this.statusHandlers.forEach((fn) => fn(status));
  }
}

/** App-wide singleton. */
export const realtime = new RealtimeClient();
