/** 自動化 › 詞庫 — keyword dictionaries reused by the 訪客發消息 trigger's
 *  "import from 詞庫". Left: dictionary list; right: newline/comma-separated
 *  keyword editor. */
import { DeleteOutlined, PlusOutlined, ReadOutlined, SaveOutlined } from "@ant-design/icons";
import { App, Button, Input, Popconfirm, Skeleton } from "antd";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { keywordDictsApi } from "@/api/endpoints";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";

export function KeywordDictsPage() {
  const qc = useQueryClient();
  const { message, modal } = App.useApp();
  const [activeId, setActiveId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  const dicts = useQuery({
    queryKey: ["keyword-dicts"],
    queryFn: () => keywordDictsApi.list(),
    retry: 1,
  });

  const items = useQuery({
    queryKey: ["keyword-dict-items", activeId],
    queryFn: () => keywordDictsApi.items(activeId as string),
    enabled: !!activeId,
    retry: 1,
  });

  useEffect(() => {
    if (items.data) setDraft(items.data.map((i) => i.keyword).join("\n"));
  }, [items.data]);

  const create = useMutation({
    mutationFn: (name: string) => keywordDictsApi.create({ name }),
    onSuccess: (d) => {
      void qc.invalidateQueries({ queryKey: ["keyword-dicts"] });
      setActiveId(d.id);
      setDraft("");
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const remove = useMutation({
    mutationFn: (id: string) => keywordDictsApi.remove(id),
    onSuccess: (_r, id) => {
      void qc.invalidateQueries({ queryKey: ["keyword-dicts"] });
      if (activeId === id) setActiveId(null);
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const save = useMutation({
    mutationFn: () => {
      const keywords = draft
        .split(/[\n,，]/)
        .map((s) => s.trim())
        .filter(Boolean);
      return keywordDictsApi.setItems(activeId as string, keywords);
    },
    onSuccess: () => {
      message.success(t("kw.saved"));
      void qc.invalidateQueries({ queryKey: ["keyword-dicts"] });
      void qc.invalidateQueries({ queryKey: ["keyword-dict-items", activeId] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const promptCreate = () => {
    let val = "";
    modal.confirm({
      title: t("kw.add"),
      icon: <ReadOutlined />,
      content: <Input placeholder={t("kw.name")} onChange={(e) => (val = e.target.value)} autoFocus />,
      okText: t("common.create"),
      cancelText: t("common.cancel"),
      onOk: () => val.trim() && create.mutate(val.trim()),
    });
  };

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("kw.title")}</h1>
        <Button type="primary" icon={<PlusOutlined />} onClick={promptCreate}>
          {t("kw.add")}
        </Button>
      </div>
      <div className="sc-page-body" style={{ display: "flex", gap: 16 }}>
        <div style={{ flex: "none", width: 220 }}>
          {dicts.isLoading ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : (dicts.data ?? []).length === 0 ? (
            <EmptyState compact icon={<ReadOutlined />} title={t("kw.empty")} hint={t("kw.emptyHint")} />
          ) : (
            (dicts.data ?? []).map((d) => (
              <button
                key={d.id}
                type="button"
                className={`sc-view-item${activeId === d.id ? " sc-active" : ""}`}
                onClick={() => setActiveId(d.id)}
              >
                <ReadOutlined />
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>{d.name}</span>
                <span className="sc-view-count">{d.item_count ?? 0}</span>
                <Popconfirm
                  title={t("common.confirmDeleteTitle")}
                  okText={t("common.delete")}
                  cancelText={t("common.cancel")}
                  onConfirm={(e) => {
                    e?.stopPropagation();
                    remove.mutate(d.id);
                  }}
                >
                  <DeleteOutlined
                    onClick={(e) => e.stopPropagation()}
                    style={{ color: "var(--sc-text-tertiary)", fontSize: 12 }}
                  />
                </Popconfirm>
              </button>
            ))
          )}
        </div>

        <div style={{ flex: 1, minWidth: 0, maxWidth: 640 }}>
          {!activeId ? (
            <EmptyState icon={<ReadOutlined />} title={t("kw.selectOne")} />
          ) : (
            <div>
              <label className="sc-fe-label">{t("kw.words")}</label>
              <Input.TextArea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                autoSize={{ minRows: 10, maxRows: 24 }}
                placeholder={t("kw.wordsHint")}
              />
              <div className="sc-fe-hint" style={{ marginBottom: 12 }}>
                {t("kw.wordsHint")}
              </div>
              <Button type="primary" icon={<SaveOutlined />} loading={save.isPending} onClick={() => save.mutate()}>
                {t("common.save")}
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
