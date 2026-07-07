/** Renders MessageContent blocks: text / media / product_card / quick_buttons /
 *  button_reply / system_event. Unknown kinds render nothing (forward-compat). */
import type {
  ContentBlock,
  MediaBlock,
  ProductCardBlock,
  QuickButtonsBlock,
} from "../../shared/content";
import { tapCardButton, tapQuickButton } from "../controller";
import { t } from "../i18n";

const URL_RE = /(https?:\/\/[^\s<]+)/g;

function TextWithLinks(props: { text: string }) {
  const parts = props.text.split(URL_RE);
  return (
    <span>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <a href={p} target="_blank" rel="noopener noreferrer">
            {p}
          </a>
        ) : (
          p
        ),
      )}
    </span>
  );
}

function prettySize(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function Media(props: { block: MediaBlock }) {
  const b = props.block;
  const url = b.url || "";
  switch (b.media_type) {
    case "image":
    case "sticker":
      return (
        <a href={url} target="_blank" rel="noopener noreferrer" class="sc-media-img">
          <img src={url} alt={b.caption || t("image")} loading="lazy" />
          {b.caption ? <div class="sc-caption">{b.caption}</div> : null}
        </a>
      );
    case "video":
      return (
        <div class="sc-media-video">
          <video src={url} controls preload="metadata" />
          {b.caption ? <div class="sc-caption">{b.caption}</div> : null}
        </div>
      );
    case "audio":
    case "voice":
      return (
        <div class="sc-media-audio">
          <audio src={url} controls preload="metadata" />
        </div>
      );
    default:
      return (
        <a class="sc-file" href={url} target="_blank" rel="noopener noreferrer" download>
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path
              d="M6 2h8l4 4v16H6V2Zm8 0v4h4"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linejoin="round"
            />
          </svg>
          <span class="sc-file-meta">
            <span class="sc-file-name">{b.name || b.caption || t("file")}</span>
            <span class="sc-file-size">{prettySize(b.size)}</span>
          </span>
        </a>
      );
  }
}

function ProductCard(props: { block: ProductCardBlock }) {
  const b = props.block;
  const img = b.image_url || "";
  return (
    <div class="sc-card">
      {img ? (
        b.url ? (
          <a href={b.url} target="_blank" rel="noopener noreferrer">
            <img class="sc-card-img" src={img} alt={b.title} loading="lazy" />
          </a>
        ) : (
          <img class="sc-card-img" src={img} alt={b.title} loading="lazy" />
        )
      ) : null}
      <div class="sc-card-body">
        <div class="sc-card-title">
          {b.url ? (
            <a href={b.url} target="_blank" rel="noopener noreferrer">
              {b.title}
            </a>
          ) : (
            b.title
          )}
        </div>
        {b.subtitle ? <div class="sc-card-subtitle">{b.subtitle}</div> : null}
        {b.price ? (
          <div class="sc-card-price">
            {b.currency ? b.currency + " " : ""}
            {b.price}
          </div>
        ) : null}
        {b.buttons && b.buttons.length > 0 ? (
          <div class="sc-card-btns">
            {b.buttons.map((btn) => (
              <button
                type="button"
                class="sc-card-btn"
                onClick={() => tapCardButton(btn)}
              >
                {btn.text}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function QuickButtons(props: {
  block: QuickButtonsBlock;
  messageId: string;
  answered: boolean;
}) {
  const { block, messageId, answered } = props;
  return (
    <div class="sc-quick">
      <div class="sc-text">
        <TextWithLinks text={block.text} />
      </div>
      <div class="sc-chips">
        {block.buttons.map((btn) => (
          <button
            type="button"
            class="sc-chip"
            disabled={answered}
            onClick={() => tapQuickButton(messageId, btn)}
          >
            {btn.text}
          </button>
        ))}
      </div>
    </div>
  );
}

export function Block(props: {
  block: ContentBlock;
  messageId: string;
  quickAnswered: boolean;
}) {
  const b = props.block;
  switch (b.kind) {
    case "text":
      return (
        <div class="sc-text">
          <TextWithLinks text={(b as { text: string }).text} />
        </div>
      );
    case "media":
      return <Media block={b as MediaBlock} />;
    case "product_card":
      return <ProductCard block={b as ProductCardBlock} />;
    case "quick_buttons":
      return (
        <QuickButtons
          block={b as QuickButtonsBlock}
          messageId={props.messageId}
          answered={props.quickAnswered}
        />
      );
    case "button_reply":
      return <div class="sc-text">{(b as { text: string }).text}</div>;
    case "system_event":
      return <div class="sc-system-chip">{(b as { event: string }).event}</div>;
    default:
      return null;
  }
}

/** True when a block renders as a "bare" card (no bubble chrome). */
export function isBare(block: ContentBlock): boolean {
  return block.kind === "product_card" || block.kind === "media";
}
