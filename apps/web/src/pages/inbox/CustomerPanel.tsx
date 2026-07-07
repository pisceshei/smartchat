/** Inbox pane 4 — customer info: contact / basic info / visitor tags /
 *  duplicates (ONE ID) / conversation info / audit log / orders. */
import {
  AuditOutlined,
  IdcardOutlined,
  InfoCircleOutlined,
  ShoppingOutlined,
  TagsOutlined,
  UserOutlined,
  UsergroupAddOutlined,
} from "@ant-design/icons";
import { App, Avatar, Button, Collapse, Input, Segmented, Select, Skeleton, Tag } from "antd";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { contactsApi, tagsApi } from "@/api/endpoints";
import type { Conversation } from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { fullTime, tzNow } from "@/utils/time";

function InfoRow({ label, value }: { label: string; value?: React.ReactNode }) {
  return (
    <div className="sc-info-row">
      <span className="sc-info-label">{label}</span>
      <span className="sc-info-value">{value || <span className="sc-text-tertiary">-</span>}</span>
    </div>
  );
}

function DuplicatesSection({ contactId }: { contactId: string }) {
  const [status, setStatus] = useState<"suggested" | "linked" | undefined>(undefined);
  const qc = useQueryClient();
  const { message } = App.useApp();
  const { data, isLoading } = useQuery({
    queryKey: ["merge-candidates", contactId, status ?? "all"],
    queryFn: () => contactsApi.mergeCandidates(contactId, status),
    retry: 1,
  });

  const act = useMutation({
    mutationFn: (vars: { id: string; action: "merge" | "dismiss" }) =>
      vars.action === "merge" ? contactsApi.merge(vars.id) : contactsApi.dismissMerge(vars.id),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["merge-candidates", contactId] });
      void qc.invalidateQueries({ queryKey: ["contact", contactId] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <div>
      <Segmented
        size="small"
        block
        value={status ?? "all"}
        onChange={(v) => setStatus(v === "all" ? undefined : (v as "suggested" | "linked"))}
        options={[
          { label: t("inbox.cust.dup.linked"), value: "linked" },
          { label: t("inbox.cust.dup.unlinked"), value: "suggested" },
          { label: t("inbox.cust.dup.all"), value: "all" },
        ]}
        style={{ marginBottom: 8 }}
      />
      {isLoading ? (
        <Skeleton active title={false} paragraph={{ rows: 2 }} />
      ) : (data ?? []).length === 0 ? (
        <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", padding: "4px 0" }}>
          {t("inbox.cust.dup.empty")}
        </div>
      ) : (
        (data ?? []).map((cand) => (
          <div
            key={cand.id}
            style={{
              border: "1px solid var(--sc-border)",
              borderRadius: 8,
              padding: 8,
              marginBottom: 6,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Avatar size={24}>{(cand.duplicate_contact.display_name ?? "?").slice(0, 1)}</Avatar>
              <span style={{ fontSize: 13, fontWeight: 500 }}>
                {cand.duplicate_contact.display_name ?? t("inbox.cust.unnamed")}
              </span>
              <Tag style={{ marginLeft: "auto", fontSize: 10 }}>{cand.match_field}</Tag>
            </div>
            {cand.status === "suggested" && (
              <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  onClick={() => act.mutate({ id: cand.id, action: "merge" })}
                >
                  {t("inbox.cust.dup.merge")}
                </Button>
                <Button size="small" onClick={() => act.mutate({ id: cand.id, action: "dismiss" })}>
                  {t("inbox.cust.dup.dismiss")}
                </Button>
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}

export function CustomerPanel({ conversation }: { conversation: Conversation }) {
  const contactId = conversation.contact_id;
  const qc = useQueryClient();
  const { message } = App.useApp();

  const contact = useQuery({
    queryKey: ["contact", contactId],
    queryFn: () => contactsApi.get(contactId),
    retry: 1,
  });

  const visitorTags = useQuery({
    queryKey: ["tags", "visitor"],
    queryFn: () => tagsApi.list("visitor"),
    staleTime: 60_000,
    retry: 1,
  });

  const audit = useQuery({
    queryKey: ["contact-activities", contactId],
    queryFn: () => contactsApi.activities(contactId),
    retry: 0,
  });

  const orders = useQuery({
    queryKey: ["contact-orders", contactId],
    queryFn: () => contactsApi.orders(contactId),
    retry: 0,
  });

  const setTags = useMutation({
    mutationFn: (tagIds: string[]) => contactsApi.setTags(contactId, tagIds),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["contact", contactId] }),
    onError: () => message.error(t("common.operationFailed")),
  });

  const saveRemark = useMutation({
    mutationFn: (remark_name: string) => contactsApi.update(contactId, { remark_name }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["contact", contactId] });
    },
  });

  const c = contact.data;
  const tagOptions = useMemo(
    () => (visitorTags.data ?? []).map((tg) => ({ value: tg.id, label: tg.name })),
    [visitorTags.data],
  );

  if (contact.isLoading) {
    return (
      <aside className="sc-cust-panel" style={{ padding: 16 }}>
        <Skeleton active avatar paragraph={{ rows: 8 }} />
      </aside>
    );
  }

  return (
    <aside className="sc-cust-panel" aria-label={t("inbox.cust.contactInfo")}>
      <Collapse
        ghost
        defaultActiveKey={["contact", "basic", "tags", "conv"]}
        expandIconPosition="end"
        items={[
          {
            key: "contact",
            label: (
              <b>
                <UserOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.contactInfo")}
              </b>
            ),
            children: (
              <div>
                <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8 }}>
                  <Avatar size={44} src={c?.avatar_url ?? undefined} style={{ background: "var(--sc-primary-bg-strong)", color: "var(--sc-primary)", fontWeight: 600 }}>
                    {(c?.display_name ?? "?").slice(0, 1).toUpperCase()}
                  </Avatar>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{c?.display_name ?? t("inbox.cust.unnamed")}</div>
                    <div style={{ fontSize: 12, color: "var(--sc-text-tertiary)" }}>
                      {t("inbox.cust.oneId")}: <span className="sc-mono">{c?.one_id ?? c?.id.slice(0, 8)}</span>
                    </div>
                  </div>
                </div>
                <InfoRow
                  label={t("inbox.cust.remarkName")}
                  value={
                    <Input
                      size="small"
                      defaultValue={c?.remark_name ?? ""}
                      placeholder="-"
                      variant="filled"
                      onBlur={(e) => {
                        if (e.target.value !== (c?.remark_name ?? "")) saveRemark.mutate(e.target.value);
                      }}
                    />
                  }
                />
                <InfoRow
                  label={t("inbox.cust.channel")}
                  value={
                    <span style={{ display: "inline-flex", gap: 4 }}>
                      {(c?.channel_identities ?? []).map((ci) => (
                        <ChannelIcon key={ci.id} type={ci.channel_type} size={16} />
                      ))}
                    </span>
                  }
                />
              </div>
            ),
          },
          {
            key: "basic",
            label: (
              <b>
                <InfoCircleOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.basicInfo")}
              </b>
            ),
            children: (
              <div>
                <InfoRow label={t("inbox.cust.phone")} value={c?.phone} />
                <InfoRow label={t("inbox.cust.email")} value={c?.email} />
                <InfoRow label={t("inbox.cust.language")} value={c?.language} />
                <InfoRow
                  label={t("inbox.cust.country")}
                  value={[c?.country, c?.city].filter(Boolean).join(" · ")}
                />
                <InfoRow label={t("inbox.cust.localTime")} value={tzNow(c?.timezone)} />
                <InfoRow label={t("inbox.cust.ip")} value={c?.last_ip} />
                <InfoRow label={t("inbox.cust.browser")} value={c?.browser} />
                <InfoRow label={t("inbox.cust.device")} value={c?.device} />
              </div>
            ),
          },
          {
            key: "tags",
            label: (
              <b>
                <TagsOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.visitorTags")}
              </b>
            ),
            children: (
              <Select
                mode="multiple"
                size="small"
                style={{ width: "100%" }}
                placeholder={t("inbox.cust.addTag")}
                options={tagOptions}
                value={(c?.tags ?? []).map((tg) => tg.id)}
                onChange={(ids) => setTags.mutate(ids)}
                loading={setTags.isPending}
              />
            ),
          },
          {
            key: "dup",
            label: (
              <b>
                <UsergroupAddOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.duplicates")}
              </b>
            ),
            children: <DuplicatesSection contactId={contactId} />,
          },
          {
            key: "conv",
            label: (
              <b>
                <IdcardOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.convInfo")}
              </b>
            ),
            children: (
              <div>
                <InfoRow
                  label={t("inbox.cust.convId")}
                  value={<span className="sc-mono" style={{ fontSize: 12 }}>{conversation.id.slice(0, 13)}…</span>}
                />
                <InfoRow label={t("inbox.cust.assignee")} value={conversation.assignee_name} />
                <InfoRow
                  label={t("inbox.cust.convTags")}
                  value={
                    (conversation.tags ?? []).length > 0 ? (
                      <span>
                        {(conversation.tags ?? []).map((tg) => (
                          <Tag key={tg.id} color={tg.color} style={{ fontSize: 11 }}>
                            {tg.name}
                          </Tag>
                        ))}
                      </span>
                    ) : undefined
                  }
                />
                <InfoRow label={t("inbox.cust.convRemark")} value={conversation.remark} />
              </div>
            ),
          },
          {
            key: "audit",
            label: (
              <b>
                <AuditOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.auditLog")}
              </b>
            ),
            children:
              (audit.data ?? []).length === 0 ? (
                <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)" }}>
                  {t("inbox.cust.auditEmpty")}
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {(audit.data ?? []).slice(0, 20).map((a) => (
                    <div key={a.id} style={{ fontSize: 12.5 }}>
                      <div>
                        <b>{a.actor_name}</b> {a.action}
                        {a.detail ? `：${a.detail}` : ""}
                      </div>
                      <div style={{ color: "var(--sc-text-tertiary)", fontSize: 11.5 }}>
                        {fullTime(a.created_at)}
                      </div>
                    </div>
                  ))}
                </div>
              ),
          },
          {
            key: "orders",
            label: (
              <b>
                <ShoppingOutlined style={{ marginRight: 6 }} />
                {t("inbox.cust.orders")}
              </b>
            ),
            children:
              (orders.data ?? []).length === 0 ? (
                <EmptyState compact icon={<ShoppingOutlined />} title={t("inbox.cust.ordersEmpty")} />
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {(orders.data ?? []).map((o) => (
                    <div
                      key={o.id}
                      style={{ border: "1px solid var(--sc-border)", borderRadius: 8, padding: 8 }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}>
                        <span className="sc-mono">{o.order_no}</span>
                        <Tag style={{ fontSize: 10 }}>{o.status}</Tag>
                      </div>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }}>
                        <span style={{ color: "var(--sc-text-tertiary)" }}>{fullTime(o.created_at)}</span>
                        <b>
                          {o.currency} {o.total}
                        </b>
                      </div>
                    </div>
                  ))}
                </div>
              ),
          },
        ]}
      />
    </aside>
  );
}
