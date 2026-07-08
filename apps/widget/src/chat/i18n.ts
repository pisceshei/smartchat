/** zh-Hant + zh-CN + en UI strings, auto-selected from navigator.language. */

export type Lang = "en" | "zh-Hant" | "zh-CN";

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
    home_new_conversation: "New Conversation",
    home_reply_hint: "We typically reply in a few minutes",
    home_tab_home: "Home",
    home_tab_chat: "Chat",
    home_online_hint: "Need help? We are online!",
    home_offline_hint: "Leave a message and we will get back to you.",
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
    home_new_conversation: "新對話",
    home_reply_hint: "我們通常在幾分鐘內回覆",
    home_tab_home: "首頁",
    home_tab_chat: "對話",
    home_online_hint: "需要幫助嗎？我們在線上！",
    home_offline_hint: "請留言，我們會盡快回覆您。",
  },
  "zh-CN": {
    online: "在线",
    offline: "离线",
    connecting: "连接中…",
    reconnecting: "重新连接中…",
    typing: "正在输入…",
    composer_placeholder: "输入消息…",
    send: "发送",
    emoji: "表情",
    attach: "附加文件",
    close: "关闭",
    prechat_title: "开始对话前",
    prechat_intro: "请留下您的资料，让我们更好地为您服务。",
    start_chat: "开始对话",
    skip: "跳过",
    required: "必填",
    invalid_email: "请输入有效的电子邮箱地址",
    invalid_phone: "请输入有效的电话号码",
    offline_notice: "我们目前不在线。请留言，我们会尽快回复您。",
    offline_email_label: "您的电子邮箱（以便回复您）",
    offline_email_save: "确定",
    offline_email_saved: "已收到！我们会通过电子邮件回复您。",
    failed: "发送失败",
    retry: "重试",
    file: "文件",
    image: "图片",
    voice: "语音消息",
    video: "视频",
    today: "今天",
    welcome_default: "您好！请问有什么可以帮到您？",
    powered_by: "技术支持",
    upload_too_large: "文件过大（上限 20MB）",
    upload_failed: "上传失败，请重试",
    select_placeholder: "请选择…",
    you: "您",
    home_new_conversation: "新对话",
    home_reply_hint: "我们通常在几分钟内回复",
    home_tab_home: "首页",
    home_tab_chat: "对话",
    home_online_hint: "需要帮助吗？我们在线上！",
    home_offline_hint: "请留言，我们会尽快回复您。",
  },
};

export function detectLang(preferred?: string | null): Lang {
  const l = (preferred || navigator.language || "en").toLowerCase();
  if (l.indexOf("zh") !== 0) return "en";
  return /^zh[-_]?(cn|sg|hans)/.test(l) ? "zh-CN" : "zh-Hant";
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
    return new Intl.DateTimeFormat(current, {
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(iso));
  } catch {
    return "";
  }
}
