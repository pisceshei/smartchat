/** Notification preferences shown in the sidebar bell popover: sound + desktop
 *  toggles. Enabling desktop notifications is the user gesture we use to request
 *  browser permission (and to prime the AudioContext for sound playback). */
import { Switch } from "antd";
import { useState } from "react";
import { t } from "@/i18n";
import { useNotificationStore } from "@/stores/notifications";
import { ensureNotificationPermission, primeAudio } from "@/utils/notify";

export function NotificationSettings() {
  const soundEnabled = useNotificationStore((s) => s.soundEnabled);
  const desktopEnabled = useNotificationStore((s) => s.desktopEnabled);
  const setSoundEnabled = useNotificationStore((s) => s.setSoundEnabled);
  const setDesktopEnabled = useNotificationStore((s) => s.setDesktopEnabled);

  const supported = typeof Notification !== "undefined";
  const [permission, setPermission] = useState<NotificationPermission>(
    supported ? Notification.permission : "denied",
  );

  const onToggleSound = (checked: boolean) => {
    if (checked) primeAudio();
    setSoundEnabled(checked);
  };

  const onToggleDesktop = async (checked: boolean) => {
    if (checked) {
      primeAudio();
      const p = await ensureNotificationPermission();
      setPermission(p);
      if (p !== "granted") {
        setDesktopEnabled(false);
        return;
      }
    }
    setDesktopEnabled(checked);
  };

  const row: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "6px 0",
  };

  return (
    <div style={{ width: 280 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>{t("notify.settings.title")}</div>
      <div style={row}>
        <span>{t("notify.settings.sound")}</span>
        <Switch checked={soundEnabled} onChange={onToggleSound} />
      </div>
      <div style={row}>
        <span>{t("notify.settings.desktop")}</span>
        <Switch
          checked={desktopEnabled}
          onChange={onToggleDesktop}
          disabled={!supported || permission === "denied"}
        />
      </div>
      {supported && permission === "denied" ? (
        <div style={{ fontSize: 12, color: "var(--sc-warning)", marginTop: 4 }}>
          {t("notify.settings.blocked")}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: "var(--sc-text-secondary)", marginTop: 4 }}>
          {t("notify.settings.hint")}
        </div>
      )}
    </div>
  );
}
