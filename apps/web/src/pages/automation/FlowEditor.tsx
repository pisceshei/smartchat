/** Visual flow editor (canvas) built on @xyflow/react. Left palette (drag or
 *  click to add) → 觸發器/條件/動作 nodes with backend-matching ports → right
 *  property panel. Draft autosaves (debounced); 測試一下 runs the sandbox
 *  test-run API; 保存 validates + publishes. */
import "@xyflow/react/dist/style.css";
import {
  ArrowLeftOutlined,
  CheckCircleFilled,
  CloudSyncOutlined,
  ExclamationCircleOutlined,
  ExperimentOutlined,
  LoadingOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  App,
  Alert,
  Button,
  Drawer,
  Empty,
  Input,
  Result,
  Spin,
  Tag,
} from "antd";
import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Node,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { flowsApi } from "@/api/endpoints";
import type { FlowTestResult } from "@/api/types";
import { MessageBlocks } from "@/components/MessageBlocks";
import { t } from "@/i18n";
import { PropertyPanel } from "./PropertyPanel";
import { fromReactFlow, genId, newNode, toReactFlow, validateGraph, type RFEdge, type RFNode } from "./graph";
import { nodeTypes } from "./nodeComponents";
import { NODE_GROUPS, metaFor } from "./nodes";

const DND_KEY = "application/sc-node-kind";

function Palette({ onAdd }: { onAdd: (kind: string) => void }) {
  return (
    <div className="sc-fe-palette">
      <div className="sc-fe-hint" style={{ marginBottom: 10 }}>
        {t("fe.paletteHint")}
      </div>
      {NODE_GROUPS.map((grp) => (
        <div key={grp.category} className="sc-fe-palette-group">
          <div className="sc-fe-palette-title">
            {grp.category === "trigger"
              ? t("fe.palette.trigger")
              : grp.category === "condition"
                ? t("fe.palette.condition")
                : t("fe.palette.action")}
          </div>
          {grp.items.map((m) => (
            <div
              key={m.kind}
              className="sc-fe-palette-item"
              draggable
              onDragStart={(e) => {
                e.dataTransfer.setData(DND_KEY, m.kind);
                e.dataTransfer.effectAllowed = "move";
              }}
              onClick={() => onAdd(m.kind)}
              role="button"
              tabIndex={0}
            >
              <span className="sc-fn-chip" style={{ background: m.accent, width: 22, height: 22, fontSize: 12 }}>
                {m.icon}
              </span>
              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {m.label}
              </span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

type SaveState = "idle" | "saving" | "saved" | "error";

function EditorCanvas({ flowId }: { flowId: string }) {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const rf = useReactFlow();
  const [searchParams] = useSearchParams();
  const wrapRef = useRef<HTMLDivElement>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [testOpen, setTestOpen] = useState(searchParams.get("test") === "1");
  const hydrated = useRef(false);
  const dirty = useRef(false);
  const addCount = useRef(0);
  const markDirty = () => {
    dirty.current = true;
  };

  const flow = useQuery({
    queryKey: ["flow", flowId],
    queryFn: () => flowsApi.get(flowId),
    retry: 1,
  });

  // hydrate canvas once
  useEffect(() => {
    if (hydrated.current) return;
    if (flow.data) {
      const { nodes: n, edges: e } = toReactFlow(flow.data.draft_graph ?? { nodes: [], edges: [] });
      setNodes(n);
      setEdges(e);
      setName(flow.data.name);
      hydrated.current = true;
    } else if (flow.isError) {
      hydrated.current = true; // allow scratch editing offline
    }
  }, [flow.data, flow.isError, setNodes, setEdges]);

  const graph = useMemo(() => fromReactFlow(nodes, edges), [nodes, edges]);

  /* ---- draft autosave (debounced) ---- */
  const saveDraft = useMutation({
    mutationFn: () => flowsApi.saveDraft(flowId, fromReactFlow(nodes, edges)),
    onMutate: () => setSaveState("saving"),
    onSuccess: () => setSaveState("saved"),
    onError: () => setSaveState("error"),
  });

  useEffect(() => {
    if (!hydrated.current || !dirty.current) return;
    const h = setTimeout(() => saveDraft.mutate(), 900);
    return () => clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges]);

  // node/edge structural or position changes mark the draft dirty (pure
  // selection changes do not, so opening a node never triggers a save)
  const handleNodesChange: typeof onNodesChange = (changes) => {
    if (changes.some((ch) => ch.type !== "select")) markDirty();
    onNodesChange(changes);
  };
  const handleEdgesChange: typeof onEdgesChange = (changes) => {
    if (changes.some((ch) => ch.type !== "select")) markDirty();
    onEdgesChange(changes);
  };

  /* ---- node ops ---- */
  const addNodeAt = useCallback(
    (kind: string, position: { x: number; y: number }) => {
      const domainNode = newNode(kind, position);
      const rfNode: RFNode = {
        id: domainNode.id,
        type: domainNode.category,
        position,
        data: { node: domainNode },
      };
      dirty.current = true;
      setNodes((nds) => nds.concat(rfNode));
      setSelectedId(domainNode.id);
    },
    [setNodes],
  );

  const addNodeCenter = useCallback(
    (kind: string) => {
      addCount.current += 1;
      const offset = (addCount.current % 6) * 36;
      const el = wrapRef.current;
      const cx = el ? el.clientWidth / 2 : 300;
      const cy = el ? el.clientHeight / 2 : 200;
      const pos = rf.screenToFlowPosition
        ? rf.screenToFlowPosition({
            x: (el?.getBoundingClientRect().left ?? 0) + cx - 100 + offset,
            y: (el?.getBoundingClientRect().top ?? 0) + cy - 60 + offset,
          })
        : { x: cx + offset, y: cy + offset };
      addNodeAt(kind, pos);
    },
    [addNodeAt, rf],
  );

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      const kind = event.dataTransfer.getData(DND_KEY);
      if (!kind || !metaFor(kind)) return;
      const pos = rf.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      addNodeAt(kind, pos);
    },
    [addNodeAt, rf],
  );

  const onConnect = useCallback(
    (conn: Connection) => {
      if (!conn.source || !conn.target) return;
      dirty.current = true;
      setEdges((eds) => {
        const filtered = eds.filter(
          (e) => !(e.source === conn.source && e.sourceHandle === conn.sourceHandle),
        );
        const edge: RFEdge = {
          id: genId("e"),
          source: conn.source!,
          target: conn.target!,
          sourceHandle: conn.sourceHandle ?? "out",
          type: "smoothstep",
        };
        return filtered.concat(edge);
      });
    },
    [setEdges],
  );

  const onSelectionChange = useCallback((p: OnSelectionChangeParams) => {
    setSelectedId((p.nodes as Node[])[0]?.id ?? null);
  }, []);

  const updateNodeConfig = (id: string, cfg: Record<string, unknown>) => {
    markDirty();
    setNodes((nds) =>
      nds.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, node: { ...n.data.node, config: cfg } } } : n,
      ),
    );
  };
  const updateNodeTitle = (id: string, title: string) => {
    markDirty();
    setNodes((nds) =>
      nds.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, node: { ...n.data.node, title } } } : n,
      ),
    );
  };
  const deleteNode = (id: string) => {
    markDirty();
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
    setSelectedId(null);
  };

  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selectedId)?.data.node ?? null,
    [nodes, selectedId],
  );

  /* ---- rename ---- */
  const renameFlow = useMutation({
    mutationFn: (nm: string) => flowsApi.update(flowId, { name: nm }),
  });

  /* ---- test run ---- */
  const [testInput, setTestInput] = useState("");
  const testRun = useMutation({
    mutationFn: (): Promise<FlowTestResult> =>
      flowsApi.testRun(flowId, { graph: fromReactFlow(nodes, edges), input: testInput }),
    onError: () => message.error(t("common.operationFailed")),
  });

  /* ---- publish ---- */
  const publish = useMutation({
    mutationFn: () => flowsApi.publish(flowId, fromReactFlow(nodes, edges)),
    onSuccess: (res) => {
      if (res.ok) message.success(t("fe.published"));
      else message.warning(t("fe.validateErrors", { n: res.errors.length }));
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const localErrors = useMemo(() => validateGraph(graph), [graph]);

  const onPublish = () => {
    if (localErrors.length > 0) {
      message.warning(t("fe.validateErrors", { n: localErrors.length }));
      // still surface backend validation (which is authoritative)
    }
    publish.mutate();
  };

  const saveChip = () => {
    if (saveState === "saving")
      return (
        <span className="sc-fe-savechip">
          <LoadingOutlined spin /> {t("fe.saving")}
        </span>
      );
    if (saveState === "saved")
      return (
        <span className="sc-fe-savechip">
          <CloudSyncOutlined /> {t("fe.saved")}
        </span>
      );
    if (saveState === "error")
      return (
        <span className="sc-fe-savechip" style={{ color: "var(--sc-error)" }}>
          <ExclamationCircleOutlined /> {t("fe.saveFailed")}
        </span>
      );
    return <span className="sc-fe-savechip">{t("fe.autosave")}</span>;
  };

  if (flow.isLoading && !hydrated.current) {
    return (
      <div className="sc-fe" style={{ alignItems: "center", justifyContent: "center" }}>
        <Spin />
      </div>
    );
  }

  return (
    <div className="sc-fe">
      <div className="sc-fe-topbar">
        <Button type="text" icon={<ArrowLeftOutlined />} onClick={() => navigate("/automation")}>
          {t("fe.back")}
        </Button>
        <Input
          className="sc-fe-title-input"
          variant="borderless"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name.trim() && renameFlow.mutate(name.trim())}
          placeholder={t("fe.untitled")}
          style={{ maxWidth: 280 }}
        />
        {flow.data && (
          <Tag color={flow.data.published_version_id ? "green" : "orange"} style={{ marginInlineStart: 0 }}>
            {flow.data.published_version_id ? t("flow.status.published") : t("flow.status.draft")}
          </Tag>
        )}
        <div style={{ flex: 1 }} />
        {saveChip()}
        <Button icon={<ExperimentOutlined />} onClick={() => setTestOpen(true)}>
          {t("fe.test")}
        </Button>
        <Button type="primary" icon={<SaveOutlined />} loading={publish.isPending} onClick={onPublish}>
          {t("fe.publish")}
        </Button>
      </div>

      {flow.isError && (
        <Alert
          type="warning"
          banner
          showIcon
          message={t("fe.loadFailed")}
          description="後端流程接口尚未就緒，您仍可在此搭建並在接口上線後保存。"
        />
      )}

      <div className="sc-fe-main">
        <Palette onAdd={addNodeCenter} />

        <div
          className="sc-fe-canvas"
          ref={wrapRef}
          onDrop={onDrop}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
          }}
        >
          {nodes.length === 0 && <div className="sc-fe-empty-hint">{t("fe.dragHint")}</div>}
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={handleNodesChange}
            onEdgesChange={handleEdgesChange}
            onConnect={onConnect}
            onSelectionChange={onSelectionChange}
            nodeTypes={nodeTypes}
            defaultEdgeOptions={{ type: "smoothstep" }}
            fitView
            minZoom={0.2}
            maxZoom={2}
            deleteKeyCode={["Backspace", "Delete"]}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--sc-border)" />
            <Controls showInteractive={false} />
            <MiniMap
              pannable
              zoomable
              nodeColor={(n) => metaFor((n.data as RFNode["data"])?.node?.kind)?.accent ?? "#94A3B8"}
              style={{ background: "var(--sc-bg-subtle)" }}
            />
          </ReactFlow>
        </div>

        {selectedNode ? (
          <PropertyPanel
            key={selectedNode.id}
            node={selectedNode}
            onConfig={(cfg) => updateNodeConfig(selectedNode.id, cfg)}
            onTitle={(title) => updateNodeTitle(selectedNode.id, title)}
            onDelete={() => deleteNode(selectedNode.id)}
          />
        ) : (
          <aside className="sc-fe-props">
            <div className="sc-fe-props-head">
              <span style={{ fontWeight: 600, fontSize: 13.5 }}>{t("fe.props")}</span>
            </div>
            <div style={{ padding: 24 }}>
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("fe.noSelection")} />
              {localErrors.length > 0 && (
                <Alert
                  style={{ marginTop: 16 }}
                  type="warning"
                  showIcon
                  message={t("fe.validateErrors", { n: localErrors.length })}
                  description={
                    <ul style={{ margin: 0, paddingInlineStart: 18 }}>
                      {localErrors.slice(0, 6).map((e, i) => (
                        <li key={i} style={{ fontSize: 12 }}>
                          {e.message}
                        </li>
                      ))}
                    </ul>
                  }
                />
              )}
            </div>
          </aside>
        )}
      </div>

      <Drawer
        title={t("fe.testTitle")}
        open={testOpen}
        onClose={() => setTestOpen(false)}
        width={420}
      >
        <Alert type="info" showIcon message={t("fe.testSandboxHint")} style={{ marginBottom: 12 }} />
        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <Input
            value={testInput}
            onChange={(e) => setTestInput(e.target.value)}
            placeholder={t("fe.testInput")}
            onPressEnter={() => testRun.mutate()}
          />
          <Button type="primary" loading={testRun.isPending} onClick={() => testRun.mutate()}>
            {t("fe.testRun")}
          </Button>
        </div>

        {!testRun.data && !testRun.isPending && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("fe.testEmpty")} />
        )}

        {testRun.data && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div>
              {testRun.data.steps.map((s, i) => {
                const m = metaFor(s.kind);
                return (
                  <div key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "6px 0" }}>
                    <span className="sc-fn-chip" style={{ background: m?.accent ?? "#94A3B8", width: 22, height: 22, fontSize: 12 }}>
                      {m?.icon}
                    </span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 13, fontWeight: 600 }}>
                        {s.title}
                        {s.status === "ok" && <CheckCircleFilled style={{ color: "var(--sc-success)", marginInlineStart: 6, fontSize: 12 }} />}
                        {s.status === "waiting" && <Tag color="orange" style={{ marginInlineStart: 6 }}>{t("fe.port.timeout")}</Tag>}
                        {s.status === "error" && <ExclamationCircleOutlined style={{ color: "var(--sc-error)", marginInlineStart: 6 }} />}
                      </div>
                      {s.detail && <div style={{ fontSize: 12, color: "var(--sc-text-secondary)" }}>{s.detail}</div>}
                    </div>
                  </div>
                );
              })}
            </div>
            {(testRun.data.preview_messages ?? []).length > 0 && (
              <div>
                <div className="sc-fe-label">{t("fe.testTitle")}</div>
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {(testRun.data.preview_messages ?? []).map((m, i) => (
                    <div key={i} className="sc-bubble" style={{ maxWidth: "100%" }}>
                      <MessageBlocks content={m} />
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </div>
  );
}

export function FlowEditor() {
  const { flowId } = useParams<{ flowId: string }>();
  if (!flowId) {
    return <Result status="404" title={t("common.notFound")} />;
  }
  return (
    <ReactFlowProvider>
      <EditorCanvas flowId={flowId} />
    </ReactFlowProvider>
  );
}
