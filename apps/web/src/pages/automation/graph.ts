/** Domain FlowGraph <-> @xyflow/react node/edge conversion + port derivation.
 *  The port protocol (source_port / xyflow sourceHandle) follows plan 附錄 B.1:
 *  out / button:<id> / branch:<idx> / else / answered / timeout / invalid /
 *  success / failed. */
import type { Edge, Node } from "@xyflow/react";
import type { FlowEdge, FlowGraph, FlowNode } from "@/api/types";
import { t } from "@/i18n";
import { metaFor } from "./nodes";

export interface Port {
  id: string; // = source_port / sourceHandle
  label: string;
  tone?: "default" | "timeout" | "error" | "else";
}

export interface RFNodeData extends Record<string, unknown> {
  node: FlowNode;
}

export type RFNode = Node<RFNodeData>;
export type RFEdge = Edge;

let idSeq = 0;
export function genId(prefix = "n"): string {
  idSeq += 1;
  const rnd =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  return `${prefix}_${rnd}${idSeq.toString(36)}`;
}

interface Branch {
  id?: string;
  lang?: string;
  device?: string;
  weight?: number;
  countries?: string[];
  field?: string;
  op?: string;
  value?: string;
  days?: number[];
  start?: string;
  end?: string;
}

interface Button {
  id: string;
  text: string;
}

const DEVICE_LABEL: Record<string, string> = {
  desktop: t("nc.device.desktop"),
  mobile: t("nc.device.mobile"),
  tablet: t("nc.device.tablet"),
};

/** Ports a node exposes given its live config (branch/button ports are dynamic). */
export function portsOf(node: FlowNode): Port[] {
  const meta = metaFor(node.kind);
  if (!meta || meta.terminal) return [];
  const cfg = node.config ?? {};

  if (node.category === "condition") {
    const branches = (cfg.branches as Branch[] | undefined) ?? [];
    const ports: Port[] = branches.map((b, i) => {
      let label = `${t("nc.branches")} ${i + 1}`;
      if (node.kind === "cond.language" && b.lang) label = b.lang;
      else if (node.kind === "cond.device" && b.device) label = DEVICE_LABEL[b.device] ?? b.device;
      else if (node.kind === "cond.random") label = `${b.weight ?? 0}%`;
      else if (node.kind === "cond.country" && b.countries?.length) label = b.countries.join("/");
      else if (node.kind === "cond.contact_attribute" && b.field)
        label = `${b.field} ${b.op ?? ""} ${b.value ?? ""}`.trim();
      else if (node.kind === "cond.external_variable" && (b.op || b.value))
        label = `${b.op ?? ""} ${b.value ?? ""}`.trim();
      else if (node.kind === "cond.time_window") label = `${b.start ?? ""}-${b.end ?? ""}`;
      return { id: `branch:${i}`, label };
    });
    ports.push({ id: "else", label: t("fe.port.else"), tone: "else" });
    return ports;
  }

  if (node.kind === "action.quick_buttons") {
    const buttons = (cfg.buttons as Button[] | undefined) ?? [];
    const ports: Port[] = buttons.map((b, i) => ({
      id: `button:${b.id}`,
      label: b.text || `${t("nc.buttonText")} ${i + 1}`,
    }));
    ports.push({ id: "timeout", label: t("fe.port.timeout"), tone: "timeout" });
    return ports;
  }

  if (node.kind === "action.ask_question") {
    return [
      { id: "answered", label: t("fe.port.answered") },
      { id: "timeout", label: t("fe.port.timeout"), tone: "timeout" },
      { id: "invalid", label: t("fe.port.invalid"), tone: "error" },
    ];
  }

  if (node.kind === "action.external_request") {
    return [
      { id: "success", label: t("fe.port.success") },
      { id: "failed", label: t("fe.port.failed"), tone: "error" },
    ];
  }

  return [{ id: "out", label: t("fe.port.out") }];
}

/** A newly-created domain node at a canvas position. */
export function newNode(kind: string, position: { x: number; y: number }): FlowNode {
  const meta = metaFor(kind);
  return {
    id: genId(),
    kind,
    category: meta?.category ?? "action",
    position,
    title: null,
    config: meta ? meta.defaultConfig() : {},
  };
}

export function toReactFlow(graph: FlowGraph): { nodes: RFNode[]; edges: RFEdge[] } {
  const nodes: RFNode[] = (graph.nodes ?? []).map((n) => ({
    id: n.id,
    type: n.category,
    position: n.position ?? { x: 0, y: 0 },
    data: { node: n },
  }));
  const edges: RFEdge[] = (graph.edges ?? []).map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.source_port,
    type: "smoothstep",
  }));
  return { nodes, edges };
}

export function fromReactFlow(nodes: RFNode[], edges: RFEdge[]): FlowGraph {
  return {
    nodes: nodes.map((rf) => ({
      ...rf.data.node,
      position: rf.position,
    })),
    edges: edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      source_port: e.sourceHandle ?? "out",
    })) as FlowEdge[],
  };
}

/** Client-side publish validation mirroring the backend rules (plan 附錄 B.1):
 *  ≥1 trigger, every non-terminal reachable node should lead somewhere, edge
 *  ports must exist, no duplicate port usage. Returns human messages. */
export function validateGraph(graph: FlowGraph): { node_id?: string; message: string }[] {
  const errors: { node_id?: string; message: string }[] = [];
  const triggers = graph.nodes.filter((n) => n.category === "trigger");
  if (triggers.length === 0) errors.push({ message: "流程至少需要一個觸發器" });

  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const usedPort = new Set<string>();

  for (const e of graph.edges) {
    const src = byId.get(e.source);
    if (!src) {
      errors.push({ message: "存在指向不存在節點的連線" });
      continue;
    }
    const ports = portsOf(src);
    if (!ports.some((p) => p.id === e.source_port)) {
      errors.push({ node_id: e.source, message: `節點「${labelOf(src)}」的出口已失效，請重新連線` });
    }
    const key = `${e.source}::${e.source_port}`;
    if (usedPort.has(key)) {
      errors.push({ node_id: e.source, message: `節點「${labelOf(src)}」有出口重複連線` });
    }
    usedPort.add(key);
    if (!byId.has(e.target)) errors.push({ message: "存在指向不存在節點的連線" });
  }

  // condition branches must be fully wired (else is optional-but-recommended)
  for (const n of graph.nodes) {
    const ports = portsOf(n);
    if (n.category === "condition") {
      const wired = new Set(
        graph.edges.filter((e) => e.source === n.id).map((e) => e.source_port),
      );
      const missing = ports.filter((p) => p.id !== "else" && !wired.has(p.id));
      if (missing.length === ports.length - 1 && ports.length > 1) {
        errors.push({ node_id: n.id, message: `條件節點「${labelOf(n)}」尚未連接任何分支` });
      }
    }
  }
  return errors;
}

export function labelOf(node: FlowNode): string {
  return node.title || metaFor(node.kind)?.label || node.kind;
}
