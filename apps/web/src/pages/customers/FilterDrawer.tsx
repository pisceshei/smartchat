/** 高級篩選 — dynamic predicate rows over base + custom fields. */
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { Button, Drawer, Input, Select, Space } from "antd";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { customFieldsApi } from "@/api/endpoints";
import type { FilterPredicate } from "@/api/types";
import { t } from "@/i18n";

const BASE_FIELDS: { value: string; label: string }[] = [
  { value: "display_name", label: t("cust.filter.field.name") },
  { value: "email", label: t("cust.filter.field.email") },
  { value: "phone", label: t("cust.filter.field.phone") },
  { value: "country", label: t("cust.filter.field.country") },
  { value: "city", label: t("cust.filter.field.city") },
  { value: "language", label: t("cust.filter.field.language") },
  { value: "tag", label: t("cust.filter.field.tag") },
  { value: "is_blacklisted", label: t("cust.filter.field.blacklist") },
  { value: "assignee", label: t("cust.filter.field.assignee") },
];

const OPERATORS: { value: FilterPredicate["op"]; label: string }[] = [
  { value: "contains", label: t("cust.filter.op.contains") },
  { value: "eq", label: t("cust.filter.op.eq") },
  { value: "neq", label: t("cust.filter.op.neq") },
  { value: "empty", label: t("cust.filter.op.empty") },
  { value: "not_empty", label: t("cust.filter.op.notEmpty") },
];

export function FilterDrawer({
  open,
  onClose,
  value,
  onApply,
}: {
  open: boolean;
  onClose: () => void;
  value: FilterPredicate[];
  onApply: (filters: FilterPredicate[]) => void;
}) {
  const [rows, setRows] = useState<FilterPredicate[]>(value);

  useEffect(() => {
    if (open) setRows(value.length > 0 ? value : [{ field: "display_name", op: "contains", value: "" }]);
  }, [open, value]);

  const customFields = useQuery({
    queryKey: ["custom-fields"],
    queryFn: () => customFieldsApi.list(),
    enabled: open,
    retry: 1,
  });

  const fieldOptions = [
    ...BASE_FIELDS,
    ...(customFields.data ?? []).map((f) => ({
      value: `custom.${f.key}`,
      label: `${f.label}（${t("cust.nav.customFields")}）`,
    })),
  ];

  const setRow = (i: number, patch: Partial<FilterPredicate>) => {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  };

  return (
    <Drawer
      title={t("cust.filter.title")}
      open={open}
      onClose={onClose}
      width={440}
      footer={
        <Space style={{ display: "flex", justifyContent: "flex-end" }}>
          <Button
            onClick={() => {
              setRows([]);
              onApply([]);
            }}
          >
            {t("common.reset")}
          </Button>
          <Button
            type="primary"
            onClick={() =>
              onApply(
                rows.filter(
                  (r) => r.field && (r.op === "empty" || r.op === "not_empty" || (r.value ?? "") !== ""),
                ),
              )
            }
          >
            {t("common.apply")}
          </Button>
        </Space>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {rows.map((row, i) => (
          <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <Select
              style={{ width: 140 }}
              size="middle"
              value={row.field}
              options={fieldOptions}
              onChange={(field) => setRow(i, { field })}
              showSearch
              optionFilterProp="label"
              aria-label={t("cust.filter.field")}
            />
            <Select
              style={{ width: 100 }}
              value={row.op}
              options={OPERATORS}
              onChange={(op) => setRow(i, { op })}
              aria-label={t("cust.filter.operator")}
            />
            {row.op !== "empty" && row.op !== "not_empty" && (
              <Input
                style={{ flex: 1 }}
                value={row.value ?? ""}
                onChange={(e) => setRow(i, { value: e.target.value })}
                placeholder={t("cust.filter.value")}
              />
            )}
            <Button
              type="text"
              danger
              icon={<DeleteOutlined />}
              onClick={() => setRows((prev) => prev.filter((_, idx) => idx !== i))}
              aria-label={t("common.delete")}
            />
          </div>
        ))}
        <Button
          type="dashed"
          icon={<PlusOutlined />}
          onClick={() => setRows((prev) => [...prev, { field: "display_name", op: "contains", value: "" }])}
        >
          {t("cust.filter.addCondition")}
        </Button>
      </div>
    </Drawer>
  );
}
