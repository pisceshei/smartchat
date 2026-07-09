/** 建立群發計畫 wizard — 6 steps replicating the SaleSmartly flow:
 *  推送管道 → 選受眾 → 選/建範本+變量映射 → 發送規則 → 排程 → 確認.
 *  Calls POST /broadcasts with the assembled body. */
import { PlusOutlined } from "@ant-design/icons";
import {
  App,
  Button,
  DatePicker,
  Input,
  InputNumber,
  Modal,
  Radio,
  Select,
  Steps,
  Tag,
  TimePicker,
} from "antd";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  broadcastsApi,
  channelsApi,
  msgTemplatesApi,
  segmentsApi,
  type BroadcastCreateBody,
} from "@/api/endpoints";
import type {
  BroadcastSchedule,
  MsgTemplate,
  Segment,
  TemplateChannel,
  WhatsAppTemplate,
} from "@/api/types";
import { ChannelIcon } from "@/components/ChannelIcon";
import { CHANNEL_NAME, galleryType } from "@/constants/channels";
import { t } from "@/i18n";
import { dayjs, hhmmToMin, minToHHmm } from "@/utils/time";
import { SegmentBuilder } from "./SegmentBuilder";

/** Broadcast-capable channels (plan B.3: 8 渠道). */
const BROADCAST_CHANNELS = [
  "whatsapp_api",
  "email",
  "messenger",
  "whatsapp_app",
  "telegram_bot",
  "instagram",
  "line_oa",
  "widget",
] as const;

/** Map a broadcast channel to its template channel (only 4 exist). */
function templateChannelFor(ch: string): TemplateChannel | null {
  if (ch === "whatsapp_api" || ch === "whatsapp_app") return "whatsapp";
  if (ch === "email") return "email";
  if (ch === "messenger") return "messenger";
  return null;
}

/** WhatsApp interval floor is tighter (3–120s) than the generic 3–600s. */
function intervalMax(ch: string): number {
  return ch.startsWith("whatsapp") ? 120 : 600;
}

function templateVariables(tpl: MsgTemplate | undefined): string[] {
  if (!tpl) return [];
  if (tpl.channel === "whatsapp") {
    const wa = tpl as WhatsAppTemplate;
    const text = `${wa.header?.text ?? ""} ${wa.body.text}`;
    const found = new Set<string>();
    for (const m of text.matchAll(/\{\{\s*(\w+)\s*\}\}/g)) found.add(m[1]);
    return Array.from(found);
  }
  if (tpl.channel === "email") return tpl.variables ?? [];
  return [];
}

function templateName(tpl: MsgTemplate): string {
  return tpl.name;
}

const STEP_KEYS = [
  "bc.wiz.step.channel",
  "bc.wiz.step.audience",
  "bc.wiz.step.content",
  "bc.wiz.step.rules",
  "bc.wiz.step.schedule",
  "bc.wiz.step.confirm",
] as const;

export function BroadcastWizard({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [step, setStep] = useState(0);
  const [segBuilderOpen, setSegBuilderOpen] = useState(false);

  // form state
  const [name, setName] = useState("");
  const [channelType, setChannelType] = useState<string>("");
  const [accountId, setAccountId] = useState<string>("");
  const [segmentId, setSegmentId] = useState<string>("");
  const [templateId, setTemplateId] = useState<string>("");
  const [varMap, setVarMap] = useState<Record<string, string>>({});
  const [intervalSeconds, setIntervalSeconds] = useState(10);
  const [useWindow, setUseWindow] = useState(false);
  const [windowRange, setWindowRange] = useState<[number, number]>([540, 1260]); // 09:00–21:00
  const [weeklyCap, setWeeklyCap] = useState<number | null>(null);
  const [scheduleMode, setScheduleMode] = useState<BroadcastSchedule["mode"]>("immediate");
  const [sendAt, setSendAt] = useState<string | null>(null);
  const [rrule, setRrule] = useState("");

  const accounts = useQuery({
    queryKey: ["channel-accounts"],
    queryFn: () => channelsApi.listAccounts(),
    enabled: open,
    retry: 1,
  });
  const segments = useQuery({
    queryKey: ["segments"],
    queryFn: () => segmentsApi.list(),
    enabled: open,
    retry: 1,
  });

  const tplChannel = templateChannelFor(channelType);
  const templates = useQuery({
    queryKey: ["msg-templates", tplChannel],
    queryFn: () => msgTemplatesApi.list(tplChannel as TemplateChannel),
    enabled: open && !!tplChannel,
    retry: 1,
  });

  const connectedTypes = useMemo(() => {
    const set = new Set<string>();
    for (const a of accounts.data ?? [])
      if (a.status !== "disconnected") set.add(galleryType(a.channel_type));
    return set;
  }, [accounts.data]);

  const channelAccounts = (accounts.data ?? []).filter(
    (a) => galleryType(a.channel_type) === channelType,
  );
  const selectedTemplate = (templates.data ?? []).find((tp) => tp.id === templateId);
  const variables = templateVariables(selectedTemplate);

  const reset = () => {
    setStep(0);
    setName("");
    setChannelType("");
    setAccountId("");
    setSegmentId("");
    setTemplateId("");
    setVarMap({});
    setIntervalSeconds(10);
    setUseWindow(false);
    setWindowRange([540, 1260]);
    setWeeklyCap(null);
    setScheduleMode("immediate");
    setSendAt(null);
    setRrule("");
  };

  const create = useMutation({
    mutationFn: () => {
      const schedule: BroadcastSchedule = {
        mode: scheduleMode,
        send_at: scheduleMode === "scheduled" ? sendAt : null,
        rrule: scheduleMode === "recurring" ? rrule : null,
        timezone: "Asia/Hong_Kong",
      };
      const body: BroadcastCreateBody = {
        name: name.trim(),
        type: scheduleMode === "recurring" ? "recurring" : "one_time",
        // the backend validates channel_type against the ACCOUNT's stored
        // canonical type (whatsapp_cloud/whatsapp_bsp) — send that, not the
        // gallery family name
        channel_type:
          (accounts.data ?? []).find((a) => a.id === accountId)?.channel_type ?? channelType,
        channel_account_id: accountId,
        segment_id: segmentId,
        template_id: templateId,
        variable_mapping: varMap,
        schedule,
        send_rules: {
          interval_seconds: intervalSeconds,
          window: useWindow ? { start_min: windowRange[0], end_min: windowRange[1] } : null,
          per_contact_weekly_cap: weeklyCap,
        },
      };
      return broadcastsApi.create(body);
    },
    onSuccess: () => {
      message.success(t("common.createSuccess"));
      void qc.invalidateQueries({ queryKey: ["broadcasts"] });
      reset();
      onClose();
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const canNext = (): boolean => {
    switch (step) {
      case 0:
        return !!name.trim() && !!channelType && !!accountId;
      case 1:
        return !!segmentId;
      case 2:
        return !tplChannel || !!templateId;
      case 3:
        return intervalSeconds >= 3;
      case 4:
        return (
          scheduleMode === "immediate" ||
          (scheduleMode === "scheduled" && !!sendAt) ||
          (scheduleMode === "recurring" && !!rrule.trim())
        );
      default:
        return true;
    }
  };

  const close = () => {
    reset();
    onClose();
  };

  return (
    <Modal
      title={t("bc.wiz.title")}
      open={open}
      onCancel={close}
      width={780}
      footer={
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <Button onClick={close}>{t("common.cancel")}</Button>
          <div style={{ display: "flex", gap: 8 }}>
            {step > 0 && <Button onClick={() => setStep((s) => s - 1)}>{t("common.prev")}</Button>}
            {step < STEP_KEYS.length - 1 ? (
              <Button type="primary" disabled={!canNext()} onClick={() => setStep((s) => s + 1)}>
                {t("common.next")}
              </Button>
            ) : (
              <Button type="primary" loading={create.isPending} onClick={() => create.mutate()}>
                {t("common.submit")}
              </Button>
            )}
          </div>
        </div>
      }
    >
      <Steps
        size="small"
        current={step}
        items={STEP_KEYS.map((k) => ({ title: t(k) }))}
        style={{ marginBottom: 20 }}
      />

      <div style={{ minHeight: 260 }}>
        {/* step 0 — channel + name + account */}
        {step === 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div>
              <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.name")}</div>
              <Input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={50}
                placeholder={t("bc.wiz.namePlaceholder")}
              />
            </div>
            <div>
              <div className="sc-mkt-hint" style={{ marginBottom: 6 }}>
                {t("bc.wiz.step.channel")}｜{t("bc.wiz.channelHint")}
              </div>
              <div className="sc-chan-grid">
                {BROADCAST_CHANNELS.map((ch) => {
                  const connected = connectedTypes.has(ch);
                  return (
                    <button
                      type="button"
                      key={ch}
                      className={`sc-chan-tile${channelType === ch ? " sc-active" : ""}`}
                      disabled={!connected}
                      onClick={() => {
                        setChannelType(ch);
                        setAccountId("");
                        setTemplateId("");
                        setVarMap({});
                        setIntervalSeconds(Math.min(10, intervalMax(ch)));
                      }}
                    >
                      <ChannelIcon type={ch} size={22} />
                      <div>
                        <div className="sc-chan-tile-name">{CHANNEL_NAME[ch] ?? ch}</div>
                        <div className="sc-chan-tile-sub">
                          {connected ? t("common.enabled") : t("bc.wiz.noAccount")}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
            {channelType && (
              <div>
                <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.selectAccount")}</div>
                <Select
                  style={{ width: "100%" }}
                  value={accountId || undefined}
                  onChange={setAccountId}
                  placeholder={t("common.select")}
                  options={channelAccounts.map((a) => ({ value: a.id, label: a.display_name }))}
                />
              </div>
            )}
          </div>
        )}

        {/* step 1 — audience */}
        {step === 1 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <div>
              <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.selectSegment")}</div>
              <div style={{ display: "flex", gap: 8 }}>
                <Select
                  style={{ flex: 1 }}
                  value={segmentId || undefined}
                  onChange={setSegmentId}
                  placeholder={t("common.select")}
                  loading={segments.isLoading}
                  options={(segments.data ?? []).map((s) => ({
                    value: s.id,
                    label: s.count != null ? `${s.name} · ${s.count}` : s.name,
                  }))}
                />
                <Button icon={<PlusOutlined />} onClick={() => setSegBuilderOpen(true)}>
                  {t("bc.wiz.createSegment")}
                </Button>
              </div>
            </div>
            {segmentId && (
              <div style={{ fontSize: 13, color: "var(--sc-text-secondary)" }}>
                {(() => {
                  const seg = (segments.data ?? []).find((s) => s.id === segmentId);
                  return seg?.count != null
                    ? `${t("bc.wiz.estimated")}: ${seg.count}`
                    : null;
                })()}
              </div>
            )}
          </div>
        )}

        {/* step 2 — template + variable mapping */}
        {step === 2 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            {!tplChannel ? (
              <div className="sc-mkt-hint">{t("bc.wiz.noTemplate")}</div>
            ) : (
              <>
                <div>
                  <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.selectTemplate")}</div>
                  <Select
                    style={{ width: "100%" }}
                    value={templateId || undefined}
                    onChange={(v) => {
                      setTemplateId(v);
                      setVarMap({});
                    }}
                    placeholder={t("common.select")}
                    loading={templates.isLoading}
                    notFoundContent={t("bc.wiz.noTemplate")}
                    options={(templates.data ?? []).map((tp) => ({
                      value: tp.id,
                      label: templateName(tp),
                    }))}
                  />
                </div>
                {variables.length > 0 && (
                  <div>
                    <div className="sc-mkt-hint" style={{ marginBottom: 6 }}>
                      {t("bc.wiz.varMapping")}｜{t("bc.wiz.varMappingHint")}
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                      {variables.map((v) => (
                        <div key={v} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <Tag style={{ margin: 0, minWidth: 56, textAlign: "center" }}>{`{{${v}}}`}</Tag>
                          <Input
                            style={{ flex: 1 }}
                            value={varMap[v] ?? ""}
                            onChange={(e) => setVarMap((m) => ({ ...m, [v]: e.target.value }))}
                            placeholder="{{contact.display_name}}"
                          />
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* step 3 — send rules */}
        {step === 3 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div>
              <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>
                {t("bc.wiz.interval")}｜{t("bc.wiz.intervalHint")}
              </div>
              <InputNumber
                min={3}
                max={intervalMax(channelType)}
                value={intervalSeconds}
                onChange={(v) => setIntervalSeconds(v ?? 3)}
                addonAfter={t("common.seconds")}
              />
            </div>
            <div>
              <Radio.Group value={useWindow} onChange={(e) => setUseWindow(e.target.value)}>
                <Radio value={false}>{t("common.all")}</Radio>
                <Radio value={true}>{t("bc.wiz.window")}</Radio>
              </Radio.Group>
              {useWindow && (
                <div style={{ marginTop: 8 }}>
                  <TimePicker.RangePicker
                    format="HH:mm"
                    minuteStep={30}
                    value={[
                      dayjs().startOf("day").add(windowRange[0], "minute"),
                      dayjs().startOf("day").add(windowRange[1], "minute"),
                    ]}
                    onChange={(v) => {
                      if (v && v[0] && v[1])
                        setWindowRange([hhmmToMin(v[0].format("HH:mm")), hhmmToMin(v[1].format("HH:mm"))]);
                    }}
                  />
                  <div className="sc-mkt-hint">{t("bc.wiz.windowHint")}</div>
                </div>
              )}
            </div>
            <div>
              <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.weeklyCap")}</div>
              <InputNumber
                min={0}
                value={weeklyCap ?? undefined}
                onChange={(v) => setWeeklyCap(v ?? null)}
                placeholder={t("common.none")}
              />
            </div>
          </div>
        )}

        {/* step 4 — schedule */}
        {step === 4 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Radio.Group
              value={scheduleMode}
              onChange={(e) => setScheduleMode(e.target.value)}
              options={[
                { value: "immediate", label: t("bc.wiz.immediate") },
                { value: "scheduled", label: t("bc.wiz.scheduled") },
                { value: "recurring", label: t("bc.wiz.recurring") },
              ]}
              optionType="button"
            />
            {scheduleMode === "scheduled" && (
              <div>
                <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.sendAt")}</div>
                <DatePicker
                  showTime
                  format="YYYY-MM-DD HH:mm"
                  value={sendAt ? dayjs(sendAt) : null}
                  onChange={(v) => setSendAt(v ? v.toISOString() : null)}
                />
              </div>
            )}
            {scheduleMode === "recurring" && (
              <div>
                <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.rrule")}</div>
                <Input
                  value={rrule}
                  onChange={(e) => setRrule(e.target.value)}
                  placeholder={t("bc.wiz.rrulePlaceholder")}
                />
              </div>
            )}
          </div>
        )}

        {/* step 5 — confirm */}
        {step === 5 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div className="sc-mkt-hint" style={{ marginBottom: 4 }}>{t("bc.wiz.confirmHint")}</div>
            {(
              [
                [t("bc.wiz.name"), name],
                [t("bc.wiz.review.channel"), CHANNEL_NAME[channelType] ?? channelType],
                [
                  t("bc.wiz.review.account"),
                  channelAccounts.find((a) => a.id === accountId)?.display_name ?? "—",
                ],
                [
                  t("bc.wiz.review.audience"),
                  (segments.data ?? []).find((s) => s.id === segmentId)?.name ?? "—",
                ],
                [t("bc.wiz.review.template"), selectedTemplate ? templateName(selectedTemplate) : "—"],
                [
                  t("bc.wiz.interval"),
                  `${intervalSeconds} ${t("common.seconds")}${
                    useWindow ? ` · ${minToHHmm(windowRange[0])}–${minToHHmm(windowRange[1])}` : ""
                  }`,
                ],
                [
                  t("bc.wiz.review.schedule"),
                  scheduleMode === "immediate"
                    ? t("bc.wiz.immediate")
                    : scheduleMode === "scheduled"
                      ? `${t("bc.wiz.scheduled")} · ${sendAt ? dayjs(sendAt).format("YYYY-MM-DD HH:mm") : ""}`
                      : `${t("bc.wiz.recurring")} · ${rrule}`,
                ],
              ] as [string, string][]
            ).map(([label, value]) => (
              <div key={label} style={{ display: "flex", gap: 12, fontSize: 13.5, padding: "3px 0" }}>
                <span style={{ width: 96, flex: "none", color: "var(--sc-text-tertiary)" }}>{label}</span>
                <span style={{ flex: 1, color: "var(--sc-text)" }}>{value}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <SegmentBuilder
        open={segBuilderOpen}
        onClose={() => setSegBuilderOpen(false)}
        onCreated={(seg: Segment) => {
          setSegBuilderOpen(false);
          void qc.invalidateQueries({ queryKey: ["segments"] });
          setSegmentId(seg.id);
        }}
      />
    </Modal>
  );
}
