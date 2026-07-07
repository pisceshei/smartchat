/** Shared renderer for the MessageContent block union — used by the inbox
 *  conversation view and (later) broadcast/flow previews. Mirrors
 *  py_contracts/content.py exactly. */
import {
  AudioOutlined,
  EnvironmentOutlined,
  FileOutlined,
  FileTextOutlined,
  PaperClipOutlined,
} from "@ant-design/icons";
import { Image, Typography } from "antd";
import type {
  ContentBlock,
  EmailBlock,
  MediaBlock,
  MessageContent,
  ProductCardBlock,
  QuickButtonsBlock,
} from "@/api/types";
import { t } from "@/i18n";

function formatBytes(size?: number | null): string {
  if (!size) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function Media({ block }: { block: MediaBlock }) {
  const url = block.url ?? undefined;
  switch (block.media_type) {
    case "image":
    case "sticker":
      return (
        <div>
          {url ? (
            <Image
              src={url}
              alt={block.caption ?? t("inbox.msg.image")}
              style={{ maxWidth: 260, maxHeight: 220, borderRadius: 8, objectFit: "cover" }}
            />
          ) : (
            <span className="sc-text-secondary">
              <FileOutlined /> {t("inbox.msg.image")}
            </span>
          )}
          {block.caption && <div style={{ marginTop: 4 }}>{block.caption}</div>}
        </div>
      );
    case "video":
      return url ? (
        <video src={url} controls style={{ maxWidth: 280, borderRadius: 8 }} />
      ) : (
        <span className="sc-text-secondary">
          <FileOutlined /> {t("inbox.msg.video")}
        </span>
      );
    case "audio":
    case "voice":
      return (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <AudioOutlined style={{ color: "var(--sc-primary)" }} />
          {url ? (
            <audio src={url} controls style={{ height: 32, maxWidth: 240 }} />
          ) : (
            <span>{t("inbox.msg.voice")}</span>
          )}
          {block.duration_ms ? (
            <span className="sc-text-tertiary" style={{ fontSize: 12 }}>
              {Math.round(block.duration_ms / 1000)}s
            </span>
          ) : null}
        </span>
      );
    default:
      return (
        <a
          href={url}
          target="_blank"
          rel="noreferrer"
          style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
        >
          <PaperClipOutlined />
          <span>{block.file_name ?? block.caption ?? t("inbox.msg.file")}</span>
          <span className="sc-text-tertiary" style={{ fontSize: 12 }}>
            {formatBytes(block.size)}
          </span>
        </a>
      );
  }
}

function ProductCard({ block }: { block: ProductCardBlock }) {
  const img = block.image_url ?? undefined;
  return (
    <div className="sc-card-block">
      {img && <img src={img} alt={block.title} loading="lazy" />}
      <div className="sc-card-body">
        <div className="sc-card-title">{block.title}</div>
        {block.subtitle && (
          <div className="sc-text-secondary" style={{ fontSize: 12.5, marginTop: 2 }}>
            {block.subtitle}
          </div>
        )}
        {block.price && (
          <div className="sc-card-price">
            {block.currency ?? ""} {block.price}
          </div>
        )}
      </div>
      {block.buttons.length > 0 && (
        <div className="sc-card-btns">
          {block.buttons.map((b, i) => (
            <button
              key={i}
              type="button"
              onClick={() => {
                if (b.action === "url") window.open(b.value, "_blank", "noreferrer");
              }}
            >
              {b.text}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function QuickButtons({ block }: { block: QuickButtonsBlock }) {
  return (
    <div>
      <div>{block.text}</div>
      <div className="sc-quick-btns">
        {block.buttons.map((b) => (
          <span key={b.id}>{b.text}</span>
        ))}
      </div>
    </div>
  );
}

function Email({ block }: { block: EmailBlock }) {
  return (
    <div>
      {block.subject && (
        <div style={{ fontWeight: 600, marginBottom: 4, display: "flex", gap: 6, alignItems: "center" }}>
          <FileTextOutlined style={{ color: "var(--sc-text-secondary)" }} />
          {block.subject}
        </div>
      )}
      <Typography.Paragraph
        style={{ margin: 0, whiteSpace: "pre-wrap" }}
        ellipsis={{ rows: 12, expandable: true, symbol: t("common.more") }}
      >
        {block.text}
      </Typography.Paragraph>
    </div>
  );
}

export function BlockRenderer({ block }: { block: ContentBlock }) {
  switch (block.kind) {
    case "text":
      return <span>{block.text}</span>;
    case "media":
      return <Media block={block} />;
    case "product_card":
      return <ProductCard block={block} />;
    case "quick_buttons":
      return <QuickButtons block={block} />;
    case "button_reply":
      return (
        <span>
          <span
            style={{
              display: "inline-block",
              border: "1px solid var(--sc-primary)",
              color: "var(--sc-primary)",
              borderRadius: 999,
              padding: "1px 10px",
              fontSize: 13,
            }}
          >
            {block.text}
          </span>
        </span>
      );
    case "template":
      return (
        <span className="sc-text-secondary">
          <FileTextOutlined /> {t("inbox.msg.template")}：{block.template_name} ({block.language})
        </span>
      );
    case "location":
      return (
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
          <EnvironmentOutlined style={{ color: "var(--sc-error)" }} />
          <span>
            {block.name ?? t("inbox.msg.location")}
            {block.address && (
              <span className="sc-text-secondary" style={{ marginLeft: 6, fontSize: 12.5 }}>
                {block.address}
              </span>
            )}
          </span>
        </span>
      );
    case "email":
      return <Email block={block} />;
    case "system_event":
      return <span>{block.event}</span>;
    default:
      return null;
  }
}

export function MessageBlocks({ content }: { content: MessageContent }) {
  const blocks = content?.blocks ?? [];
  return (
    <>
      {blocks.map((b, i) => (
        <div key={i} style={i > 0 ? { marginTop: 6 } : undefined}>
          <BlockRenderer block={b} />
        </div>
      ))}
    </>
  );
}
