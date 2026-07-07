import dayjs from "dayjs";
import "dayjs/locale/zh-tw";
import relativeTime from "dayjs/plugin/relativeTime";
import timezone from "dayjs/plugin/timezone";
import utc from "dayjs/plugin/utc";
import { t } from "@/i18n";

dayjs.extend(relativeTime);
dayjs.extend(utc);
dayjs.extend(timezone);
dayjs.locale("zh-tw");

export { dayjs };

/** Conversation-list style timestamp: HH:mm today / 昨天 / MM-DD / YYYY-MM-DD. */
export function listTime(iso?: string | null): string {
  if (!iso) return "";
  const d = dayjs(iso);
  const now = dayjs();
  if (d.isSame(now, "day")) return d.format("HH:mm");
  if (d.isSame(now.subtract(1, "day"), "day")) return t("common.yesterday");
  if (d.isSame(now, "year")) return d.format("MM-DD");
  return d.format("YYYY-MM-DD");
}

export function fullTime(iso?: string | null): string {
  return iso ? dayjs(iso).format("YYYY-MM-DD HH:mm:ss") : "";
}

export function msgTime(iso: string): string {
  return dayjs(iso).format("HH:mm");
}

export function dateSeparator(iso: string): string {
  const d = dayjs(iso);
  const now = dayjs();
  if (d.isSame(now, "day")) return t("common.today");
  if (d.isSame(now.subtract(1, "day"), "day")) return t("common.yesterday");
  return d.format("YYYY年M月D日");
}

export function relative(iso?: string | null): string {
  return iso ? dayjs(iso).fromNow() : "";
}

export function tzNow(tz?: string | null): string {
  if (!tz) return "";
  try {
    return dayjs().tz(tz).format("HH:mm (UTCZ)");
  } catch {
    return "";
  }
}

/** minutes-from-midnight → "HH:mm" */
export function minToHHmm(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export function hhmmToMin(s: string): number {
  const [h, m] = s.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}
