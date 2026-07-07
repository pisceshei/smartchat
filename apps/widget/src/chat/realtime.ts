/**
 * Realtime event delivery: WebSocket to /ws/widget with seq/resume, falling
 * back to REST long-polling (GET /widget/events?cursor=) after repeated WS
 * failures. Uplink is REST-only (messages) — the socket carries only
 * downstream events plus lightweight typing/heartbeat frames.
 *
 * Wire frames (server → client):
 *   {"type":"hello","seq":N}
 *   {"type":"event","seq":N,"event":{"type":"message.created","payload":{...}}}
 *   {"type":"resync_required"}
 *   {"type":"pong"}
 * Client → server: {"type":"ping"} | {"type":"typing"}
 */
import type { WidgetApi, WidgetEvent } from "./api";

export interface RealtimeOpts {
  wsBase: string;
  api: WidgetApi;
  onEvent: (seq: number, event: WidgetEvent) => void;
  onState: (state: "connecting" | "online" | "reconnecting") => void;
  /** history got out of range — caller should refetch via REST */
  onResync: () => void;
}

const PING_INTERVAL = 25_000;
const STALE_AFTER = 70_000;
const WS_RETRY_BASE = 1_000;
const WS_RETRY_MAX = 30_000;
const WS_FAILS_BEFORE_POLL = 3;
const WS_REATTEMPT_AFTER = 120_000; // while long-polling, retry WS this often

export class Realtime {
  private seq = 0;
  private ws: WebSocket | null = null;
  private stopped = true;
  private wsFails = 0;
  private mode: "ws" | "poll" = "ws";
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFrameAt = 0;
  private pollAbort: AbortController | null = null;
  private pollBackoff = 1_000;

  constructor(private opts: RealtimeOpts) {}

  start(fromSeq: number): void {
    this.seq = fromSeq;
    this.stopped = false;
    this.wsFails = 0;
    this.mode = typeof WebSocket === "undefined" ? "poll" : "ws";
    this.opts.onState("connecting");
    if (this.mode === "ws") this.connectWs();
    else this.pollLoop();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* already closed */
      }
      this.ws = null;
    }
    if (this.pollAbort) this.pollAbort.abort();
  }

  /** Advance the resume cursor (e.g. after a REST send returns a seq). */
  advance(seq: number | null | undefined): void {
    if (typeof seq === "number" && seq > this.seq) this.seq = seq;
  }

  sendTyping(): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "typing" }));
    }
  }

  // ---- WebSocket path ------------------------------------------------------
  private connectWs(): void {
    if (this.stopped) return;
    const token = this.opts.api.token || "";
    const url =
      this.opts.wsBase +
      "/ws/widget?token=" +
      encodeURIComponent(token) +
      "&resume_from=" +
      this.seq;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      this.onWsDown();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.wsFails = 0;
      this.lastFrameAt = Date.now();
      this.opts.onState("online");
      this.pingTimer = setInterval(() => {
        if (Date.now() - this.lastFrameAt > STALE_AFTER) {
          try {
            ws.close();
          } catch {
            /* noop */
          }
          return;
        }
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, PING_INTERVAL);
    };

    ws.onmessage = (ev: MessageEvent) => {
      this.lastFrameAt = Date.now();
      let frame: {
        type?: string;
        seq?: number;
        event?: WidgetEvent;
      };
      try {
        frame = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      if (frame.type === "event" && typeof frame.seq === "number" && frame.event) {
        if (frame.seq <= this.seq) return; // duplicate on resume — drop
        this.seq = frame.seq;
        this.opts.onEvent(frame.seq, frame.event);
      } else if (frame.type === "resync_required") {
        this.opts.onResync();
      } else if (frame.type === "hello" && typeof frame.seq === "number") {
        if (frame.seq > this.seq) this.seq = frame.seq;
      }
    };

    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      this.clearTimers();
      this.onWsDown();
    };
    ws.onerror = () => {
      try {
        ws.close();
      } catch {
        /* noop */
      }
    };
  }

  private onWsDown(): void {
    if (this.stopped) return;
    this.wsFails += 1;
    this.opts.onState("reconnecting");
    if (this.wsFails >= WS_FAILS_BEFORE_POLL) {
      this.mode = "poll";
      this.pollLoop();
      // keep probing WS in the background so we escape long-poll eventually
      this.retryTimer = setTimeout(() => {
        if (this.stopped || this.mode === "ws") return;
        this.mode = "ws";
        this.wsFails = WS_FAILS_BEFORE_POLL - 1; // one strike before poll again
        if (this.pollAbort) this.pollAbort.abort();
        this.connectWs();
      }, WS_REATTEMPT_AFTER);
    } else {
      const delay = Math.min(WS_RETRY_BASE * 2 ** (this.wsFails - 1), WS_RETRY_MAX);
      this.retryTimer = setTimeout(() => this.connectWs(), delay);
    }
  }

  // ---- long-poll path --------------------------------------------------------
  private async pollLoop(): Promise<void> {
    while (!this.stopped && this.mode === "poll") {
      this.pollAbort = new AbortController();
      try {
        const res = await this.opts.api.events(this.seq, 25, this.pollAbort.signal);
        this.pollBackoff = 1_000;
        this.opts.onState("online");
        for (const item of res.events) {
          if (item.seq <= this.seq) continue;
          this.seq = item.seq;
          this.opts.onEvent(item.seq, item.event);
        }
        if (typeof res.cursor === "number" && res.cursor > this.seq) {
          this.seq = res.cursor;
        }
      } catch (e) {
        if (this.stopped || this.mode !== "poll") return;
        if ((e as Error).name === "AbortError") return;
        this.opts.onState("reconnecting");
        await sleep(this.pollBackoff);
        this.pollBackoff = Math.min(this.pollBackoff * 2, 30_000);
      }
    }
  }

  private clearTimers(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
