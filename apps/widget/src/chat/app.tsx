import { useEffect } from "preact/hooks";

import { DEFAULT_PRIMARY } from "../shared/config";
import { requestClose } from "./controller";
import { t } from "./i18n";
import { useAppState } from "./store";
import { Composer } from "./components/Composer";
import { Header } from "./components/Header";
import { BottomNav, HomeScreen } from "./components/HomeScreen";
import { MessageList } from "./components/MessageList";
import { OfflineNotice } from "./components/OfflineNotice";
import { PreChatForm } from "./components/PreChatForm";

function BootSkeleton() {
  return (
    <div class="sc-app sc-boot">
      <div class="sc-skel-header" />
      <div class="sc-skel-body">
        <div class="sc-skel-bubble" />
        <div class="sc-skel-bubble short" />
      </div>
    </div>
  );
}

export function App() {
  const s = useAppState();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") requestClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    const primary = s.config?.appearance?.primary_color || DEFAULT_PRIMARY;
    document.documentElement.style.setProperty("--sc-primary", primary);
  }, [s.config]);

  if (!s.ready || !s.config) return <BootSkeleton />;

  const showPrechat = s.prechatVisible || s.prechatBlocking;
  const features = s.config.features || {};
  const homeEnabled = !!s.config.home?.enabled;
  const onHome = homeEnabled && s.view === "home";

  return (
    <div class="sc-app">
      {onHome ? (
        <HomeScreen config={s.config} lang={s.lang} />
      ) : (
        <>
          <Header config={s.config} conn={s.conn} />
          {showPrechat ? (
            <PreChatForm config={s.config} blocking={s.prechatBlocking} lang={s.lang} />
          ) : (
            <MessageList
              messages={s.messages}
              config={s.config}
              agentTyping={s.agentTyping}
              answeredQuickBlocks={s.answeredQuickBlocks}
              offlineBanner={
                <OfflineNotice config={s.config} lang={s.lang} emailSaved={s.offlineEmailSaved} />
              }
            />
          )}
          {!showPrechat ? (
            <Composer
              disabled={s.conn === "boot"}
              allowUpload={features.file_upload !== false}
              allowEmoji={features.emoji !== false}
              error={s.composerError}
            />
          ) : null}
        </>
      )}
      {homeEnabled ? <BottomNav view={onHome ? "home" : "chat"} unread={s.unread} /> : null}
      {s.config.appearance?.show_branding !== false ? (
        <div class="sc-branding">
          {t("powered_by")} <strong>SmartChat</strong>
        </div>
      ) : null}
    </div>
  );
}
