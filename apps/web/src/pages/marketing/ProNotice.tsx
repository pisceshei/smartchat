/** Non-blocking Pro-gate banner. Broadcasts + reports are Pro features; when the
 *  workspace is on Free we surface an upgrade hint but still render the page so
 *  the module is explorable (the backend enforces the hard gate). */
import { CrownOutlined } from "@ant-design/icons";
import { Button } from "antd";
import { useNavigate } from "react-router-dom";
import { t } from "@/i18n";
import { useIsPro } from "@/pages/billing/plan";

export function ProNotice({ message }: { message?: string }) {
  const isPro = useIsPro();
  const navigate = useNavigate();
  if (isPro) return null;
  return (
    <div className="sc-pro-notice">
      <CrownOutlined />
      <span style={{ flex: 1 }}>{message ?? t("common.gate.proRequired")}</span>
      <Button size="small" type="primary" onClick={() => navigate("/subscription/change-plan")}>
        {t("common.gate.upgrade")}
      </Button>
    </div>
  );
}
