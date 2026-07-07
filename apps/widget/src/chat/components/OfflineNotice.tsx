import { useState } from "preact/hooks";

import { localized, type WidgetBootstrap } from "../../shared/config";
import { saveOfflineEmail } from "../controller";
import { t } from "../i18n";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export function OfflineNotice(props: {
  config: WidgetBootstrap;
  lang: string;
  emailSaved: boolean;
}) {
  const off = props.config.offline;
  const [email, setEmail] = useState("");
  const [err, setErr] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
  if (!off || off.is_online !== false) return null;

  const notice = localized(off.notice, props.lang) || t("offline_notice");
  const wantsEmail = off.email_fallback !== false && !props.emailSaved && !justSaved;

  const save = (e: Event) => {
    e.preventDefault();
    if (!EMAIL_RE.test(email)) {
      setErr(true);
      return;
    }
    saveOfflineEmail(email.trim());
    setJustSaved(true);
  };

  return (
    <div class="sc-offline">
      <div class="sc-offline-notice">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.7" />
          <path d="M12 8v4.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
          <circle cx="12" cy="15.8" r="1" fill="currentColor" />
        </svg>
        <span>{notice}</span>
      </div>
      {justSaved || (props.emailSaved && off.email_fallback !== false) ? (
        <div class="sc-offline-saved">{t("offline_email_saved")}</div>
      ) : wantsEmail ? (
        <form class="sc-offline-form" onSubmit={save}>
          <input
            class={"sc-field-input" + (err ? " invalid" : "")}
            type="email"
            placeholder={t("offline_email_label")}
            value={email}
            onInput={(e) => {
              setEmail((e.currentTarget as HTMLInputElement).value);
              setErr(false);
            }}
          />
          <button type="submit" class="sc-primary-btn small">
            {t("offline_email_save")}
          </button>
        </form>
      ) : null}
    </div>
  );
}
