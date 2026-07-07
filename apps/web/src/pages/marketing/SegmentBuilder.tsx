/** 建立受眾 — AND/OR predicate builder over contact fields with a live
 *  audience-size estimate (segments/estimate, 5s statement timeout backend).
 *  Saves via segments.create; the resulting Segment id is handed back to the
 *  caller (broadcast wizard / EDM form). */
import { DeleteOutlined, PlusOutlined, TeamOutlined } from "@ant-design/icons";
import { App, Button, Input, Modal, Radio, Segmented, Select } from "antd";
import { useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { segmentsApi } from "@/api/endpoints";
import type {
  FilterPredicate,
  Segment,
  SegmentDefinition,
  SegmentMode,
} from "@/api/types";
import { t } from "@/i18n";

const FIELDS: { value: string; labelKey: Parameters<typeof t>[0] }[] = [
  { value: "display_name", labelKey: "seg.field.display_name" },
  { value: "email", labelKey: "seg.field.email" },
  { value: "phone", labelKey: "seg.field.phone" },
  { value: "country", labelKey: "seg.field.country" },
  { value: "city", labelKey: "seg.field.city" },
  { value: "language", labelKey: "seg.field.language" },
  { value: "tags", labelKey: "seg.field.tags" },
  { value: "remark", labelKey: "seg.field.remark" },
];

const OPS: { value: FilterPredicate["op"]; labelKey: Parameters<typeof t>[0] }[] = [
  { value: "contains", labelKey: "seg.op.contains" },
  { value: "eq", labelKey: "seg.op.eq" },
  { value: "neq", labelKey: "seg.op.neq" },
  { value: "empty", labelKey: "seg.op.empty" },
  { value: "not_empty", labelKey: "seg.op.not_empty" },
];

const needsValue = (op: FilterPredicate["op"]) => op !== "empty" && op !== "not_empty";

function newPredicate(): FilterPredicate {
  return { field: "display_name", op: "contains", value: "" };
}

export function SegmentBuilder({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (segment: Segment) => void;
}) {
  const { message } = App.useApp();
  const [name, setName] = useState("");
  const [mode, setMode] = useState<SegmentMode>("dynamic");
  const [logic, setLogic] = useState<"and" | "or">("and");
  const [predicates, setPredicates] = useState<FilterPredicate[]>([newPredicate()]);
  const [estimate, setEstimate] = useState<number | null>(null);

  const definition = useMemo<SegmentDefinition>(
    () => ({ logic, predicates: predicates.filter((p) => p.field) }),
    [logic, predicates],
  );

  const estimateMut = useMutation({
    mutationFn: () => segmentsApi.estimate(definition),
    onSuccess: (r) => setEstimate(r.count),
    onError: () => message.error(t("common.operationFailed")),
  });

  const createMut = useMutation({
    mutationFn: () => segmentsApi.create({ name: name.trim(), mode, definition }),
    onSuccess: (seg) => {
      message.success(t("seg.saved"));
      onCreated(seg);
      reset();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const reset = () => {
    setName("");
    setMode("dynamic");
    setLogic("and");
    setPredicates([newPredicate()]);
    setEstimate(null);
  };

  const patch = (i: number, next: Partial<FilterPredicate>) =>
    setPredicates((ps) => ps.map((p, idx) => (idx === i ? { ...p, ...next } : p)));

  return (
    <Modal
      title={t("seg.create")}
      open={open}
      onCancel={() => {
        reset();
        onClose();
      }}
      okText={t("common.create")}
      cancelText={t("common.cancel")}
      confirmLoading={createMut.isPending}
      onOk={() => {
        if (!name.trim()) {
          message.warning(t("seg.name"));
          return;
        }
        createMut.mutate();
      }}
      width={620}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingTop: 6 }}>
        <div>
          <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("seg.name")}</div>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={50} autoFocus />
        </div>
        <div>
          <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("seg.mode")}</div>
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            options={[
              { value: "dynamic", label: t("seg.mode.dynamic") },
              { value: "static", label: t("seg.mode.static") },
            ]}
          />
        </div>

        <div className="sc-seg-group">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--sc-text-secondary)" }}>
              {t("seg.definition")}
            </span>
            <Segmented
              size="small"
              value={logic}
              onChange={(v) => setLogic(v as "and" | "or")}
              options={[
                { value: "and", label: t("seg.logic.and") },
                { value: "or", label: t("seg.logic.or") },
              ]}
            />
          </div>

          {predicates.map((p, i) => (
            <div className="sc-seg-row" key={i}>
              <Select
                style={{ width: 130 }}
                value={p.field}
                onChange={(v) => patch(i, { field: v })}
                options={FIELDS.map((f) => ({ value: f.value, label: t(f.labelKey) }))}
              />
              <Select
                style={{ width: 100 }}
                value={p.op}
                onChange={(v) => patch(i, { op: v, value: needsValue(v) ? (p.value ?? "") : null })}
                options={OPS.map((o) => ({ value: o.value, label: t(o.labelKey) }))}
              />
              {needsValue(p.op) && (
                <Input
                  style={{ flex: 1 }}
                  value={p.value ?? ""}
                  onChange={(e) => patch(i, { value: e.target.value })}
                  placeholder={t("seg.value")}
                />
              )}
              <Button
                type="text"
                size="small"
                icon={<DeleteOutlined />}
                disabled={predicates.length === 1}
                onClick={() => setPredicates((ps) => ps.filter((_, idx) => idx !== i))}
              />
            </div>
          ))}

          <Button
            type="dashed"
            size="small"
            icon={<PlusOutlined />}
            onClick={() => setPredicates((ps) => [...ps, newPredicate()])}
          >
            {t("seg.addPredicate")}
          </Button>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <Button
            icon={<TeamOutlined />}
            loading={estimateMut.isPending}
            onClick={() => estimateMut.mutate()}
          >
            {t("seg.estimate")}
          </Button>
          {estimate != null && (
            <span style={{ color: "var(--sc-text-secondary)", fontSize: 13 }}>
              {t("seg.estimated", { count: estimate })}
            </span>
          )}
        </div>
      </div>
    </Modal>
  );
}
