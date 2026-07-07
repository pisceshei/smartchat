/**
 * TypeScript mirror of packages/py_contracts/py_contracts/content.py —
 * the canonical MessageContent discriminated union. The API serialises
 * message content in exactly this shape; media/file blocks additionally
 * carry a resolved `url` when read back (MinIO presigned / public URL).
 */

export interface TextBlock {
  kind: "text";
  text: string;
}

export interface MediaBlock {
  kind: "media";
  media_type: "image" | "video" | "audio" | "voice" | "file" | "sticker";
  file_id?: string | null;
  /** Resolved download/display URL (added by the API at read time). */
  url?: string | null;
  caption?: string | null;
  mime?: string | null;
  size?: number | null;
  duration_ms?: number | null;
  width?: number | null;
  height?: number | null;
  /** Original filename, when known. */
  name?: string | null;
}

export interface CardButton {
  text: string;
  action: "url" | "postback";
  value: string;
}

export interface ProductCardBlock {
  kind: "product_card";
  title: string;
  subtitle?: string | null;
  image_file_id?: string | null;
  image_url?: string | null;
  price?: string | null;
  currency?: string | null;
  url?: string | null;
  buttons?: CardButton[];
}

export interface QuickButton {
  id: string;
  text: string;
}

export interface QuickButtonsBlock {
  kind: "quick_buttons";
  text: string;
  buttons: QuickButton[];
}

export interface ButtonReplyBlock {
  kind: "button_reply";
  payload: string;
  text: string;
  flow_session_id?: string | null;
}

export interface SystemEventBlock {
  kind: "system_event";
  event: string;
  meta?: Record<string, unknown>;
}

export type ContentBlock =
  | TextBlock
  | MediaBlock
  | ProductCardBlock
  | QuickButtonsBlock
  | ButtonReplyBlock
  | SystemEventBlock
  // Forward-compat: unknown kinds render as nothing rather than crashing.
  | { kind: string; [k: string]: unknown };

export interface MessageContent {
  blocks: ContentBlock[];
}

export type SenderType = "contact" | "member" | "ai_agent" | "automation" | "system";

/** Message as serialised by the widget REST/WS API. */
export interface WireMessage {
  id: string;
  conversation_id?: string | null;
  sender_type: SenderType;
  sender_name?: string | null;
  sender_avatar_url?: string | null;
  content: MessageContent;
  client_msg_id?: string | null;
  created_at: string; // ISO-8601
  seq?: number | null;
  delivery_status?: "pending" | "sent" | "delivered" | "read" | "failed" | null;
}
