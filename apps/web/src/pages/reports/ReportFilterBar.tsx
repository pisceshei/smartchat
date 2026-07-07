/** Shared reports filter bar: 時間範圍 / 統計間隔 / 社媒 / 帳號 / 接待成員
 *  + 分享 + 匯出. Controlled — owns nothing but surfaces changes via onChange.
 *  Export polls /reports/exports/{job_id} until the signed CSV URL is ready. */
import { DownloadOutlined, ShareAltOutlined } from "@ant-design/icons";
import { App, Button, DatePicker, Segmented, Select } from "antd";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { channelsApi, membersApi, reportsApi } from "@/api/endpoints";
import type { CustomerDimension, ReportFilters, ReportInterval } from "@/api/types";
import { CHANNEL_NAME } from "@/constants/channels";
import { t } from "@/i18n";
import { dayjs } from "@/utils/time";
import "./reports.css";

const { RangePicker } = DatePicker;

export interface ReportFilterBarProps {
  value: ReportFilters;
  onChange: (next: ReportFilters) => void;
  /** report key for export/share (e.g. "customers"); omit to hide those buttons. */
  reportKey?: string;
  /** extra body sent to export/share (e.g. {dimension}). */
  exportExtra?: { dimension?: CustomerDimension };
  showInterval?: boolean;
  showChannel?: boolean;
  showMember?: boolean;
}

export function ReportFilterBar({
  value,
  onChange,
  reportKey,
  exportExtra,
  showInterval = true,
  showChannel = true,
  showMember = true,
}: ReportFilterBarProps) {
  const { message } = App.useApp();
  const [exporting, setExporting] = useState(false);
  const [sharing, setSharing] = useState(false);

  const accounts = useQuery({ queryKey: ["channel-accounts"], queryFn: () => channelsApi.listAccounts(), retry: 1 });
  const members = useQuery({ queryKey: ["members"], queryFn: () => membersApi.list(), enabled: showMember, retry: 1 });

  const channelTypes = Array.from(new Set((accounts.data ?? []).map((a) => a.channel_type)));
  const accountsForChannel = (accounts.data ?? []).filter(
    (a) => !value.channel_type || a.channel_type === value.channel_type,
  );

  const patch = (p: Partial<ReportFilters>) => onChange({ ...value, ...p });

  const runExport = async () => {
    if (!reportKey) return;
    setExporting(true);
    try {
      const { job_id } = await reportsApi.export(reportKey, { ...value, ...exportExtra });
      // poll up to ~30s for the signed URL
      for (let i = 0; i < 30; i++) {
        const st = await reportsApi.exportStatus(job_id);
        if (st.status === "ready" && st.url) {
          window.open(st.url, "_blank");
          message.success(t("rpt.exportReady"));
          return;
        }
        if (st.status === "failed") throw new Error("failed");
        await new Promise((r) => setTimeout(r, 1000));
      }
      message.info(t("rpt.exporting"));
    } catch {
      message.error(t("rpt.exportFailed"));
    } finally {
      setExporting(false);
    }
  };

  const runShare = async () => {
    if (!reportKey) return;
    setSharing(true);
    try {
      const { url } = await reportsApi.share(reportKey, { ...value, ...exportExtra });
      await navigator.clipboard.writeText(url);
      message.success(t("rpt.shared"));
    } catch {
      message.error(t("rpt.shareFailed"));
    } finally {
      setSharing(false);
    }
  };

  return (
    <div className="sc-rpt-filterbar">
      <RangePicker
        allowClear={false}
        value={[value.from ? dayjs(value.from) : dayjs().subtract(7, "day"), value.to ? dayjs(value.to) : dayjs()]}
        onChange={(v) => {
          if (v && v[0] && v[1])
            patch({ from: v[0].startOf("day").toISOString(), to: v[1].endOf("day").toISOString() });
        }}
      />

      {showInterval && (
        <Segmented
          value={value.interval ?? "day"}
          onChange={(v) => patch({ interval: v as ReportInterval })}
          options={[
            { value: "hour", label: t("rpt.interval.hour") },
            { value: "day", label: t("rpt.interval.day") },
            { value: "week", label: t("rpt.interval.week") },
            { value: "month", label: t("rpt.interval.month") },
          ]}
        />
      )}

      {showChannel && (
        <>
          <Select
            allowClear
            style={{ width: 140 }}
            placeholder={t("rpt.filter.allChannels")}
            value={value.channel_type ?? undefined}
            onChange={(v) => patch({ channel_type: v ?? null, channel_account_id: null })}
            options={channelTypes.map((c) => ({ value: c, label: CHANNEL_NAME[c] ?? c }))}
          />
          <Select
            allowClear
            style={{ width: 160 }}
            placeholder={t("rpt.filter.allAccounts")}
            value={value.channel_account_id ?? undefined}
            onChange={(v) => patch({ channel_account_id: v ?? null })}
            options={accountsForChannel.map((a) => ({ value: a.id, label: a.display_name }))}
          />
        </>
      )}

      {showMember && (
        <Select
          allowClear
          style={{ width: 150 }}
          placeholder={t("rpt.filter.allMembers")}
          value={value.member_id ?? undefined}
          onChange={(v) => patch({ member_id: v ?? null })}
          options={(members.data ?? []).map((m) => ({ value: m.id, label: m.display_name }))}
        />
      )}

      <span className="sc-rpt-spacer" />
      <span className="sc-rpt-tz">{t("rpt.tz")}</span>

      {reportKey && (
        <>
          <Button icon={<ShareAltOutlined />} loading={sharing} onClick={runShare}>{t("rpt.share")}</Button>
          <Button icon={<DownloadOutlined />} loading={exporting} onClick={runExport}>{t("rpt.export")}</Button>
        </>
      )}
    </div>
  );
}

/** Default 7-day / daily filter used to seed each report page. */
export function defaultFilters(interval: ReportInterval = "day"): ReportFilters {
  return {
    from: dayjs().subtract(7, "day").startOf("day").toISOString(),
    to: dayjs().endOf("day").toISOString(),
    interval,
  };
}
