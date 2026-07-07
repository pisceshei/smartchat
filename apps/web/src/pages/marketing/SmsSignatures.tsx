/** SMS signatures manager + GSM-7/UCS-2 segment estimator.
 *  Contract: GET/POST /msg-templates/sms/signatures. */
import { PlusOutlined } from "@ant-design/icons";
import { App, Button, Empty, Input, List, Modal, Skeleton } from "antd";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { msgTemplatesApi } from "@/api/endpoints";
import { t } from "@/i18n";

/** Rough GSM-7 vs UCS-2 segmentation for a live cost/segment preview. */
export function smsSegments(text: string): { encoding: string; chars: number; segments: number } {
  const chars = [...text].length;
  const isUcs2 = /[^\x00-\x7F]/.test(text);
  const single = isUcs2 ? 70 : 160;
  const multi = isUcs2 ? 67 : 153;
  const segments = chars === 0 ? 0 : chars <= single ? 1 : Math.ceil(chars / multi);
  return { encoding: isUcs2 ? "UCS-2" : "GSM-7", chars, segments };
}

export function SmsSignaturesModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const { message } = App.useApp();
  const [name, setName] = useState("");
  const [text, setText] = useState("");

  const list = useQuery({
    queryKey: ["sms-signatures"],
    queryFn: () => msgTemplatesApi.signatures(),
    enabled: open,
    retry: 1,
  });

  const create = useMutation({
    mutationFn: () => msgTemplatesApi.createSignature({ name: name.trim(), text: text.trim() }),
    onSuccess: () => {
      message.success(t("common.saveSuccess"));
      void qc.invalidateQueries({ queryKey: ["sms-signatures"] });
      setName("");
      setText("");
    },
    onError: () => message.error(t("common.operationFailed")),
  });

  return (
    <Modal title={t("tpl.sms.addSignature")} open={open} onCancel={onClose} footer={null}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10, paddingTop: 6 }}>
        <Input placeholder={t("tpl.sms.signatureName")} value={name} onChange={(e) => setName(e.target.value)} />
        <Input placeholder={t("tpl.sms.signatureText")} value={text} onChange={(e) => setText(e.target.value)} />
        <Button
          type="primary"
          icon={<PlusOutlined />}
          loading={create.isPending}
          disabled={!name.trim() || !text.trim()}
          onClick={() => create.mutate()}
        >
          {t("common.add")}
        </Button>
        <div style={{ marginTop: 6 }}>
          {list.isLoading ? (
            <Skeleton active paragraph={{ rows: 2 }} />
          ) : (list.data?.length ?? 0) === 0 ? (
            <Empty description={t("common.emptyData")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
          ) : (
            <List
              size="small"
              dataSource={list.data ?? []}
              renderItem={(s) => (
                <List.Item>
                  <span style={{ fontWeight: 600 }}>{s.name}</span>
                  <span style={{ color: "var(--sc-text-secondary)" }}>{s.text}</span>
                </List.Item>
              )}
            />
          )}
        </div>
      </div>
    </Modal>
  );
}
