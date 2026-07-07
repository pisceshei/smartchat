/** Composer AI-assist — streams rewrite / expand / shorten / tone / grammar /
 *  translate-draft over SSE (plan 附錄 B.2). Renders the streamed suggestion
 *  with an 採用 button that replaces the composer text. */
import {
  ColumnHeightOutlined,
  EditOutlined,
  LoadingOutlined,
  ShrinkOutlined,
  SmileOutlined,
  TranslationOutlined,
} from "@ant-design/icons";
import { App, Button, Select, Space } from "antd";
import { useEffect, useRef, useState } from "react";
import { composeAssistStream } from "@/api/endpoints";
import type { ComposerAssistMode } from "@/api/types";
import { TRANSLATE_LANGS } from "@/constants/channels";
import { t } from "@/i18n";

const TONES = ["friendly", "professional", "concise", "warm"] as const;

export function AiAssistPopover({
  text,
  conversationId,
  customerLang,
  onApply,
  onClose,
}: {
  text: string;
  conversationId: string;
  customerLang?: string | null;
  onApply: (v: string) => void;
  onClose: () => void;
}) {
  const { message } = App.useApp();
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState<ComposerAssistMode | null>(null);
  const [tone, setTone] = useState<string>("professional");
  const [lang, setLang] = useState<string>(customerLang || "en");
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const run = async (mode: ComposerAssistMode) => {
    if (!text.trim()) return;
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setBusy(mode);
    setResult("");
    try {
      await composeAssistStream(
        {
          conversation_id: conversationId,
          mode,
          text,
          tone: mode === "tone" ? tone : undefined,
          target_lang: mode === "translate_draft" ? lang : undefined,
        },
        (full) => setResult(full),
        ac.signal,
      );
    } catch {
      if (!ac.signal.aborted) message.error(t("inbox.assist.failed"));
    } finally {
      setBusy(null);
    }
  };

  const modeBtn = (mode: ComposerAssistMode, icon: React.ReactNode, label: string) => (
    <Button
      size="small"
      icon={busy === mode ? <LoadingOutlined spin /> : icon}
      disabled={!text.trim() || (!!busy && busy !== mode)}
      onClick={() => run(mode)}
    >
      {label}
    </Button>
  );

  return (
    <div style={{ width: 320 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>{t("inbox.assist.title")}</span>
        <span style={{ fontSize: 11, color: "var(--sc-text-tertiary)" }}>{t("inbox.assist.pointsHint")}</span>
      </div>

      {!text.trim() ? (
        <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", padding: "12px 0" }}>
          {t("inbox.assist.empty")}
        </div>
      ) : (
        <>
          <Space wrap size={6}>
            {modeBtn("rewrite", <EditOutlined />, t("inbox.assist.rewrite"))}
            {modeBtn("expand", <ColumnHeightOutlined />, t("inbox.assist.expand"))}
            {modeBtn("shorten", <ShrinkOutlined />, t("inbox.assist.shorten"))}
            {modeBtn("fix_grammar", <SmileOutlined />, t("inbox.assist.fixGrammar"))}
          </Space>

          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <Select
              size="small"
              value={tone}
              onChange={setTone}
              style={{ width: 100 }}
              options={TONES.map((tn) => ({ value: tn, label: t(`ai.tone.${tn}` as never) }))}
            />
            {modeBtn("tone", <SmileOutlined />, t("inbox.assist.tone"))}
          </div>

          <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
            <Select
              size="small"
              value={lang}
              onChange={setLang}
              style={{ width: 100 }}
              options={TRANSLATE_LANGS}
              showSearch
              optionFilterProp="label"
            />
            {modeBtn("translate_draft", <TranslationOutlined />, t("inbox.assist.translateDraft"))}
          </div>
        </>
      )}

      {(result || busy) && (
        <div
          style={{
            marginTop: 10,
            padding: 10,
            border: "1px solid var(--sc-border)",
            borderRadius: 8,
            background: "var(--sc-bg-subtle)",
            fontSize: 13,
            lineHeight: 1.55,
            maxHeight: 200,
            overflowY: "auto",
            whiteSpace: "pre-wrap",
          }}
        >
          {result || <span style={{ color: "var(--sc-text-tertiary)" }}>{t("inbox.assist.generating")}</span>}
        </div>
      )}

      {result && !busy && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
          <Button
            type="primary"
            size="small"
            onClick={() => {
              onApply(result);
              onClose();
            }}
          >
            {t("inbox.assist.apply")}
          </Button>
        </div>
      )}
    </div>
  );
}
