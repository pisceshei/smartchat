/** 排班 — per-member weekly grid (weekday × time range + rest toggle). */
import { ScheduleOutlined } from "@ant-design/icons";
import { App, Button, Select, Skeleton, Switch, TimePicker } from "antd";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { membersApi, shiftsApi } from "@/api/endpoints";
import type { ShiftSlot } from "@/api/types";
import { EmptyState } from "@/components/EmptyState";
import { t } from "@/i18n";
import { dayjs, hhmmToMin, minToHHmm } from "@/utils/time";

const WEEKDAY_KEYS = [
  "shifts.weekday.1",
  "shifts.weekday.2",
  "shifts.weekday.3",
  "shifts.weekday.4",
  "shifts.weekday.5",
  "shifts.weekday.6",
  "shifts.weekday.7",
] as const;

function defaultSlots(): ShiftSlot[] {
  return [1, 2, 3, 4, 5, 6, 7].map((weekday) => ({
    weekday,
    start_min: 9 * 60,
    end_min: 18 * 60,
    enabled: weekday <= 5,
  }));
}

export function ShiftsPage() {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [memberId, setMemberId] = useState<string | undefined>(undefined);
  const [slots, setSlots] = useState<ShiftSlot[]>(defaultSlots());

  const members = useQuery({ queryKey: ["members"], queryFn: () => membersApi.list(), retry: 1 });
  const enabledQ = useQuery({
    queryKey: ["shifts-enabled"],
    queryFn: () => shiftsApi.getEnabled(),
    retry: 0,
  });

  const shiftsQ = useQuery({
    queryKey: ["shifts", memberId],
    queryFn: () => shiftsApi.get(memberId!),
    enabled: !!memberId,
    retry: 0,
  });

  useEffect(() => {
    if (shiftsQ.data) {
      const byDay = new Map(shiftsQ.data.slots.map((s) => [s.weekday, s]));
      setSlots(
        [1, 2, 3, 4, 5, 6, 7].map(
          (weekday) =>
            byDay.get(weekday) ?? { weekday, start_min: 9 * 60, end_min: 18 * 60, enabled: false },
        ),
      );
    } else if (memberId && shiftsQ.isError) {
      setSlots(defaultSlots());
    }
  }, [shiftsQ.data, shiftsQ.isError, memberId]);

  const setGlobalEnabled = useMutation({
    mutationFn: (enabled: boolean) => shiftsApi.setEnabled(enabled),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["shifts-enabled"] }),
    onError: () => message.error(t("common.operationFailed")),
  });

  const save = useMutation({
    mutationFn: () => shiftsApi.save(memberId!, { member_id: memberId!, slots }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["shifts", memberId] });
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  const setSlot = (weekday: number, patch: Partial<ShiftSlot>) => {
    setSlots((prev) => prev.map((s) => (s.weekday === weekday ? { ...s, ...patch } : s)));
  };

  return (
    <div className="sc-page">
      <div className="sc-page-header">
        <h1 className="sc-page-title">{t("shifts.title")}</h1>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13.5 }}>
          <span className="sc-text-secondary">{t("shifts.enable")}</span>
          <Switch
            checked={enabledQ.data?.enabled ?? false}
            onChange={(v) => setGlobalEnabled.mutate(v)}
            loading={setGlobalEnabled.isPending}
          />
        </span>
      </div>

      <div className="sc-page-body">
        <div style={{ fontSize: 12.5, color: "var(--sc-text-tertiary)", marginBottom: 14 }}>
          {t("shifts.enableHint")}
        </div>

        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 18 }}>
          <span style={{ fontSize: 13.5, color: "var(--sc-text-secondary)" }}>{t("shifts.member")}</span>
          <Select
            style={{ width: 240 }}
            placeholder={t("shifts.selectMember")}
            value={memberId}
            loading={members.isLoading}
            options={(members.data ?? [])
              .filter((m) => m.member_type === "human")
              .map((m) => ({ value: m.id, label: m.display_name }))}
            onChange={setMemberId}
            showSearch
            optionFilterProp="label"
          />
          {memberId && (
            <Button type="primary" onClick={() => save.mutate()} loading={save.isPending}>
              {t("common.save")}
            </Button>
          )}
        </div>

        {!memberId ? (
          <EmptyState icon={<ScheduleOutlined />} title={t("shifts.selectMember")} />
        ) : shiftsQ.isLoading ? (
          <Skeleton active paragraph={{ rows: 7 }} />
        ) : (
          <div
            style={{
              background: "var(--sc-bg-container)",
              border: "1px solid var(--sc-border)",
              borderRadius: 10,
              overflow: "hidden",
              maxWidth: 620,
            }}
          >
            {slots.map((slot, i) => (
              <div
                key={slot.weekday}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 16,
                  padding: "10px 16px",
                  borderTop: i > 0 ? "1px solid var(--sc-border)" : undefined,
                  background: slot.enabled ? undefined : "var(--sc-bg-subtle)",
                }}
              >
                <span style={{ width: 48, fontWeight: 500 }}>{t(WEEKDAY_KEYS[slot.weekday - 1])}</span>
                <Switch
                  size="small"
                  checked={slot.enabled}
                  onChange={(enabled) => setSlot(slot.weekday, { enabled })}
                />
                {slot.enabled ? (
                  <TimePicker.RangePicker
                    format="HH:mm"
                    minuteStep={15}
                    allowClear={false}
                    value={[
                      dayjs(minToHHmm(slot.start_min), "HH:mm"),
                      dayjs(minToHHmm(slot.end_min), "HH:mm"),
                    ]}
                    onChange={(range) => {
                      if (range?.[0] && range?.[1]) {
                        setSlot(slot.weekday, {
                          start_min: hhmmToMin(range[0].format("HH:mm")),
                          end_min: hhmmToMin(range[1].format("HH:mm")),
                        });
                      }
                    }}
                  />
                ) : (
                  <span className="sc-text-tertiary" style={{ fontSize: 13 }}>
                    {t("shifts.rest")}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
