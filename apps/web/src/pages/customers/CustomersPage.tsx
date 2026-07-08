/** 客戶列表 — server-paginated table, advanced filter drawer, column settings,
 *  contact detail drawer, export. */
import {
  ContactsOutlined,
  DownloadOutlined,
  FilterOutlined,
  PlusOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  App,
  Avatar,
  Badge,
  Button,
  Checkbox,
  Form,
  Input,
  Modal,
  Popover,
  Table,
  Tag,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { contactsApi } from "@/api/endpoints";
import type { Contact, FilterPredicate } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { listTime } from "@/utils/time";
import { ContactDrawer } from "./ContactDrawer";
import { FilterDrawer } from "./FilterDrawer";

const ALL_COLUMNS = [
  { key: "name", label: t("cust.col.name"), fixed: true },
  { key: "one_id", label: t("cust.col.oneId") },
  { key: "channels", label: t("cust.col.channels") },
  { key: "assignee", label: t("cust.col.assignee") },
  { key: "email", label: t("cust.col.email") },
  { key: "tags", label: t("cust.col.tags") },
  { key: "last_active", label: t("cust.col.lastActive") },
] as const;

type ColKey = (typeof ALL_COLUMNS)[number]["key"];

const COL_STORE_KEY = "smartchat.customers.columns";

function loadVisibleCols(): ColKey[] {
  try {
    const raw = localStorage.getItem(COL_STORE_KEY);
    if (raw) return JSON.parse(raw) as ColKey[];
  } catch {
    /* default below */
  }
  return ALL_COLUMNS.map((c) => c.key);
}

export function CustomersPage() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const qc = useQueryClient();

  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [q, setQ] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [filters, setFilters] = useState<FilterPredicate[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [visibleCols, setVisibleCols] = useState<ColKey[]>(loadVisibleCols);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addForm] = Form.useForm();

  const query = useQuery({
    queryKey: ["contacts", { page, pageSize, q, filters }],
    queryFn: () => contactsApi.list({ page, page_size: pageSize, q, filters }),
    placeholderData: keepPreviousData,
    retry: 1,
  });

  const exportMut = useMutation({
    mutationFn: () => contactsApi.export({ q, filters }),
    onSuccess: () => message.success(t("cust.exportStarted")),
    onError: () => message.error(t("common.operationFailed")),
  });

  const createMut = useMutation({
    mutationFn: (values: { display_name: string; email?: string; phone?: string }) =>
      contactsApi.create(values),
    onSuccess: () => {
      message.success(t("common.createSuccess"));
      setAddOpen(false);
      addForm.resetFields();
      void qc.invalidateQueries({ queryKey: ["contacts"] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const saveCols = (cols: ColKey[]) => {
    setVisibleCols(cols);
    localStorage.setItem(COL_STORE_KEY, JSON.stringify(cols));
  };

  const columns = useMemo<ColumnsType<Contact>>(() => {
    const defs: Record<ColKey, ColumnsType<Contact>[number]> = {
      name: {
        title: t("cust.col.name"),
        key: "name",
        fixed: "left",
        width: 220,
        render: (_, c) => (
          <span
            className="sc-clickable"
            style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
            onClick={() => setDetailId(c.id)}
          >
            <Avatar size={30} src={c.avatar_url ?? undefined} style={{ background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontWeight: 600 }}>
              {(c.display_name ?? "?").slice(0, 1).toUpperCase()}
            </Avatar>
            <span>
              <span style={{ fontWeight: 500, color: "var(--sc-primary)" }}>
                {c.display_name ?? t("inbox.cust.unnamed")}
              </span>
              {c.is_blacklisted && (
                <Tag color="red" style={{ marginLeft: 6, fontSize: 10 }}>
                  {t("cust.blacklisted")}
                </Tag>
              )}
            </span>
          </span>
        ),
      },
      one_id: {
        title: t("cust.col.oneId"),
        key: "one_id",
        width: 130,
        render: (_, c) => (
          <span className="sc-mono" style={{ fontSize: 12 }}>
            {c.one_id ?? c.id.slice(0, 8)}
          </span>
        ),
      },
      channels: {
        title: t("cust.col.channels"),
        key: "channels",
        width: 140,
        render: (_, c) => {
          const idents = c.channel_identities ?? [];
          return (
            <span style={{ display: "inline-flex", gap: 4 }}>
              {idents.slice(0, 5).map((ci) => (
                <ChannelIcon key={ci.id} type={ci.channel_type} size={16} />
              ))}
              {idents.length > 5 && (
                <Badge count={`+${idents.length - 5}`} color="var(--sc-text-tertiary)" />
              )}
            </span>
          );
        },
      },
      assignee: {
        title: t("cust.col.assignee"),
        key: "assignee",
        width: 120,
        render: (_, c) => c.assignee_name ?? <span className="sc-text-tertiary">-</span>,
      },
      email: {
        title: t("cust.col.email"),
        key: "email",
        width: 200,
        render: (_, c) => c.email ?? <span className="sc-text-tertiary">-</span>,
      },
      tags: {
        title: t("cust.col.tags"),
        key: "tags",
        width: 180,
        render: (_, c) => {
          const tags = c.tags ?? [];
          return (
            <span>
              {tags.slice(0, 3).map((tg) => (
                <Tag key={tg.id} color={tg.color} style={{ fontSize: 11 }}>
                  {tg.name}
                </Tag>
              ))}
              {tags.length > 3 && <Tag style={{ fontSize: 11 }}>+{tags.length - 3}</Tag>}
            </span>
          );
        },
      },
      last_active: {
        title: t("cust.col.lastActive"),
        key: "last_active",
        width: 110,
        render: (_, c) => (
          <span className="sc-text-secondary">{listTime(c.last_active_at) || "-"}</span>
        ),
      },
    };
    const cols = ALL_COLUMNS.filter((c) => visibleCols.includes(c.key)).map((c) => defs[c.key]);
    cols.push({
      title: t("common.actions"),
      key: "actions",
      fixed: "right",
      width: 130,
      render: (_, c) => (
        <span>
          <Button type="link" size="small" onClick={() => setDetailId(c.id)}>
            {t("common.detail")}
          </Button>
          <Button type="link" size="small" onClick={() => navigate("/inbox")}>
            {t("cust.chat")}
          </Button>
        </span>
      ),
    });
    return cols;
  }, [visibleCols, navigate]);

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("cust.nav.list")}</h1>
        <div style={{ display: "flex", gap: 8 }}>
          <Input
            allowClear
            style={{ width: 240 }}
            prefix={<SearchOutlined style={{ color: "var(--sc-text-tertiary)" }} />}
            placeholder={t("common.search")}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onPressEnter={() => {
              setQ(searchInput);
              setPage(1);
            }}
          />
          <Badge count={filters.length} size="small">
            <Button icon={<FilterOutlined />} onClick={() => setFilterOpen(true)}>
              {t("cust.filter.title")}
            </Button>
          </Badge>
          <Popover
            trigger="click"
            placement="bottomRight"
            content={
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {ALL_COLUMNS.map((c) => (
                  <Checkbox
                    key={c.key}
                    checked={visibleCols.includes(c.key)}
                    disabled={c.key === "name"}
                    onChange={(e) =>
                      saveCols(
                        e.target.checked
                          ? [...visibleCols, c.key]
                          : visibleCols.filter((k) => k !== c.key),
                      )
                    }
                  >
                    {c.label}
                  </Checkbox>
                ))}
              </div>
            }
          >
            <Button icon={<SettingOutlined />}>{t("cust.columnSettings")}</Button>
          </Popover>
          <Button
            icon={<DownloadOutlined />}
            onClick={() => exportMut.mutate()}
            loading={exportMut.isPending}
          >
            {t("common.export")}
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setAddOpen(true)}>
            {t("cust.addCustomer")}
          </Button>
        </div>
      </div>

      <div className="sc-page-body">
        <Table<Contact>
          rowKey="id"
          size="middle"
          columns={columns}
          dataSource={query.data?.items ?? []}
          loading={query.isLoading}
          scroll={{ x: 1100 }}
          locale={{
            emptyText: (
              <EmptyState
                icon={<ContactsOutlined />}
                title={t("cust.empty")}
                hint={t("cust.emptyHint")}
              />
            ),
          }}
          pagination={{
            current: page,
            pageSize,
            total: query.data?.total ?? 0,
            showSizeChanger: true,
            showTotal: (total) => `${total}`,
            onChange: (p, ps) => {
              setPage(p);
              setPageSize(ps);
            },
          }}
        />
      </div>

      <FilterDrawer
        open={filterOpen}
        onClose={() => setFilterOpen(false)}
        value={filters}
        onApply={(f) => {
          setFilters(f);
          setPage(1);
          setFilterOpen(false);
        }}
      />

      <ContactDrawer contactId={detailId} onClose={() => setDetailId(null)} />

      <Modal
        title={t("cust.addCustomer")}
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        onOk={() => addForm.submit()}
        okText={t("common.create")}
        cancelText={t("common.cancel")}
        confirmLoading={createMut.isPending}
        destroyOnHidden
      >
        <Form form={addForm} layout="vertical" onFinish={(v) => createMut.mutate(v)}>
          <Form.Item
            name="display_name"
            label={t("cust.col.name")}
            rules={[{ required: true, message: t("common.required") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="email"
            label={t("cust.col.email")}
            rules={[{ type: "email", message: t("auth.emailInvalid") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="phone" label={t("cust.filter.field.phone")}>
            <Input />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
