import { useState } from "preact/hooks";

import { localized, type PreChatField, type WidgetBootstrap } from "../../shared/config";
import { skipPrechat, submitPrechat } from "../controller";
import { t } from "../i18n";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const PHONE_RE = /^\+?[0-9()\-\s]{5,20}$/;

function validate(field: PreChatField, value: string, lang: string): string | null {
  if (field.required && !value.trim()) return t("required");
  if (!value.trim()) return null;
  if (field.type === "email" && !EMAIL_RE.test(value)) return t("invalid_email");
  if (field.type === "phone" && !PHONE_RE.test(value)) return t("invalid_phone");
  void lang;
  return null;
}

export function PreChatForm(props: { config: WidgetBootstrap; blocking: boolean; lang: string }) {
  const fields = props.config.pre_chat?.fields ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  const setValue = (key: string, v: string) => {
    setValues((s) => ({ ...s, [key]: v }));
    setErrors((s) => ({ ...s, [key]: "" }));
  };

  const submit = (e: Event) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    for (const f of fields) {
      const err = validate(f, values[f.key] || "", props.lang);
      if (err) errs[f.key] = err;
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    const out: Record<string, unknown> = {};
    for (const f of fields) {
      const v = (values[f.key] || "").trim();
      if (v) out[f.key] = v;
    }
    submitPrechat(out);
  };

  return (
    <div class="sc-prechat">
      <form class="sc-prechat-card" onSubmit={submit}>
        <div class="sc-prechat-title">{t("prechat_title")}</div>
        <div class="sc-prechat-intro">{t("prechat_intro")}</div>
        {fields.map((f) => {
          const label = localized(f.label, props.lang) || f.key;
          const placeholder = localized(f.placeholder, props.lang);
          const err = errors[f.key];
          return (
            <label class="sc-field">
              <span class="sc-field-label">
                {label}
                {f.required ? <span class="sc-req">*</span> : null}
              </span>
              {f.type === "textarea" ? (
                <textarea
                  class={"sc-field-input" + (err ? " invalid" : "")}
                  rows={3}
                  placeholder={placeholder}
                  value={values[f.key] || ""}
                  onInput={(e) =>
                    setValue(f.key, (e.currentTarget as HTMLTextAreaElement).value)
                  }
                />
              ) : f.type === "select" ? (
                <select
                  class={"sc-field-input" + (err ? " invalid" : "")}
                  value={values[f.key] || ""}
                  onChange={(e) =>
                    setValue(f.key, (e.currentTarget as HTMLSelectElement).value)
                  }
                >
                  <option value="">{t("select_placeholder")}</option>
                  {(f.options || []).map((o) => (
                    <option value={o.value}>{localized(o.label, props.lang) || o.value}</option>
                  ))}
                </select>
              ) : (
                <input
                  class={"sc-field-input" + (err ? " invalid" : "")}
                  type={f.type === "email" ? "email" : f.type === "phone" ? "tel" : "text"}
                  placeholder={placeholder}
                  value={values[f.key] || ""}
                  onInput={(e) =>
                    setValue(f.key, (e.currentTarget as HTMLInputElement).value)
                  }
                />
              )}
              {err ? <span class="sc-field-err">{err}</span> : null}
            </label>
          );
        })}
        <button type="submit" class="sc-primary-btn">
          {t("start_chat")}
        </button>
        {!props.blocking ? (
          <button type="button" class="sc-link-btn" onClick={skipPrechat}>
            {t("skip")}
          </button>
        ) : null}
      </form>
    </div>
  );
}
