/** zh-Hant + en UI strings, auto-selected from navigator.language. */

export type Lang = "en" | "zh-Hant";

const STRINGS: Record<Lang, Record<string, string>> = {
  en: {
    online: "Online",
    offline: "Offline",
    connecting: "Connecting…",
    reconnecting: "Reconnecting…",
    typing: "typing…",
    composer_placeholder: "Type a message…",
    send: "Send",
    emoji: "Emoji",
    attach: "Attach file",
    close: "Close",
    prechat_title: "Before we start",
    prechat_intro: "Please leave your details so we can serve you better.",
    start_chat: "Start chat",
    skip: "Skip",
    required: "Required",
    invalid_email: "Please enter a valid email address",
    invalid_phone: "Please enter a valid phone number",
    offline_notice: "We are currently offline. Leave a message and we will get back to you soon.",
    offline_email_label: "Your email (so we can reply)",
    offline_email_save: "OK",
    offline_email_saved: "Thanks! We will reply to your email.",
    failed: "Failed to send",
    retry: "Retry",
    file: "File",
    image: "Image",
    voice: "Voice message",
    video: "Video",
    today: "Today",
    welcome_default: "Hi there! How can we help you today?",
    powered_by: "Powered by",
    upload_too_large: "File is too large (max 20MB)",
    upload_failed: "Upload failed, please try again",
    select_placeholder: "Please select…",
    you: "You",
  },
  "zh-Hant": {
    online: "在線",
    offline: "離線",
    connecting: "連線中…",
    reconnecting: "重新連線中…",
    typing: "正在輸入…",
    composer_placeholder: "輸入訊息…",
    send: "傳送",
    emoji: "表情",
    attach: "附加檔案",
    close: "關閉",
    prechat_title: "開始對話前",
    prechat_intro: "請留下您的資料，讓我們更好地為您服務。",
    start_chat: "開始對話",
    skip: "略過",
    required: "必填",
    invalid_email: "請輸入有效的電子郵件地址",
    invalid_phone: "請輸入有效的電話號碼",
    offline_notice: "我們目前不在線。請留言，我們會盡快回覆您。",
    offline_email_label: "您的電子郵件（以便回覆您）",
    offline_email_save: "確定",
    offline_email_saved: "已收到！我們會透過電子郵件回覆您。",
    failed: "傳送失敗",
    retry: "重試",
    file: "檔案",
    image: "圖片",
    voice: "語音訊息",
    video: "影片",
    today: "今天",
    welcome_default: "您好！請問有什麼可以幫到您？",
    powered_by: "技術支援",
    upload_too_large: "檔案過大（上限 20MB）",
    upload_failed: "上傳失敗，請重試",
    select_placeholder: "請選擇…",
    you: "您",
  },
};

export function detectLang(preferred?: string | null): Lang {
  const l = (preferred || navigator.language || "en").toLowerCase();
  return l.indexOf("zh") === 0 || l === "zh-hant" ? "zh-Hant" : "en";
}

let current: Lang = "en";

export function setLang(l: Lang): void {
  current = l;
}

export function getLang(): Lang {
  return current;
}

export function t(key: string): string {
  return STRINGS[current][key] ?? STRINGS.en[key] ?? key;
}

export function formatTime(iso: string): string {
  try {
    return new Intl.DateTimeFormat(current === "zh-Hant" ? "zh-Hant" : "en", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(iso));
  } catch {
    return "";
  }
}
