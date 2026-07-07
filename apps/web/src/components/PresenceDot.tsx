import { t } from "@/i18n";

export function PresenceDot({ online, showLabel = false }: { online: boolean; showLabel?: boolean }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span className={`sc-presence-dot${online ? " sc-online" : ""}`} />
      {showLabel && (
        <span style={{ fontSize: 12.5, color: online ? "var(--sc-success)" : "var(--sc-text-tertiary)" }}>
          {online ? t("common.online") : t("common.offline")}
        </span>
      )}
    </span>
  );
}
