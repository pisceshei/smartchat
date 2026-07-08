/** Self-contained browser-notification helpers — no external assets, no deps:
 *   - a WebAudio two-tone chime (throttled) for new inbound messages,
 *   - a document.title flasher for unseen messages while the tab is unfocused,
 *   - a lazy Notification-permission requester.
 *  Audio playback and the permission prompt both require a prior user gesture,
 *  so `primeAudio` / `ensureNotificationPermission` are called from a click. */

type WebkitWindow = Window & { webkitAudioContext?: typeof AudioContext };

let audioCtx: AudioContext | null = null;
let lastBeepAt = 0;
const BEEP_THROTTLE_MS = 1500;

function getAudioCtx(): AudioContext | null {
  const Ctor = window.AudioContext ?? (window as WebkitWindow).webkitAudioContext;
  if (!Ctor) return null;
  audioCtx ??= new Ctor();
  return audioCtx;
}

/** Unlock/resume the AudioContext from a user gesture so later `playBeep`
 *  calls aren't blocked by the browser autoplay policy. */
export function primeAudio(): void {
  try {
    const ctx = getAudioCtx();
    if (ctx && ctx.state === "suspended") void ctx.resume();
  } catch {
    /* ignore — audio simply won't play */
  }
}

/** A short descending two-tone beep. Throttled so a burst of inbound messages
 *  doesn't machine-gun the speaker. */
export function playBeep(): void {
  const now = Date.now();
  if (now - lastBeepAt < BEEP_THROTTLE_MS) return;
  lastBeepAt = now;
  try {
    const ctx = getAudioCtx();
    if (!ctx) return;
    if (ctx.state === "suspended") void ctx.resume();
    const t0 = ctx.currentTime;
    [880, 660].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const start = t0 + i * 0.16;
      const end = start + 0.15;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.2, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, end);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(end + 0.02);
    });
  } catch {
    /* ignore */
  }
}

/* -------------------------------------------------------- title flasher */

let flashTimer: ReturnType<typeof setInterval> | null = null;
let baseTitle = "";
let altTitle = "";
let showingAlt = false;

/** Start (or update) flashing the tab title between the original title and
 *  `alt`. Safe to call repeatedly — later calls just refresh the alt text. */
export function flashTitle(alt: string): void {
  altTitle = alt;
  if (flashTimer) return;
  baseTitle = document.title; // captured once, before we start mutating it
  showingAlt = false;
  flashTimer = setInterval(() => {
    showingAlt = !showingAlt;
    document.title = showingAlt ? altTitle : baseTitle;
  }, 1000);
}

/** Stop flashing and restore the original tab title. */
export function stopTitleFlash(): void {
  if (flashTimer) {
    clearInterval(flashTimer);
    flashTimer = null;
  }
  showingAlt = false;
  if (baseTitle) document.title = baseTitle;
}

/* -------------------------------------------------- notification permission */

/** Lazily request Notification permission. Returns the resulting permission
 *  ("granted" / "denied" / "default"); never throws. */
export async function ensureNotificationPermission(): Promise<NotificationPermission> {
  if (typeof Notification === "undefined") return "denied";
  if (Notification.permission !== "default") return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}
