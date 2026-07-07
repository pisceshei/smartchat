/** @xyflow/react custom node renderers, one per category. A single card shell
 *  reads the domain FlowNode from `data.node`, draws the accent icon chip,
 *  a config summary, and the dynamic output ports (Handles) from graph.ts. */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { FlowNode } from "@/api/types";
import { t } from "@/i18n";
import type { RFNodeData } from "./graph";
import { portsOf } from "./graph";
import { metaFor } from "./nodes";

interface KeywordGroup {
  id: string;
  keywords: string[];
}
interface TextBlockCfg {
  kind: string;
  text?: string;
}

/** Short human summary of a node's config for the card body. */
function summarize(node: FlowNode): string {
  const c = node.config ?? {};
  switch (node.kind) {
    case "trigger.visitor_message": {
      if (c.match_mode === "any") return t("nc.matchMode.any");
      const groups = (c.keyword_groups as KeywordGroup[] | undefined) ?? [];
      const words = groups.flatMap((g) => g.keywords).filter(Boolean);
      const dicts = (c.dict_ids as string[] | undefined)?.length ?? 0;
      if (words.length === 0 && dicts === 0) return "";
      const head = words.slice(0, 3).join("、");
      const extra = words.length > 3 ? ` +${words.length - 3}` : "";
      return `${head}${extra}${dicts ? `（含 ${dicts} 個詞庫）` : ""}`;
    }
    case "trigger.page_visited":
      return c.url ? `${c.url}` : "";
    case "trigger.agent_timeout":
    case "trigger.visitor_timeout":
      return c.minutes ? `${c.minutes} 分鐘` : "";
    case "trigger.visitor_intent": {
      const n = (c.intent_ids as string[] | undefined)?.length ?? 0;
      return n ? `${n} 個意圖` : "";
    }
    case "action.send_message": {
      const blocks = (c.blocks as TextBlockCfg[] | undefined) ?? [];
      const first = blocks.find((b) => b.kind === "text");
      if (first?.text) return first.text;
      if (blocks.some((b) => b.kind === "product_card")) return "商品卡片";
      return "";
    }
    case "action.ask_question":
      return (c.question as string) || "";
    case "action.send_email":
      return (c.subject as string) || "";
    case "action.quick_buttons":
      return (c.text as string) || "";
    case "action.delay": {
      if (!c.value) return "";
      const unit = (c.unit as string) || "minutes";
      const unitLabel =
        unit === "hours" ? t("nc.delayUnit.hours") : unit === "days" ? t("nc.delayUnit.days") : t("nc.delayUnit.minutes");
      return `${c.value} ${unitLabel}`;
    }
    case "action.external_request":
      return c.url ? `${(c.method as string) || "GET"} ${c.url}` : "";
    case "action.update_contact":
      return c.field ? `${c.field} = ${c.value ?? ""}` : "";
    case "action.request_rating":
      return (c.prompt as string) || "";
    default:
      return "";
  }
}

function NodeCard({ id, data, selected }: NodeProps) {
  const node = (data as RFNodeData).node;
  const meta = metaFor(node.kind);
  const ports = portsOf(node);
  const summary = summarize(node);
  const catLabel =
    node.category === "trigger"
      ? t("fe.palette.trigger")
      : node.category === "condition"
        ? t("fe.palette.condition")
        : t("fe.palette.action");

  return (
    <div className={`sc-fn${selected ? " sc-selected" : ""}`} data-nodeid={id}>
      {/* triggers are entry points — no inbound handle */}
      {node.category !== "trigger" && (
        <Handle type="target" position={Position.Left} style={{ top: 22 }} />
      )}

      <div className="sc-fn-head">
        <span className="sc-fn-chip" style={{ background: meta?.accent ?? "var(--sc-primary)" }}>
          {meta?.icon}
        </span>
        <div className="sc-fn-head-text">
          <div className="sc-fn-name">{node.title || meta?.label || node.kind}</div>
          <div className="sc-fn-cat">{catLabel}</div>
        </div>
      </div>

      <div className={`sc-fn-body${summary ? "" : " sc-fn-body-empty"}`}>
        {summary || meta?.desc || ""}
      </div>

      {ports.length > 0 && (
        <div className="sc-fn-ports">
          {ports.map((p) => (
            <div key={p.id} className={`sc-fn-port${p.tone && p.tone !== "default" ? ` sc-tone-${p.tone}` : ""}`}>
              <span className="sc-fn-port-dot">{p.label}</span>
              <Handle type="source" position={Position.Right} id={p.id} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export const nodeTypes = {
  trigger: NodeCard,
  condition: NodeCard,
  action: NodeCard,
};
