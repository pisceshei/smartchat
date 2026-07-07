/** Channel icon chip: bundled vector icons where available, otherwise a
 *  2-letter glyph badge. Never emoji, never third-party logo assets. */
import {
  CommentOutlined,
  FacebookFilled,
  GlobalOutlined,
  InstagramOutlined,
  MailOutlined,
  MessageOutlined,
  SendOutlined,
  SlackOutlined,
  WechatFilled,
  WhatsAppOutlined,
  YoutubeFilled,
} from "@ant-design/icons";
import type { ReactNode } from "react";
import type { ChannelType } from "@/api/types";
import { CHANNEL_CATALOG } from "@/constants/channels";
import { channelColors } from "@/theme/tokens";

const ICONS: Partial<Record<ChannelType, ReactNode>> = {
  widget: <GlobalOutlined />,
  whatsapp_app: <WhatsAppOutlined />,
  whatsapp_api: <WhatsAppOutlined />,
  messenger: <FacebookFilled />,
  instagram: <InstagramOutlined />,
  telegram_app: <SendOutlined />,
  telegram_bot: <SendOutlined />,
  email: <MailOutlined />,
  youtube: <YoutubeFilled />,
  wechat: <WechatFilled />,
  wechat_kf: <WechatFilled />,
  wecom: <CommentOutlined />,
};

export function ChannelIcon({
  type,
  size = 18,
}: {
  type: ChannelType | string;
  size?: number;
}) {
  const color = channelColors[type] ?? "#64748B";
  const meta = CHANNEL_CATALOG.find((c) => c.type === type);
  const icon = ICONS[type as ChannelType];
  return (
    <span
      className="sc-channel-chip"
      style={{ width: size, height: size, background: color, fontSize: size * 0.58 }}
      title={meta?.name ?? String(type)}
      aria-label={meta?.name ?? String(type)}
    >
      {icon ?? (
        <span style={{ fontSize: size * 0.42, fontWeight: 700, lineHeight: 1 }}>
          {meta?.glyph ?? <MessageOutlined />}
        </span>
      )}
    </span>
  );
}
