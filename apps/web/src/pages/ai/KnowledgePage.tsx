/** 自動化 › 知識庫 — collections (left) + documents (right). Documents ingest
 *  from file upload / FAQ pairs / product import / URL / raw text; status
 *  chips reflect the pgvector ingest pipeline (plan 附錄 B.2). */
import {
  DatabaseOutlined,
  DeleteOutlined,
  PlusOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  App,
  Badge,
  Button,
  Input,
  Modal,
  Popconfirm,
  Segmented,
  Skeleton,
  Table,
  Tag,
  Upload,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KB_DOC_TYPES, kbApi } from "@/api/endpoints";
import type { KbDocType, KbDocument, KbFaqPair, KbIngestStatus } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { fullTime } from "@/utils/time";
import { subId } from "@/pages/automation/nodes";

const STATUS_META: Record<KbIngestStatus, { badge: "default" | "processing" | "success" | "error"; key: string }> = {
  pending: { badge: "default", key: "kb.status.pending" },
  processing: { badge: "processing", key: "kb.status.processing" },
  ready: { badge: "success", key: "kb.status.ready" },
  failed: { badge: "error", key: "kb.status.failed" },
};

function AddDocModal({
  open,
  collectionId,
  onClose,
}: {
  open: boolean;
  collectionId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [docType, setDocType] = useState<KbDocType>("faq");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [source, setSource] = useState("");
  const [pairs, setPairs] = useState<(KbFaqPair & { id: string })[]>([{ id: subId("faq"), question: "", answer: "" }]);

  const reset = () => {
    setTitle("");
    setText("");
    setUrl("");
    setSource("");
    setPairs([{ id: subId("faq"), question: "", answer: "" }]);
  };

  const done = () => {
    message.success(t("kb.docAdded"));
    void qc.invalidateQueries({ queryKey: ["kb-documents", collectionId] });
    void qc.invalidateQueries({ queryKey: ["kb-collections"] });
    reset();
    onClose();
  };

  const submit = useMutation({
    mutationFn: async () => {
      if (docType === "faq")
        return kbApi.addFaq(collectionId, {
          title: title || "FAQ",
          pairs: pairs.map(({ question, answer }) => ({ question, answer })).filter((p) => p.question),
        });
      if (docType === "url") return kbApi.addUrl(collectionId, { url });
      if (docType === "text") return kbApi.addText(collectionId, { title: title || "文字", text });
      if (docType === "product") return kbApi.importProducts(collectionId, { source: source || "catalog" });
      throw new Error("use uploader for file");
    },
    onSuccess: done,
    onError: () => message.error(t("common.operationFailed")),
  });

  const uploadFile = useMutation({
    mutationFn: (file: File) => kbApi.addFile(collectionId, file),
    onSuccess: done,
    onError: () => message.error(t("kb.uploadFailed")),
  });

  return (
    <Modal
      title={t("kb.addDoc")}
      open={open}
      onCancel={onClose}
      onOk={() => submit.mutate()}
      okText={t("common.add")}
      cancelText={t("common.cancel")}
      confirmLoading={submit.isPending}
      okButtonProps={{ style: { display: docType === "file" ? "none" : undefined } }}
      width={560}
    >
      <Segmented
        block
        style={{ marginBottom: 16 }}
        value={docType}
        onChange={(v) => setDocType(v as KbDocType)}
        options={KB_DOC_TYPES.map((d) => ({ value: d.type, label: d.label }))}
      />

      {docType === "file" && (
        <Upload.Dragger
          multiple
          showUploadList={false}
          customRequest={({ file, onSuccess }) => {
            void uploadFile.mutateAsync(file as File).then(() => onSuccess?.(null));
          }}
        >
          <p style={{ fontSize: 28, color: "var(--sc-primary)" }}>
            <UploadOutlined />
          </p>
          <p>{t("kb.add.file")}</p>
          <p style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>PDF / Word / Markdown / TXT</p>
        </Upload.Dragger>
      )}

      {docType === "faq" && (
        <div>
          <Input
            style={{ marginBottom: 10 }}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={t("kb.faq.docTitle")}
          />
          {pairs.map((p, i) => (
            <div key={p.id} className="sc-branch-row">
              <div className="sc-branch-row-head">
                <span>#{i + 1}</span>
                {pairs.length > 1 && (
                  <a onClick={() => setPairs((prev) => prev.filter((x) => x.id !== p.id))}>{t("common.delete")}</a>
                )}
              </div>
              <Input
                style={{ marginBottom: 6 }}
                value={p.question}
                onChange={(e) => setPairs((prev) => prev.map((x) => (x.id === p.id ? { ...x, question: e.target.value } : x)))}
                placeholder={t("kb.faq.question")}
              />
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                value={p.answer}
                onChange={(e) => setPairs((prev) => prev.map((x) => (x.id === p.id ? { ...x, answer: e.target.value } : x)))}
                placeholder={t("kb.faq.answer")}
              />
            </div>
          ))}
          <Button type="dashed" block icon={<PlusOutlined />} onClick={() => setPairs((prev) => [...prev, { id: subId("faq"), question: "", answer: "" }])}>
            {t("kb.faq.addPair")}
          </Button>
        </div>
      )}

      {docType === "url" && (
        <div>
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" />
          <div className="sc-fe-hint">{t("kb.url.hint")}</div>
        </div>
      )}

      {docType === "text" && (
        <div>
          <Input style={{ marginBottom: 10 }} value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("kb.text.title")} />
          <Input.TextArea autoSize={{ minRows: 6, maxRows: 14 }} value={text} onChange={(e) => setText(e.target.value)} placeholder={t("kb.text.content")} />
        </div>
      )}

      {docType === "product" && (
        <div>
          <Input value={source} onChange={(e) => setSource(e.target.value)} placeholder={t("kb.product.source")} />
          <div className="sc-fe-hint">{t("kb.product.hint")}</div>
        </div>
      )}
    </Modal>
  );
}

export function KnowledgePage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);

  const collections = useQuery({
    queryKey: ["kb-collections"],
    queryFn: () => kbApi.collections(),
    retry: 1,
  });
  const documents = useQuery({
    queryKey: ["kb-documents", activeId],
    queryFn: () => kbApi.documents(activeId as string),
    enabled: !!activeId,
    retry: 1,
    refetchInterval: (q) =>
      (q.state.data ?? []).some((d) => d.status === "pending" || d.status === "processing") ? 4000 : false,
  });

  const createCol = useMutation({
    mutationFn: (name: string) => kbApi.createCollection({ name }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["kb-collections"] });
      setActiveId(c.id);
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const removeCol = useMutation({
    mutationFn: (id: string) => kbApi.removeCollection(id),
    onSuccess: (_r, id) => {
      void qc.invalidateQueries({ queryKey: ["kb-collections"] });
      if (activeId === id) setActiveId(null);
    },
    onError: () => message.error(t("common.operationFailed")),
  });
  const reingest = useMutation({
    mutationFn: (docId: string) => kbApi.reingest(activeId as string, docId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kb-documents", activeId] }),
    onError: () => message.error(t("common.operationFailed")),
  });
  const removeDoc = useMutation({
    mutationFn: (docId: string) => kbApi.removeDocument(activeId as string, docId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["kb-documents", activeId] });
      void qc.invalidateQueries({ queryKey: ["kb-collections"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const promptCreate = () => {
    let val = "";
    modal.confirm({
      title: t("kb.addCollection"),
      icon: <DatabaseOutlined />,
      content: <Input placeholder={t("kb.collectionName")} onChange={(e) => (val = e.target.value)} autoFocus />,
      okText: t("common.create"),
      cancelText: t("common.cancel"),
      onOk: () => val.trim() && createCol.mutate(val.trim()),
    });
  };

  const typeLabel = (ty: KbDocType) => KB_DOC_TYPES.find((d) => d.type === ty)?.label ?? ty;

  const columns: ColumnsType<KbDocument> = [
    { title: t("kb.doc.title"), dataIndex: "title", render: (v: string) => <span style={{ fontWeight: 500 }}>{v}</span> },
    { title: t("kb.doc.type"), dataIndex: "doc_type", width: 100, render: (v: KbDocType) => <Tag>{typeLabel(v)}</Tag> },
    {
      title: t("kb.doc.status"),
      dataIndex: "status",
      width: 120,
      render: (v: KbIngestStatus, r) => (
        <Badge status={STATUS_META[v].badge} text={<span style={{ fontSize: 12.5 }}>{t(STATUS_META[v].key as never)}{r.error ? `：${r.error}` : ""}</span>} />
      ),
    },
    { title: t("kb.doc.chunks"), dataIndex: "chunk_count", width: 80, align: "right", render: (v?: number) => v ?? 0 },
    { title: t("kb.doc.updated"), dataIndex: "updated_at", width: 150, render: (v?: string) => <span style={{ fontSize: 12.5, color: "var(--sc-text-secondary)" }}>{fullTime(v)}</span> },
    {
      title: t("common.actions"),
      width: 120,
      render: (_, r) => (
        <div style={{ display: "flex", gap: 2 }}>
          <Button type="text" size="small" icon={<ReloadOutlined />} onClick={() => reingest.mutate(r.id)}>
            {t("kb.reingest")}
          </Button>
          <Popconfirm
            title={t("common.confirmDeleteTitle")}
            okText={t("common.delete")}
            cancelText={t("common.cancel")}
            onConfirm={() => removeDoc.mutate(r.id)}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </div>
      ),
    },
  ];

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("kb.title")}</h1>
        {activeId && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
            {t("kb.addDoc")}
          </Button>
        )}
      </div>
      <div className="sc-page-body" style={{ display: "flex", gap: 16 }}>
        <div style={{ flex: "none", width: 220 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--sc-text-tertiary)" }}>{t("kb.collections")}</span>
            <Button type="text" size="small" icon={<PlusOutlined />} onClick={promptCreate} />
          </div>
          {collections.isLoading ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : (collections.data ?? []).length === 0 ? (
            <EmptyState compact icon={<DatabaseOutlined />} title={t("kb.empty")} hint={t("kb.emptyHint")} />
          ) : (
            (collections.data ?? []).map((c) => (
              <button
                key={c.id}
                type="button"
                className={`sc-view-item${activeId === c.id ? " sc-active" : ""}`}
                onClick={() => setActiveId(c.id)}
              >
                <DatabaseOutlined />
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</span>
                <span className="sc-view-count">{c.document_count ?? 0}</span>
                <Popconfirm
                  title={t("common.confirmDeleteTitle")}
                  okText={t("common.delete")}
                  cancelText={t("common.cancel")}
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    removeCol.mutate(c.id);
                  }}
                >
                  <DeleteOutlined onClick={(e) => e.stopPropagation()} style={{ color: "var(--sc-text-tertiary)", fontSize: 12 }} />
                </Popconfirm>
              </button>
            ))
          )}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          {!activeId ? (
            <EmptyState icon={<DatabaseOutlined />} title={t("kb.selectCollection")} />
          ) : documents.isLoading ? (
            <Skeleton active paragraph={{ rows: 5 }} />
          ) : (documents.data ?? []).length === 0 ? (
            <EmptyState
              icon={<DatabaseOutlined />}
              title={t("kb.doc.empty")}
              action={
                <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
                  {t("kb.addDoc")}
                </Button>
              }
            />
          ) : (
            <Table<KbDocument> rowKey="id" size="small" columns={columns} dataSource={documents.data} pagination={false} />
          )}
        </div>
      </div>

      {activeId && <AddDocModal open={addOpen} collectionId={activeId} onClose={() => setAddOpen(false)} />}
    </div>
  );
}
